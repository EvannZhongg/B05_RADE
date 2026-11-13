import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from unittest.mock import AsyncMock, patch

from tests.filesystem_fixtures import symlinks_available

from agent_core import (
    TokenUsage,
    JsonlSessionStore,
    MemoryDocument,
    MemoryStore,
    Session,
    SessionInUseError,
    Workspace,
)
from agent_core.llm import AuxiliaryModelCall
from agent_runtime.settings import SettingsStore

try:
    from fastapi.testclient import TestClient
    from fastapi import WebSocketDisconnect

    from interfaces.bridge import protocol as bridge_protocol
    from interfaces.bridge.event_stream import EventStream
    from interfaces.gui import server as gui_server
    from interfaces import session_host
    from interfaces import bridge_process
except ModuleNotFoundError:  # pragma: no cover - exercised without [server]
    TestClient = None


SYMLINKS_AVAILABLE = symlinks_available()


class FakeBridge:
    """Stands in for the bridge child process.

    Records what the server forwards and emits scripted protocol
    messages in reply, so the relay can be tested without a model. A
    ``None`` in a reply list ends the message stream, which is how a
    real bridge exit reaches the server.
    """

    def __init__(
        self,
        replies: dict[str, list[dict | None]] | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.cancelled = 0
        self.closed = False
        self.returncode: int | None = None
        self._replies = replies or {}
        self._messages: asyncio.Queue[dict | None] = asyncio.Queue()
        self._stream = EventStream()
        self.state = bridge_protocol.runtime_state_message(
            cache_usage=None,
            phase="inactive", provider=None,
            context_window=None, jobs=[],
        )
        self.items = []

    def send(self, message: dict) -> None:
        self.sent.append(message)
        if message["type"] == "open_session":
            self.state.update(provider=message.get("provider"))
        elif message["type"] == "user_turn":
            self.state.update(phase="starting", turn_id=message["turn_id"])
            self.items.append({"role": "user", "content": message["text"]})
            if not self._replies.get("user_turn"):
                self.emit(dict(self.state))
        elif message["type"] == "cancel":
            self.cancel_turn()
        for reply in self._replies.get(str(message["type"]), ()):
            self.emit(reply)

    async def read(self) -> dict | None:
        return await self._messages.get()

    def emit(self, *messages: dict | None) -> None:
        for message in messages:
            if message is None:
                self._messages.put_nowait(None)
                continue
            kind = message["type"]
            if kind == "runtime_state":
                self.state.update(message)
                message = dict(self.state)
            elif kind == "provider_changed":
                self.state["provider"] = message["provider"]
            elif kind == "plan_updated":
                self.state["plan"] = message["plan"]
            elif kind == "assistant_message":
                self.items.append({"role": "assistant", "content": message["content"]})
            elif kind in {"turn_completed", "turn_failed", "turn_cancelled", "fatal"}:
                self.state.update(phase="failed" if kind == "fatal" else "idle", turn_id=None)
            self._messages.put_nowait(self._stream.publish(
                message, state=None if kind == "session_open_failed" else dict(self.state),
                items=list(self.items) if kind in {"session_ready", "turn_completed", "turn_failed", "turn_cancelled"} else None,
            ))

    def cancel_turn(self) -> None:
        self.cancelled += 1
        self._messages.put_nowait(None)

    async def close(self) -> None:
        self.closed = True


class FakeBridgeProcess:
    available = True

    def __init__(self) -> None:
        self.channels = []

    def track(self, channel):
        self.channels.append(channel)
        return channel

    def open_channel(self, channel_id):
        return self.track(FakeBridge())

    async def close(self):
        for channel in self.channels:
            await channel.close()


@unittest.skipIf(TestClient is None, "Install the server extra to test the GUI")
class ActiveSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_release_keeps_same_session_waiting_without_blocking_other_sessions(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class ClosingChannel(FakeBridge):
            async def close(self):
                entered.set()
                await release.wait()
                await super().close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = FakeBridgeProcess()
            channels = [ClosingChannel(), FakeBridge(), FakeBridge()]
            with patch.object(process, "open_channel", side_effect=lambda key: process.track(channels.pop(0))):
                registry = session_host.ActiveSessionRegistry(JsonlSessionStore(root / "runs"), root)
                registry._bridge = process
                opening = {"type": "open_session", "session_id": "one"}
                await registry.open_or_attach(opening, attach_only=False)
                closing = asyncio.create_task(registry.release_idle("one"))
                await asyncio.wait_for(entered.wait(), 2)
                closing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await closing
                reopening = asyncio.create_task(registry.open_or_attach(opening, attach_only=False))
                other, created = await asyncio.wait_for(registry.open_or_attach(
                    {**opening, "session_id": "two"}, attach_only=False,
                ), 2)
                self.assertTrue(created)
                self.assertEqual(other.session_id, "two")
                self.assertFalse(reopening.done())
                release.set()
                restored, created = await asyncio.wait_for(reopening, 2)
                self.assertTrue(created)
                self.assertEqual(restored.session_id, "one")
                await registry.close_all()

    async def test_refresh_is_busy_until_the_bridge_publishes_state(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)
        runtime.send({"type": "runtime_refresh"})
        self.assertTrue(runtime.running)
        self.assertEqual(runtime.state["phase"], "inactive")
        bridge.emit(bridge_protocol.runtime_state_message(
            phase="starting", provider="first",
            context_window=None, cache_usage=None, jobs=[],
        ))
        await _wait_for_async(lambda: runtime.event_sequence == 1)
        self.assertTrue(runtime.running)
        bridge.emit(bridge_protocol.runtime_state_message(
            phase="idle", provider="first",
            context_window=None, cache_usage=None, jobs=[],
        ))
        await _wait_for_async(lambda: runtime.event_sequence == 2)
        self.assertTrue(runtime.running)
        bridge.emit({"type": "runtime_refreshed", "provider": "first", "model": "test/model"})
        await _wait_for_async(lambda: runtime.event_sequence == 3)
        self.assertFalse(runtime.running)

    async def test_caches_protocol_cursors_without_deriving_them_from_phase(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)
        items = [{"role": "user", "content": "second turn"}]
        bridge._messages.put_nowait({
            **bridge_protocol.runtime_state_message(
                cache_usage=None,
                phase="running", turn_id="t2",
                provider="first", context_window=None, jobs=[],
            ),
            "event_sequence": 75, "resume_after": 74,
            "transcript": {"items": items, "event_sequence": 63},
        })
        await _wait_for_async(lambda: runtime.event_sequence == 75)
        self.assertEqual(runtime.transcript, {"items": items, "event_sequence": 63})
        queue, events, state = await runtime.attach(63, "page", takeover=False)
        self.assertEqual(state["event_sequence"], 74)
        self.assertEqual([event["event_sequence"] for event in events], [75])
        await runtime.detach(queue, "page")

    async def test_client_commands_do_not_change_the_cached_runtime_snapshot(self) -> None:
        bridge = FakeBridge(replies={"user_turn": [{"type": "assistant_delta", "text": "queued"}]})
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)
        before = dict(runtime.state)
        runtime.send({"type": "user_turn", "turn_id": "t1", "text": "work"})
        self.assertEqual(runtime.state, before)
        self.assertEqual(runtime.transcript, {"items": [], "event_sequence": 0})
        self.assertTrue(runtime.running)

    async def test_event_replay_window_is_bounded_and_sequences_are_monotonic(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)

        bridge.emit(*(
            {
                "type": "assistant_delta",
                "turn_id": "t1",
                "text": str(index),
                "model_call_index": 0,
            }
            for index in range(session_host.EVENT_REPLAY_LIMIT + 8)
        ))
        await _wait_for_async(
            lambda: runtime.event_sequence == session_host.EVENT_REPLAY_LIMIT + 8
        )

        self.assertEqual(len(runtime._events), session_host.EVENT_REPLAY_LIMIT)
        self.assertEqual(runtime._events[0]["event_sequence"], 9)
        self.assertEqual(
            runtime._events[-1]["event_sequence"],
            session_host.EVENT_REPLAY_LIMIT + 8,
        )

    async def test_runtime_state_replaces_the_cached_attachment_snapshot(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)
        bridge.emit(bridge_protocol.runtime_state_message(
            cache_usage=None,
            phase="running",
            turn_id="t1",
            provider="first",
            context_window={"input_tokens": 1},
            jobs=[{"job_id": "j1", "kind": "shell", "status": "running"}],
        ))
        await _wait_for_async(lambda: runtime.event_sequence == 1)

        self.assertEqual([job["job_id"] for job in runtime.state["jobs"]], ["j1"])
        self.assertEqual(runtime.state["context_window"], {"input_tokens": 1})

    async def test_plan_updates_replace_the_cached_snapshot(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)
        first = {"plan_id": "plan-1", "goal": "Ship", "revision": 1, "steps": []}
        second = {"plan_id": "plan-1", "goal": "Ship", "revision": 2, "steps": []}
        bridge.emit(bridge_protocol.runtime_state_message(
            cache_usage=None,
            phase="running",
            turn_id="t1",
            provider="first",
            context_window=None,
            jobs=[],
            plan=first,
        ))
        bridge.emit({"type": "plan_updated", "plan": second})
        await _wait_for_async(lambda: runtime.event_sequence == 2)

        self.assertEqual(runtime.state["plan"], second)

    async def test_owner_close_does_not_relabel_the_runtime_as_failed(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        runtime.state["phase"] = "idle"

        await runtime.close()

        self.assertEqual(runtime.state["phase"], "idle")

    async def test_clean_bridge_exit_does_not_relabel_the_runtime_as_failed(self) -> None:
        bridge = FakeBridge()
        bridge.returncode = 0
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        runtime.state["phase"] = "idle"

        bridge.emit(None)
        await _wait_for_async(lambda: runtime.done)

        self.assertEqual(runtime.state["phase"], "idle")

    async def test_bridge_exit_does_not_invent_a_runtime_transition(self) -> None:
        bridge = FakeBridge()
        bridge.returncode = 0
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        runtime.state["phase"] = "running"

        bridge.emit(None)
        await _wait_for_async(lambda: runtime.done)

        self.assertEqual(runtime.state["phase"], "running")
        self.assertFalse(runtime.attachable)


@unittest.skipIf(TestClient is None, "Install the server extra to test the GUI")
class RuntimeSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_state_replaces_warnings_including_with_an_empty_list(self) -> None:
        bridge = FakeBridge()
        runtime = session_host.ActiveSession("s1", "first", [], bridge)
        self.addAsyncCleanup(runtime.close)

        bridge.emit(
            bridge_protocol.runtime_state_message(
                cache_usage=None,
                phase="running",
                turn_id="t1",
                    provider="first",
                context_window=None,
                jobs=[],
                runtime_warnings=("warning",),
            )
        )
        bridge.emit(
            bridge_protocol.runtime_state_message(
                cache_usage=None,
                phase="idle",
                turn_id=None,
                    provider="first",
                context_window=None,
                jobs=[],
            )
        )

        await _wait_for_async(lambda: runtime.event_sequence == 2)
        self.assertEqual(runtime.state["runtime_warnings"], [])

@unittest.skipIf(TestClient is None, "Install the server extra to test the GUI")
class SessionSearchCancellationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = JsonlSessionStore(root / "runs")
        settings = SettingsStore(root / "config")
        registry = session_host.ActiveSessionRegistry(self.store, settings.directory)
        self.app = gui_server.create_app(self.store, settings, registry)
        self.requests = []
        self.releases = []

    async def asyncTearDown(self) -> None:
        for release in self.releases:
            release.set()
        for task in self.requests:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)

    async def start_search(self, query):
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        receiving = asyncio.Event()
        sent = []

        async def receive():
            message = await incoming.get()
            receiving.set()
            return message

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/api/sessions/search",
            "root_path": "", "query_string": f"q={query}&limit=50&per_session_limit=3".encode(),
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8736),
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        self.requests.append(task)
        await asyncio.wait_for(receiving.wait(), 2)
        return task, incoming, sent

    async def test_search_limits_threads_and_cancels_running_and_queued_requests(self) -> None:
        loop = asyncio.get_running_loop()
        started = {query: asyncio.Event() for query in ("first", "second", "next")}
        releases = {query: Event() for query in started}
        self.releases.extend(releases.values())
        stop_seen = asyncio.Event()
        visited = []
        lock = Lock()
        active = 0
        peak = 0

        def search(query, *, limit, per_session_limit, should_cancel):
            nonlocal active, peak
            with lock:
                visited.append(query)
                active += 1
                peak = max(peak, active)
            try:
                loop.call_soon_threadsafe(started[query].set)
                if query == "first":
                    while not should_cancel():
                        if releases[query].wait(0.01):
                            return []
                    loop.call_soon_threadsafe(stop_seen.set)
                if not releases[query].wait(3):
                    raise AssertionError("search thread was not released")
                return []
            finally:
                with lock:
                    active -= 1

        with patch.object(self.store, "search_messages", side_effect=search):
            first, first_input, first_sent = await self.start_search("first")
            second, _, second_sent = await self.start_search("second")
            await asyncio.wait_for(started["first"].wait(), 2)
            await asyncio.wait_for(started["second"].wait(), 2)

            queued, queued_input, queued_sent = await self.start_search("queued")
            queued_input.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(queued, 2)
            self.assertEqual(queued_sent[0]["status"], 499)

            next_request, _, next_sent = await self.start_search("next")
            first_input.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(stop_seen.wait(), 2)
            self.assertFalse(first.done())
            self.assertEqual(set(visited), {"first", "second"})

            releases["first"].set()
            await asyncio.wait_for(first, 2)
            self.assertEqual(first_sent[0]["status"], 499)
            await asyncio.wait_for(started["next"].wait(), 2)
            self.assertEqual(peak, 2)
            releases["second"].set()
            releases["next"].set()
            await asyncio.wait_for(asyncio.gather(second, next_request), 2)
            self.assertEqual(second_sent[0]["status"], 200)
            self.assertEqual(next_sent[0]["status"], 200)
            self.assertEqual(set(visited), {"first", "second", "next"})

    async def test_search_handler_cancellation_stops_the_worker_and_releases_the_slot(self) -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        stopped = asyncio.Event()
        release = Event()
        self.releases.append(release)

        def search(query, *, limit, per_session_limit, should_cancel):
            loop.call_soon_threadsafe(started.set)
            while not should_cancel():
                if release.wait(0.01):
                    return []
            loop.call_soon_threadsafe(stopped.set)
            return []

        with patch.object(self.store, "search_messages", side_effect=search):
            task, _, _ = await self.start_search("cancelled")
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(stopped.is_set())

        with patch.object(self.store, "search_messages", return_value=[]):
            requests = [await self.start_search(query) for query in ("first", "second")]
            await asyncio.wait_for(asyncio.gather(*(request[0] for request in requests)), 2)
            self.assertTrue(all(request[2][0]["status"] == 200 for request in requests))

    async def test_repeated_handler_cancellation_keeps_the_slot_until_the_thread_exits(self) -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Queue()
        stop_seen = asyncio.Event()
        release_cancelled = Event()
        release_others = Event()
        self.releases.extend((release_cancelled, release_others))
        lock = Lock()
        active = 0
        peak = 0

        def search(query, *, limit, per_session_limit, should_cancel):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                loop.call_soon_threadsafe(started.put_nowait, query)
                if query == "cancelled":
                    while not should_cancel():
                        if release_cancelled.wait(0.01):
                            return []
                    loop.call_soon_threadsafe(stop_seen.set)
                    release = release_cancelled
                else:
                    release = release_others
                if not release.wait(3):
                    raise AssertionError("search thread was not released")
                return []
            finally:
                with lock:
                    active -= 1

        with patch.object(self.store, "search_messages", side_effect=search):
            task, _, _ = await self.start_search("cancelled")
            self.assertEqual(await asyncio.wait_for(started.get(), 2), "cancelled")
            task.cancel()
            await asyncio.wait_for(stop_seen.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            first, _, _ = await self.start_search("first")
            self.assertEqual(await asyncio.wait_for(started.get(), 2), "first")
            second, _, _ = await self.start_search("second")
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(started.get(), 0.05)
            release_cancelled.set()
            self.assertEqual(await asyncio.wait_for(started.get(), 2), "second")
            release_others.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            self.assertEqual(peak, 2)

    async def test_failed_searches_release_their_slots(self) -> None:
        with patch.object(self.store, "search_messages", side_effect=RuntimeError("scan failed")):
            for query in ("first", "second", "third"):
                task, _, sent = await self.start_search(query)
                with self.assertRaisesRegex(RuntimeError, "scan failed"):
                    await asyncio.wait_for(task, 2)
                self.assertEqual(sent[0]["status"], 500)


@unittest.skipIf(TestClient is None, "Install the server extra to test the GUI")
class GuiTest(unittest.TestCase):
    def test_occupied_session_reports_failure_then_allows_selection_or_retry(self) -> None:
        for selected in ("occupied", "another"):
            with self.subTest(selected=selected):
                failed = FakeBridge(replies={"open_session": [
                    bridge_protocol.session_open_failed_message("occupied", SessionInUseError("occupied")),
                ]})
                ready = FakeBridge(replies={"open_session": [
                    {"type": "session_ready", "session_id": selected},
                ]})
                pending = [failed, ready]

                def open_channel(self, channel_id: str) -> FakeBridge:
                    return pending.pop(0)

                with patch.object(FakeBridgeProcess, "open_channel", open_channel), self.client() as client:
                    with client.websocket_connect("/api/session") as socket:
                        socket.send_json({"type": "open_session", "session_id": "occupied", "attachment_id": "page"})
                        message = socket.receive_json()
                        self.assertEqual(message["type"], "session_open_failed")
                        self.assertEqual(message["error"]["type"], "SessionInUseError")
                        self.assertNotIn("runtime", message)
                        self.assertEqual(message["resume_after"], 0)
                        with self.assertRaises(WebSocketDisconnect):
                            socket.receive_json()
                    _wait_for(client, lambda: client.get("/api/active-sessions").json() == [])
                    with client.websocket_connect("/api/session") as socket:
                        socket.send_json({"type": "open_session", "session_id": selected, "attachment_id": "page"})
                        message = socket.receive_json()
                        self.assertEqual(message["type"], "session_ready")
                        self.assertEqual(message["session_id"], selected)
                self.assertTrue(failed.closed)
                self.assertEqual([message["type"] for message in failed.sent], ["open_session"])
                self.assertEqual(pending, [])

    def test_reopening_with_different_preferences_attaches_to_existing_bridge(self) -> None:
        bridge = FakeBridge(replies={
            "open_session": [{"type": "session_ready", "session_id": "shared"}],
            "provider_set": [{"type": "provider_changed", "provider": "second", "model": "openai/second"}],
        })
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({
                    "type": "open_session", "session_id": "shared",
                    "attachment_id": "page", "provider": "first",
                })
                self.assertEqual(first.receive_json()["type"], "session_ready")
            with client.websocket_connect("/api/session") as second:
                second.send_json({
                    "type": "open_session", "session_id": "shared",
                    "attachment_id": "page", "provider": "second",
                })
                self.assertTrue(second.receive_json()["replayed"])
                snapshot = second.receive_json()
                self.assertEqual(snapshot["type"], "runtime_state")
                self.assertEqual(snapshot["provider"], "first")
                self.assertFalse(bridge.closed)
                self.assertEqual([message["type"] for message in bridge.sent], ["open_session"])
                second.send_json({"type": "provider_set", "provider": "second"})
                self.assertEqual(second.receive_json()["type"], "provider_changed")

    def test_new_bridge_ignores_the_previous_process_cursor(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({
                    "type": "open_session", "session_id": "s1", "attachment_id": "page",
                    "after_event": 1000,
                })
                message = socket.receive_json()
                self.assertEqual(message["type"], "session_ready")
                self.assertEqual(message["transcript"]["event_sequence"], 1)

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "README.md").write_text("Hello RADE", encoding="utf-8")
        (self.root / "src").mkdir()
        self.store = JsonlSessionStore(self.root / ".RADE" / "runs")
        self.files = self.store.session_directory("files-run") / "files"
        (self.files / "src").mkdir(parents=True)
        (self.files / "README.md").write_text("run files", encoding="utf-8")
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "provider_config.json").write_text(json.dumps({
            "main_agent": {"provider": "first"},
            "providers": {
                "first": {"model": "openai/first"},
                "second": {"model": "openai/second"},
            },
        }), encoding="utf-8")
        (self.config / "agent_config.json").write_text(json.dumps({
            "max_same_tool_calls": 5,
            "output_reserve_tokens": 100,
            "max_generation_tokens": None,
            "provider": {"request_timeout_seconds": 300, "max_retries": 2},
            "context": {"compression": {"enabled": True, "trigger_ratio": None, "keep_recent_units": 4, "user_anchor_token_budget": 2000}},
            "main_agent": {"tools": {"read_file": True}},
            "subagent_roles": {},
            "mcp": {"enabled": False, "servers": {}},
        }), encoding="utf-8")
        (self.config / "skills").mkdir()
        (self.config / "plugins").mkdir()

    @contextmanager
    def client(self, bridge: FakeBridge | None = None):
        settings = SettingsStore(self.config)
        registry = session_host.ActiveSessionRegistry(self.store, settings.directory)
        app = gui_server.create_app(self.store, settings, registry)
        client = TestClient(
            app,
            base_url="http://127.0.0.1",
            headers={"host": "127.0.0.1"},
        )
        process = FakeBridgeProcess()
        patcher = patch.object(bridge_process.BridgeProcess, "spawn", AsyncMock(return_value=process))
        patcher.start()
        self.addCleanup(patcher.stop)
        if bridge is not None:
            patcher = patch.object(process, "open_channel", side_effect=lambda channel_id: process.track(bridge))
            patcher.start()
            self.addCleanup(patcher.stop)
        with client:
            client.portal.call(registry.start)
            try:
                yield client
            finally:
                client.portal.call(registry.close_all)

    def test_server_builds_open_session_from_browser_session_choices(self) -> None:
        bridge = FakeBridge(replies={
            "open_session": [{"type": "session_ready", "session_id": "resumed"}],
            "user_turn": [{"type": "bye"}, None],
        })
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "resumed",
                        "attachment_id": "page-1",
                        "provider": "second",
                    }
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(socket.receive_json()["type"], "bye")

        # The workspace is not asserted: the server resolves the path the
        # browser sends against the host, so the value it forwards has no
        # host-independent form to compare with.
        opening = bridge.sent[0]
        self.assertEqual(opening["type"], "open_session")
        self.assertEqual(opening["session_id"], "resumed")
        self.assertEqual(opening["provider"], "second")
        self.assertEqual(bridge.sent[1]["text"], "hi")

    def test_rejects_unconfigured_provider_before_spawning(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "provider": "unknown", "attachment_id": "page-1"})
                message = socket.receive_json()

        self.assertEqual(message["type"], "fatal")
        self.assertEqual(bridge.sent, [])

    def test_rejects_an_opening_message_that_is_not_open_session(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                message = socket.receive_json()

        self.assertEqual(message["error"]["type"], "ProtocolError")
        self.assertEqual(bridge.sent, [])

    def test_requires_an_attachment_identity(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "session_id": "s1"})
                message = socket.receive_json()

        self.assertEqual(message["error"]["type"], "ProtocolError")
        self.assertIn("attachment_id", message["error"]["message"])
        self.assertEqual(bridge.sent, [])

    def test_runs_multiple_turns_on_the_same_bridge(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "s1"}],
                "user_turn": [
                    {"type": "turn_completed", "turn_id": "done", "usage": None}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "one"}
                )
                self.assertEqual(socket.receive_json()["type"], "turn_completed")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t2", "text": "two"}
                )
                self.assertEqual(socket.receive_json()["type"], "turn_completed")

            self.assertEqual(
                [message["type"] for message in bridge.sent],
                ["open_session", "user_turn", "user_turn"],
            )
            self.assertFalse(bridge.closed)

    def test_a_page_reading_a_running_session_is_sent_every_event(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "s1"}],
                "user_turn": [
                    {
                        "type": "assistant_delta",
                        "turn_id": "t1",
                        "text": "one",
                        "model_call_index": 0,
                    }
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hello"}
                )
                # Reading the delta proves the Runtime holds it for a page.
                self.assertEqual(first.receive_json()["type"], "assistant_delta")

                session = client.get("/api/sessions/s1").json()
                active_sessions = client.get("/api/active-sessions").json()

                self.assertEqual(session["event_sequence"], 1)
                self.assertEqual(active_sessions[0]["event_sequence"], 1)

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "s1",
                            "attachment_id": "page-2",
                            "attach_only": True,
                            "takeover": True,
                            "after_event": session["event_sequence"],
                        }
                    )
                    self.assertEqual(
                        second.receive_json()["type"], "assistant_delta"
                    )
                    state = second.receive_json()

                    # The page now holds both events, so its cursor is the
                    # newest one and a reconnect asks for nothing.
                    self.assertEqual(state["event_sequence"], 2)

                    with client.websocket_connect("/api/session") as third:
                        third.send_json(
                            {
                                "type": "open_session",
                                "session_id": "s1",
                                "attachment_id": "page-3",
                                "attach_only": True,
                                "takeover": True,
                                "after_event": state["event_sequence"],
                            }
                        )
                        self.assertEqual(
                            third.receive_json()["type"], "runtime_state"
                        )

    def test_an_idle_runtime_does_not_resend_the_events_its_journal_holds(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "s1"}],
                "user_turn": [
                    {"type": "assistant_message", "turn_id": "t1", "content": "world"},
                    {"type": "turn_completed", "turn_id": "t1", "usage": None}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hello"}
                )
                self.assertEqual(socket.receive_json()["type"], "assistant_message")
                self.assertEqual(socket.receive_json()["type"], "turn_completed")

            stored = Session("s1")
            stored.begin_turn("t1")
            stored.add_item("user", "hello")
            stored.add_item("assistant", "world")
            stored.finish_turn("completed", "t1")
            self.store.append_events("s1", stored.journal)

            session = client.get("/api/sessions/s1").json()
            active_sessions = client.get("/api/active-sessions").json()

            # 'ready' and the completion the journal already holds.
            self.assertEqual(
                [item["content"] for item in session["items"]],
                ["hello", "world"],
            )
            self.assertEqual(session["event_sequence"], 3)
            self.assertEqual(active_sessions[0]["event_sequence"], 3)

            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-2",
                        "attach_only": True,
                        "takeover": True,
                        "after_event": session["event_sequence"],
                    }
                )
                state = second.receive_json()

        self.assertEqual(state["type"], "runtime_state")
        self.assertEqual(state["event_sequence"], 3)

    def test_deleting_a_session_closes_its_idle_runtime_first(self) -> None:
        stored = Session("s1")
        stored.begin_turn("t1")
        stored.add_item("user", "hello")
        stored.finish_turn("completed", "t1")
        self.store.append_events("s1", stored.journal)
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})

        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            response = client.delete("/api/sessions/s1")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(bridge.closed)
            self.assertEqual(client.get("/api/active-sessions").json(), [])

    def test_releasing_an_idle_runtime_closes_its_bridge(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            response = client.delete(
                "/api/active-sessions/s1",
                params={"attachment_id": "page-1"},
            )

            self.assertEqual(response.json(), {"released": True})
            self.assertTrue(bridge.closed)
            self.assertEqual(client.get("/api/active-sessions").json(), [])

    def test_stale_model_release_does_not_close_a_replacement_runtime(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-1",
                        "provider": "second",
                    }
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            response = client.delete(
                "/api/active-sessions/s1",
                params={"provider": "first", "attachment_id": "page-1"},
            )

            self.assertEqual(response.json(), {"released": False})
            self.assertFalse(bridge.closed)

    def test_stale_page_release_does_not_close_a_taken_over_runtime(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "s1",
                            "attachment_id": "page-2",
                            "attach_only": True,
                            "takeover": True,
                        }
                    )
                    self.assertEqual(first.receive_json()["type"], "attachment_replaced")
                    while True:
                        state = second.receive_json()
                        if state["type"] == "runtime_state":
                            break
                    self.assertEqual(state["phase"], "inactive")

                    response = client.delete(
                        "/api/active-sessions/s1",
                        params={
                            "provider": "first",
                            "attachment_id": "page-1",
                        },
                    )

                    self.assertEqual(response.json(), {"released": False})
                    self.assertFalse(bridge.closed)

    def test_relays_session_configuration_without_a_user_turn(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [
                    {"type": "session_ready", "session_id": "empty"},
                ],
                "provider_set": [
                    {
                        "type": "provider_changed",
                        "provider": "second",
                        "model": "openai/second",
                    }
                ],
                "runtime_refresh": [
                    {"type": "runtime_refreshed", "provider": "second", "model": "openai/second"}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "empty",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json({"type": "provider_set", "provider": "second"})
                self.assertEqual(
                    socket.receive_json()["type"],
                    "provider_changed",
                )
                socket.send_json({"type": "runtime_refresh"})
                self.assertEqual(socket.receive_json()["type"], "runtime_refreshed")

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "provider_set", "runtime_refresh"],
        )

    def test_deleting_a_session_removes_its_run_files(self) -> None:
        (self.files / "result.txt").write_text("done", encoding="utf-8")
        session = Session("files-run")
        session.add_item("user", "run")
        self.store.append_events(session.session_id, session.journal)

        with self.client() as client:
            response = client.delete("/api/sessions/files-run")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.files.parent.exists())

    def test_does_not_forward_unknown_or_lifecycle_messages(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready"}],
                "user_turn": [{"type": "bye"}, None],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "session_id": "s1", "attachment_id": "page-1"})
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                # A page must not shut the bridge down or restart it.
                socket.send_json({"type": "shutdown"})
                socket.send_json({"type": "open_session", "session_id": "other", "attachment_id": "page-1"})
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(socket.receive_json()["type"], "bye")

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "user_turn"],
        )

    def test_cancel_is_forwarded_to_the_bridge(
        self,
    ) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "session_id": "s1", "attachment_id": "page-1"})
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                # The fake ends its stream on cancel, as an exiting
                # bridge would.
                socket.send_json({"type": "cancel", "turn_id": "t1"})
                _wait_for(client, lambda: bridge.closed)

        self.assertEqual(bridge.cancelled, 1)
        self.assertEqual(
            [m["type"] for m in bridge.sent],
            ["open_session", "user_turn", "cancel"],
        )


    def test_idle_view_detach_reuses_bridge_for_the_next_turn(self) -> None:
        bridge = FakeBridge(replies={
            "open_session": [{"type": "session_ready", "session_id": "shared"}],
            "user_turn": [{"type": "turn_completed", "turn_id": "t1", "usage": None}],
        })
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "open_session", "session_id": "shared", "attachment_id": "page-1"})
                self.assertEqual(first.receive_json()["type"], "session_ready")
            self.assertFalse(bridge.closed)
            with client.websocket_connect("/api/session") as second:
                second.send_json({
                    "type": "open_session", "session_id": "shared", "attachment_id": "page-1",
                    "after_event": 1,
                })
                self.assertEqual(second.receive_json()["type"], "runtime_state")
                second.send_json({"type": "user_turn", "turn_id": "t1", "text": "continue"})
                self.assertEqual(second.receive_json()["type"], "turn_completed")
            self.assertEqual([m["type"] for m in bridge.sent], ["open_session", "user_turn"])
            self.assertFalse(bridge.closed)
        self.assertTrue(bridge.closed)

    def test_opening_a_page_with_another_provider_preserves_the_idle_owner(self) -> None:
        bridge = FakeBridge(replies={
            "open_session": [{"type": "session_ready", "session_id": "shared"}],
            "provider_set": [{"type": "provider_changed", "provider": "second", "model": "second/model"}],
        })
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({
                    "type": "open_session",
                    "session_id": "shared",
                    "attachment_id": "page-1",
                    "provider": "first",
                })
                self.assertEqual(first.receive_json()["type"], "session_ready")

                with client.websocket_connect("/api/session") as second:
                    second.send_json({
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-2",
                        "provider": "second",
                    })
                    self.assertEqual(second.receive_json()["type"], "attachment_replaced")

                self.assertFalse(bridge.closed)
                self.assertEqual(len(bridge.sent), 1)
                self.assertEqual(client.get("/api/active-sessions").json()[0]["provider"], "first")
                first.send_json({"type": "provider_set", "provider": "second"})
                self.assertEqual(first.receive_json()["type"], "provider_changed")

    def test_background_reconnect_does_not_replace_another_page(self) -> None:
        bridge = FakeBridge(
            replies={"open_session": [{"type": "session_ready", "session_id": "shared"}]}
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(first.receive_json()["phase"], "starting")

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "shared",
                            "attachment_id": "page-2",
                            "attach_only": True,
                        }
                    )
                    replaced = second.receive_json()

                self.assertEqual(
                    replaced,
                    {"type": "attachment_replaced", "phase": "starting"},
                )
                bridge.emit(
                    {
                        "type": "assistant_delta",
                        "turn_id": "t1",
                        "text": "still attached",
                        "model_call_index": 0,
                    }
                )
                self.assertEqual(first.receive_json()["text"], "still attached")
                first.send_json({"type": "cancel", "turn_id": "t1"})
                _wait_for(client, lambda: bridge.closed)

    def test_explicit_takeover_replaces_the_previous_page(self) -> None:
        bridge = FakeBridge(
            replies={"open_session": [{"type": "session_ready", "session_id": "shared"}]}
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(first.receive_json()["phase"], "starting")

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "shared",
                            "attachment_id": "page-2",
                            "attach_only": True,
                            "after_event": 2,
                            "takeover": True,
                        }
                    )
                    self.assertEqual(
                        first.receive_json(),
                        {"type": "attachment_replaced", "phase": "starting"},
                    )
                    state = second.receive_json()
                    self.assertEqual(state["phase"], "starting")

                    bridge.emit(
                        {
                            "type": "assistant_delta",
                            "turn_id": "t1",
                            "text": "new owner",
                            "model_call_index": 0,
                        }
                    )
                    self.assertEqual(second.receive_json()["text"], "new owner")
                    second.send_json({"type": "cancel", "turn_id": "t1"})
                    _wait_for(client, lambda: bridge.closed)

    def test_replays_events_and_completion_emitted_while_detached(self) -> None:
        bridge = FakeBridge(
            replies={"open_session": [{"type": "session_ready", "session_id": "shared"}]}
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "open_session", "session_id": "shared", "attachment_id": "page-1"})
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(first.receive_json()["phase"], "starting")

            self.assertFalse(bridge.closed)
            bridge.emit(
                {
                    "type": "assistant_delta",
                    "turn_id": "t1",
                    "text": "done",
                    "model_call_index": 0,
                },
                {"type": "turn_completed", "turn_id": "t1", "usage": None},
            )
            _wait_for(
                client,
                lambda: client.get("/api/active-sessions").json()[0]["phase"] == "idle",
            )

            active_sessions = client.get("/api/active-sessions").json()
            self.assertEqual(active_sessions[0]["session_id"], "shared")
            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                        "attach_only": True,
                        "after_event": 2,
                    }
                )
                delta = second.receive_json()
                completed = second.receive_json()
                state = second.receive_json()

            self.assertEqual(state["phase"], "idle")
            self.assertEqual(delta["text"], "done")
            self.assertEqual(completed["type"], "turn_completed")
            self.assertFalse(bridge.closed)

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "user_turn"],
        )

    def test_reconnecting_marks_old_snapshots_as_replay_before_current_state(self) -> None:
        snapshot = bridge_protocol.runtime_state_message(
            cache_usage=None,
            phase="inactive", turn_id=None,
            provider="first",
            context_window=None, jobs=[],
        )
        bridge = FakeBridge(replies={"open_session": [snapshot]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({
                    "type": "open_session", "session_id": "shared", "attachment_id": "page-1",
                })
                self.assertNotIn("replayed", first.receive_json())
                first.send_json({"type": "user_turn", "turn_id": "t1", "text": "work"})
                _wait_for(client, lambda: len(bridge.sent) == 2)

            with client.websocket_connect("/api/session") as second:
                second.send_json({
                    "type": "open_session", "session_id": "shared", "attachment_id": "page-2",
                    "attach_only": True,
                })
                replay = second.receive_json()
                self.assertTrue(second.receive_json()["replayed"])
                current = second.receive_json()
                self.assertEqual(replay["phase"], "inactive")
                self.assertTrue(replay["replayed"])
                self.assertEqual(current["phase"], "starting")
                self.assertEqual(current["turn_id"], "t1")
                self.assertNotIn("replayed", current)
                self.assertFalse(bridge.closed)
                self.assertEqual(bridge.cancelled, 0)
                bridge.emit({"type": "turn_completed", "turn_id": "t1", "usage": None})
                self.assertEqual(second.receive_json()["type"], "turn_completed")

    def test_replays_a_detached_fatal_then_allows_a_fresh_runtime(self) -> None:
        first = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        second = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        bridges = [first, second]

        def open_channel(self, channel_id: str) -> FakeBridge:
            return bridges.pop(0)

        with (
            patch.object(
                FakeBridgeProcess,
                "open_channel",
                open_channel,
            ),
            self.client() as client,
        ):
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            first.emit(
                {
                    "type": "fatal",
                    "error": {
                        "type": "RuntimeError",
                        "message": "broken",
                        "details": {},
                    },
                },
                None,
            )
            _wait_for(client, lambda: first.closed)

            runtime = client.get("/api/active-sessions").json()[0]
            self.assertEqual(runtime["event_sequence"], 1)

            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-2",
                        "attach_only": True,
                        "takeover": True,
                        "after_event": runtime["event_sequence"],
                    }
                )
                state = socket.receive_json()
                fatal = socket.receive_json()

            self.assertEqual(state["type"], "runtime_state")
            self.assertEqual(state["event_sequence"], 1)
            self.assertEqual(fatal["type"], "fatal")
            self.assertEqual(fatal["event_sequence"], 2)
            _wait_for(client, lambda: client.get("/api/active-sessions").json() == [])

            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-3"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

        self.assertEqual(first.sent[0]["type"], "open_session")
        self.assertEqual(second.sent[0]["type"], "open_session")

    def test_runs_different_sessions_concurrently(self) -> None:
        """Two pages on different sessions must not block each other."""
        bridges: list[FakeBridge] = []

        def open_channel(self, channel_id: str) -> FakeBridge:
            # Each channel has an independent message queue.
            bridge = FakeBridge(replies={"open_session": [{"type": "session_ready"}]})
            bridges.append(bridge)
            return bridge

        with (
            patch.object(
                FakeBridgeProcess,
                "open_channel",
                open_channel,
            ),
            self.client() as client,
        ):
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "open_session", "session_id": "one", "attachment_id": "page-1"})
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "one"}
                )
                _wait_for(client, lambda: len(bridges[0].sent) == 2)
                self.assertEqual(first.receive_json()["phase"], "starting")

                # Open and run a second session while the first turn is active.
                with client.websocket_connect("/api/session") as second:
                    second.send_json({"type": "open_session", "session_id": "two", "attachment_id": "page-2"})
                    self.assertEqual(
                        second.receive_json()["type"],
                        "session_ready",
                    )
                    second.send_json(
                        {"type": "user_turn", "turn_id": "t2", "text": "two"}
                    )
                    _wait_for(client, lambda: len(bridges[1].sent) == 2)
                    self.assertEqual(second.receive_json()["phase"], "starting")
                    self.assertEqual(
                        {
                            item["session_id"]: item["phase"]
                            for item in client.get("/api/active-sessions").json()
                        },
                        {"one": "starting", "two": "starting"},
                    )
                    for index, socket in enumerate((first, second), start=1):
                        bridges[index - 1].emit({
                            "type": "assistant_delta",
                            "turn_id": f"t{index}",
                            "text": f"reply-{index}",
                            "model_call_index": 0,
                        })
                        self.assertEqual(socket.receive_json()["text"], f"reply-{index}")
                    second.send_json({"type": "cancel", "turn_id": "t2"})
                    _wait_for(client, lambda: bridges[1].closed)

                self.assertFalse(bridges[0].closed)
                self.assertEqual(bridges[0].cancelled, 0)
                bridges[0].emit({
                    "type": "assistant_delta",
                    "turn_id": "t1",
                    "text": "still running",
                    "model_call_index": 0,
                })
                self.assertEqual(first.receive_json()["text"], "still running")
                first.send_json({"type": "cancel", "turn_id": "t1"})
                _wait_for(client, lambda: bridges[0].closed)

        self.assertEqual(
            [bridge.sent[0]["session_id"] for bridge in bridges],
            ["one", "two"],
        )

    def test_attach_only_does_not_start_a_missing_runtime(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "missing",
                        "attachment_id": "page-1",
                        "provider": "second",
                        "attach_only": True,
                    }
                )
                state = socket.receive_json()

        self.assertEqual(state["phase"], "inactive")
        self.assertEqual(bridge.sent, [])

    def test_lists_models_without_requiring_keys(self) -> None:
        with self.client() as client:
            self.assertEqual(
                client.get("/api/models").json(),
                {
                    "default": "first",
                    "models": [
                        {"id": "first", "model": "openai/first"},
                        {"id": "second", "model": "openai/second"},
                    ],
                },
            )

    def test_models_are_read_from_current_configuration(self) -> None:
        with self.client() as client:
            document = json.loads((self.config / "provider_config.json").read_text())
            document["main_agent"]["provider"] = "second"
            document["providers"]["third"] = {"model": "openai/third"}
            (self.config / "provider_config.json").write_text(json.dumps(document), encoding="utf-8")

            response = client.get("/api/models").json()

        self.assertEqual(response["default"], "second")
        self.assertIn({"id": "third", "model": "openai/third"}, response["models"])

    def test_settings_api_redacts_and_updates_provider(self) -> None:
        with self.client() as client:
            before = client.get("/api/settings").json()
            response = client.put("/api/settings/providers/third", json={
                "expected_revision": before["revision"],
                "model": "openai/third",
                "url": "https://example.test/v1",
                "max_context_tokens": 2000,
                "api_key": {"action": "set", "value": "secret"},
                "set_default": True,
            })

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["default_provider"], "third")
        self.assertNotIn("secret", json.dumps(body))
        self.assertIn("RADE_THIRD_API_KEY=secret", (self.config / ".env").read_text())

    def test_memory_api_returns_run_documents(self) -> None:
        memory = MemoryStore(self.config / "runs")
        memory.write(
            "files-run",
            MemoryDocument(
                facts=("This workspace uses FastAPI.",),
                decisions=("Keep Bridge as the runtime boundary.",),
            ),
        )

        with self.client() as client:
            response = client.get("/api/memory")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "runs": [{
                "run_id": "files-run",
                "memory": {
                    "preferences": [],
                    "facts": ["This workspace uses FastAPI."],
                    "decisions": ["Keep Bridge as the runtime boundary."],
                },
            }],
        })

    def test_searches_history_with_bounded_snippets_and_browser_offsets(self) -> None:
        session = Session("searchable")
        session.add_item("user", "before " * 30 + "🙂 中文 NEEDLE " + "after " * 40)
        session.add_item("assistant", "another needle")
        self.store.append_events(session.session_id, session.journal)
        with self.client() as client:
            response = client.get("/api/sessions/search", params={"q": "needle", "limit": 1, "per_session_limit": 2})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["has_more"])
            self.assertEqual(len(data["matches"]), 1)
            match = data["matches"][0]
            self.assertEqual(match["session_id"], session.session_id)
            self.assertEqual(match["item_index"], 0)
            self.assertTrue(match["prefix_omitted"])
            self.assertTrue(match["suffix_omitted"])
            self.assertNotIn("text", match)
            snippet = match["snippet"].encode("utf-16-le")
            self.assertEqual(snippet[match["match_start"] * 2:match["match_end"] * 2].decode("utf-16-le"), "NEEDLE")
            self.assertLessEqual(len(match["snippet"]), 180)
            self.assertEqual(client.get("/api/sessions/search", params={"q": "missing", "limit": 50, "per_session_limit": 2}).json(), {"matches": [], "has_more": False})
            for params in [
                {"q": "needle", "limit": 0, "per_session_limit": 2},
                {"q": "needle", "limit": 50, "per_session_limit": 0},
                {"q": "x" * 201, "limit": 50, "per_session_limit": 2},
                {"q": "needle", "limit": 50},
                {"q": "needle", "per_session_limit": 2},
            ]:
                self.assertEqual(client.get("/api/sessions/search", params=params).status_code, 422)

    def test_search_returns_limited_session_previews_with_more_indicator(self) -> None:
        session = Session("frequent-matches")
        for index in range(10):
            session.add_item("assistant", f"needle {index}")
        self.store.append_events(session.session_id, session.journal)
        with self.client() as client:
            data = client.get("/api/sessions/search", params={"q": "needle", "limit": 50, "per_session_limit": 2}).json()
            expanded = client.get("/api/sessions/search", params={"q": "needle", "limit": 50, "per_session_limit": 5}).json()
            limited = client.get("/api/sessions/search", params={"q": "needle", "limit": 1, "per_session_limit": 5}).json()
        self.assertEqual([match["item_index"] for match in data["matches"]], [0, 1])
        self.assertTrue(all(match["has_more_in_session"] for match in data["matches"]))
        self.assertFalse(data["has_more"])
        self.assertEqual([match["item_index"] for match in expanded["matches"]], [0, 1, 2, 3, 4])
        self.assertEqual(len(limited["matches"]), 1)
        self.assertTrue(limited["has_more"])

    def test_reads_history_written_by_the_tui(self) -> None:
        session = Session("from-tui")
        session.begin_turn("turn-1")
        session.add_item("user", "TUI 对话")
        session.model_completed(1, usage=TokenUsage(1000, 20, 1020, 750, None),
                                has_tool_calls=False)
        session.auxiliary_model_completed(AuxiliaryModelCall(
            task="compression", scope=None, attempt=1, status="completed",
            started_at_utc=datetime.now(timezone.utc), duration_ms=100,
            usage=TokenUsage(500, 10, 510, 450, None),
        ), model="test/main")
        session.add_item("assistant", "你好", provider_data={
            "openai_responses": [{"type": "reasoning", "encrypted_content": "native-state"}],
        })
        session.finish_turn("completed")
        self.store.append_events("from-tui", session.journal)
        with self.client() as client:
            self.assertEqual(
                client.get("/api/sessions").json(),
                [{
                    "session_id": "from-tui",
                    "input_preview": "TUI 对话",
                    "started_at": session.journal[0].timestamp_utc.isoformat().replace("+00:00", "Z"),
                }],
            )
            with patch.object(self.store, "load", wraps=self.store.load) as load:
                data = client.get("/api/sessions/from-tui").json()
                self.assertEqual(load.call_count, 1)

        self.assertEqual(data["items"][0]["content"], "TUI 对话")
        self.assertEqual(data["cache_usage"], {"input_tokens": 1500, "cache_read_tokens": 1200})
        self.assertNotIn("provider_data", data["items"][1])
        self.assertNotIn("native-state", json.dumps(data))
        self.assertEqual(
            self.store.load("from-tui").items[1].provider_data["openai_responses"][0]["encrypted_content"],
            "native-state",
        )

    def test_rejects_a_session_id_that_escapes_the_sessions_directory(
        self,
    ) -> None:
        with self.client() as client:
            response = client.get("/api/sessions/..%5Cescape")

        self.assertEqual(response.status_code, 400)

    def test_lists_and_previews_only_scenes_from_the_startup_directory(self) -> None:
        startup = (self.root / "startup").resolve()
        directory = startup / "input"
        directory.mkdir(parents=True)
        (directory / "scene1_2.json").write_text('{"P":[[0.5]],"v":[1]}', encoding="utf-8")
        (directory / "scene1_1.json").write_text("[" + " " * 12_000 + "]", encoding="utf-8")
        (directory / "arbitrary.json").touch()
        (directory / "scene1_3.json").mkdir()
        (self.root / "input").mkdir()
        (self.root / "input" / "scene2_1.json").touch()

        with patch.object(Path, "cwd", return_value=startup), self.client() as client:
            self.assertEqual(client.get("/api/input-scenes").json(), ["scene1_1.json", "scene1_2.json"])
            preview_url = "/api/input-scenes/preview"
            preview = client.get(preview_url, params={"filename": "scene1_2.json"}).json()
            self.assertEqual(preview, {"text": '{"P":[[0.5]],"v":[1]}', "truncated": False})
            preview = client.get(preview_url, params={"filename": "scene1_1.json"}).json()
            self.assertTrue(preview["truncated"])
            self.assertEqual(len(preview["text"]), 12_000)
            self.assertEqual(client.get(preview_url, params={"filename": "scene2_1.json"}).status_code, 404)
            self.assertEqual(client.get(preview_url, params={"filename": "../input/scene1_2.json"}).status_code, 400)

    def test_run_files_listing_rejects_traversal(self) -> None:
        with self.client() as client:
            listing = client.get("/api/sessions/files-run/files").json()
            self.assertIn(
                {"name": "src", "type": "directory"},
                listing["entries"],
            )
            for path in ("..", "/tmp"):
                self.assertEqual(
                    client.get(
                        "/api/sessions/files-run/files",
                        params={"path": path},
                    ).status_code,
                    400,
                )

    def test_run_files_listing_accepts_cursor(self) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.files / name).write_text(name, encoding="utf-8")

        with self.client() as client:
            first = client.get(
                "/api/sessions/files-run/files", params={"path": "."}
            ).json()
            cursor = next(
                entry["name"]
                for entry in first["entries"]
                if entry["name"] == "a.txt"
            )
            second = client.get(
                "/api/sessions/files-run/files",
                params={"path": ".", "cursor": cursor},
            ).json()

        self.assertNotIn(
            {"name": "a.txt", "type": "file"}, second["entries"]
        )
        self.assertIn(
            {"name": "b.txt", "type": "file"}, second["entries"]
        )

    def test_run_file_listing_is_scoped_to_the_selected_run(self) -> None:
        other = self.store.session_directory("other-run") / "files"
        other.mkdir(parents=True)
        (other / "selected.txt").write_text("selected", encoding="utf-8")

        with self.client() as client:
            listing = client.get("/api/sessions/other-run/files").json()
            self.assertEqual(client.get("/api/sessions/missing/files").status_code, 404)

        self.assertEqual(listing["entries"], [{"name": "selected.txt", "type": "file"}])

    def test_opens_the_selected_run_in_the_native_file_manager(self) -> None:
        selected = self.files
        for platform, command in (("win32", None), ("darwin", ["open", "-a", "Finder"]), ("linux", ["xdg-open"])):
            with self.subTest(platform=platform), self.client() as client:
                with (
                    patch("interfaces.gui.routes.run_files.sys.platform", platform),
                    patch("interfaces.gui.routes.run_files.open_windows_folder") as open_windows,
                    patch("interfaces.gui.routes.run_files.subprocess.Popen") as popen,
                ):
                    response = client.post(
                        "/api/sessions/files-run/files/open",
                    )
                self.assertEqual(response.status_code, 204)
                if command is None:
                    open_windows.assert_called_once_with(selected.resolve())
                    popen.assert_not_called()
                else:
                    popen.assert_called_once_with([*command, str(selected.resolve())])
                    open_windows.assert_not_called()

    def test_open_run_files_rejects_unknown_run(self) -> None:
        with self.client() as client, patch("interfaces.gui.routes.run_files.open_windows_folder") as open_windows:
            self.assertEqual(client.post("/api/sessions/missing/files/open").status_code, 404)
            open_windows.assert_not_called()

    def test_open_run_files_reports_file_manager_errors(self) -> None:
        with (
            self.client() as client,
            patch("interfaces.gui.routes.run_files.sys.platform", "win32"),
            patch("interfaces.gui.routes.run_files.open_windows_folder", side_effect=OSError("cannot open")),
        ):
            response = client.post("/api/sessions/files-run/files/open")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "cannot open")

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_run_files_listing_rejects_external_symlinks(self) -> None:
        (self.files / "outside").symlink_to(
            self.root,
            target_is_directory=True,
        )
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/api/sessions/files-run/files",
                    params={"path": "outside"},
                ).status_code,
                400,
            )

    def test_rejects_foreign_origins_and_hosts(self) -> None:
        with self.client(FakeBridge()) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/api/session",
                    headers={"origin": "https://other.example"},
                ):
                    pass
            self.assertEqual(
                client.get(
                    "/api/sessions",
                    headers={"host": "other.example"},
                ).status_code,
                400,
            )


async def _wait_for_async(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for the runtime")
        await asyncio.sleep(0.01)


def _wait_for(client, condition, timeout: float = 2.0) -> None:
    """Wait for a condition the server's own tasks produce.

    The test client runs the application's event loop only while a
    request is in flight, so polling from the test thread would never
    schedule those tasks.  Each poll therefore issues a request of its
    own: the real server's loop runs continuously, and this keeps the
    loop turning the same way.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        client.get("/api/models")
    raise AssertionError("timed out waiting for the relay")


if __name__ == "__main__":
    unittest.main()
