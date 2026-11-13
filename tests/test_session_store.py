import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path
from threading import Event

from agent_core import (
    FilePart,
    ImagePart,
    JsonlSessionStore,
    HostCommandExecutor,
    PlanSnapshot,
    PlanStep,
    Session,
    SessionInUseError,
    ToolCall,
    TextPart,
)
from agent_core.session_paths import session_log_path
from agent_core import session_store as session_store_module


def persist(store: JsonlSessionStore, session: Session) -> None:
    store.append_events(session.session_id, session.journal)


class JsonlSessionStoreTest(unittest.TestCase):
    def test_independent_store_waits_for_the_session_operation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = JsonlSessionStore(root / "runs")
            first.set_provider("shared", "fixture")
            second = JsonlSessionStore(first.directory)
            attempted, finished = Event(), Event()

            def read_workspace():
                attempted.set()
                try:
                    return second.provider_for("shared")
                finally:
                    finished.set()

            with ThreadPoolExecutor(max_workers=1) as pool:
                with first._locked_session("shared"):
                    result = pool.submit(read_workspace)
                    self.assertTrue(attempted.wait(2))
                    self.assertFalse(finished.wait(0.1))
                self.assertEqual(result.result(timeout=5), "fixture")

    def test_search_streams_only_until_the_extra_session_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            session = Session("streamed")
            session.begin_turn("turn-1")
            for index in range(80):
                session.add_item("assistant", f"needle {index}")
            persist(store, session)
            read_records = []
            read_journal = session_store_module._read_journal_records

            def track_records(file, **kwargs):
                for record in read_journal(file, **kwargs):
                    read_records.append(record)
                    yield record

            with (
                patch.object(store, "load", side_effect=AssertionError("search must not load a Session")),
                patch.object(Session, "apply_event", side_effect=AssertionError("search must not replay events")),
                patch.object(session_store_module, "_read_journal_records", track_records),
            ):
                matches = store.search_messages("needle", limit=50, per_session_limit=2)
            self.assertEqual(len(read_records), 4)  # Turn start and three messages.
            self.assertEqual([match["item_index"] for match in matches], [0, 1])
            self.assertTrue(all(match["has_more_in_session"] for match in matches))

    def test_search_cancellation_stops_inside_a_journal_and_discards_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            session = Session("cancelled")
            for index in range(80):
                session.add_item("user", f"needle {index}")
            persist(store, session)
            stopped = Event()
            decode = session_store_module.message_from_dict

            def cancel_after_message(data):
                message = decode(data)
                stopped.set()
                return message

            with patch.object(session_store_module, "message_from_dict", side_effect=cancel_after_message) as read:
                matches = store.search_messages(
                    "needle", limit=50, per_session_limit=3, should_cancel=stopped.is_set,
                )
            self.assertEqual(matches, [])
            self.assertEqual(read.call_count, 1)
            with patch.object(Path, "iterdir", side_effect=AssertionError("cancelled search must not enumerate")):
                self.assertEqual(store.search_messages(
                    "needle", limit=50, per_session_limit=3, should_cancel=stopped.is_set,
                ), [])

    def test_search_cancellation_stops_session_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            for session_id in ("first", "second"):
                session = Session(session_id)
                session.add_item("user", "needle")
                persist(store, session)
            stopped = Event()
            read_metadata = session_store_module._session_metadata

            def cancel_after_metadata(path):
                metadata = read_metadata(path)
                stopped.set()
                return metadata

            with patch.object(session_store_module, "_session_metadata", side_effect=cancel_after_metadata) as read:
                self.assertEqual(store.search_messages(
                    "needle", limit=50, per_session_limit=3, should_cancel=stopped.is_set,
                ), [])
            self.assertEqual(read.call_count, 1)

    def test_search_and_load_share_incomplete_journal_record_handling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            session = Session("incomplete")
            session.add_item("user", "needle")
            persist(store, session)
            path = session_log_path(store.directory, session.session_id)
            with path.open("a", encoding="utf-8") as file:
                file.write('\n{"event_type":')
            self.assertEqual(len(store.load(session.session_id).items), 1)
            self.assertEqual(len(store.search_messages("needle", limit=50, per_session_limit=3)), 1)
            with path.open("a", encoding="utf-8") as file:
                file.write("\n")
            with self.assertRaises(json.JSONDecodeError):
                store.load(session.session_id)
            with self.assertRaises(json.JSONDecodeError):
                store.search_messages("needle", limit=50, per_session_limit=3)

    def test_search_caps_each_session_without_hiding_other_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            other = Session("other")
            other.add_item("user", "needle in another session")
            persist(store, other)
            frequent = Session("frequent")
            for index in range(80):
                frequent.add_item("assistant", f"needle {index}")
            persist(store, frequent)
            matches = store.search_messages("needle", limit=5, per_session_limit=2)
            previews = [match for match in matches if match["session_id"] == "frequent"]
            self.assertEqual([match["item_index"] for match in previews], [0, 1])
            self.assertTrue(all(match["has_more_in_session"] for match in previews))
            other_matches = [match for match in matches if match["session_id"] == "other"]
            self.assertEqual(len(other_matches), 1)
            self.assertFalse(other_matches[0]["has_more_in_session"])
            expanded = store.search_messages("needle", limit=10, per_session_limit=5)
            self.assertEqual(len([match for match in expanded if match["session_id"] == "frequent"]), 5)

    def test_searches_conversation_text_across_runs_with_original_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            first = Session("first")
            first.add_item("user", "Find Needle in history")
            first.add_item("tool", "needle in tool output", tool_call_id="call-1")
            first.add_item("assistant", None, reasoning="needle in reasoning")
            first.add_item("user", "needle in injected feedback", origin="hook_feedback")
            first.add_item("user", "needle in interaction", origin="user_interaction")
            first.add_item("assistant", "The NEEDLE is here")
            persist(store, first)
            second = Session("second")
            second.add_item("user", (ImagePart(path="needle.png", filename="needle.png"), TextPart("中文 needle 正文")))
            persist(store, second)

            matches = store.search_messages(" needle ", limit=50, per_session_limit=3)
            self.assertEqual(
                {(match["session_id"], match["item_index"]) for match in matches},
                {("first", 0), ("first", 5), ("second", 0)},
            )
            for match in matches:
                self.assertEqual(match["text"][match["match_start"]:match["match_end"]].lower(), "needle")
            self.assertEqual(len(store.search_messages("needle", limit=1, per_session_limit=3)), 1)
            self.assertEqual(store.search_messages("  ", limit=50, per_session_limit=3), [])
            self.assertEqual(store.search_messages("not present", limit=50, per_session_limit=3), [])

    def test_search_preserves_archived_history_and_does_not_recover_or_write_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            session = Session("active")
            with store.open_journal(session.session_id) as sink:
                session.attach_journal_sink(sink)
                session.begin_turn("turn-1")
                session.add_item("user", "a literal [needle].*")
                session.set_archived_summary("compressed", 1)
                path = session_log_path(store.directory, session.session_id)
                before = path.read_bytes()
                with patch.object(Session, "recover", side_effect=AssertionError("search must not recover")):
                    matches = store.search_messages("[needle].*", limit=50, per_session_limit=3)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["item_index"], 0)
                self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows journal ownership")
    def test_second_writer_reports_session_in_use_without_changing_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            session = Session("owned")
            with store.open_journal("owned") as sink:
                session.attach_journal_sink(sink)
                session.add_item("user", "original")
                with self.assertRaises(SessionInUseError) as caught:
                    with JsonlSessionStore(store.directory).open_journal("owned"):
                        self.fail("a second writer acquired the journal")
                self.assertEqual(caught.exception.session_id, "owned")
                self.assertIsInstance(caught.exception.__cause__, BlockingIOError)
                session.add_item("assistant", "owner continues")
            session.attach_journal_sink(None)
            self.assertEqual([event.seq for event in store.load("owned").journal], [1, 2])
            with store.open_journal("owned"):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows file permissions")
    def test_read_only_journal_is_not_reported_as_session_in_use(self) -> None:
        import stat

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "runs")
            with store.open_journal("readonly"):
                pass
            path = session_log_path(store.directory, "readonly")
            path.chmod(stat.S_IREAD)
            try:
                with self.assertRaises(PermissionError):
                    with store.open_journal("readonly"):
                        self.fail("read-only journal opened for writing")
            finally:
                path.chmod(stat.S_IREAD | stat.S_IWRITE)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell journal protection")
    def test_recursive_powershell_cleanup_cannot_erase_audit_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            session = Session("active")
            with store.open_journal(session.session_id) as sink:
                session.attach_journal_sink(sink)
                session.add_item("user", "before cleanup")
                result = HostCommandExecutor(workspace).execute(
                    "Remove-Item -Path './sessions' -Recurse -Force -ErrorAction Stop",
                    timeout_seconds=10,
                )
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                session.add_item("assistant", "cleanup failed")
                self.assertEqual(
                    [item.content for item in store.load("active").items],
                    ["before cleanup", "cleanup failed"],
                )
            session.attach_journal_sink(None)

    def test_open_journal_persists_and_releases_writer_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            session = Session("active")
            with self.assertRaisesRegex(RuntimeError, "turn failed"):
                with store.open_journal(session.session_id) as sink:
                    session.attach_journal_sink(sink)
                    session.add_item("user", "before failure")
                    self.assertEqual(store.load("active").items[0].content, "before failure")
                    raise RuntimeError("turn failed")
            session.attach_journal_sink(None)
            with store.open_journal(session.session_id) as sink:
                session.attach_journal_sink(sink)
                session.add_item("assistant", "after failure")
            session.attach_journal_sink(None)
            self.assertEqual(len(store.load("active").items), 2)
            self.assertTrue(store.delete_session("active"))

    @unittest.skipUnless(os.name == "nt", "Windows file sharing test")
    def test_active_journal_survives_same_user_child_mutations(self) -> None:
        mutations = (
            "target.unlink()",
            "target.rename(target.with_suffix('.moved'))",
            "target.write_text('overwritten')",
            "replacement = target.with_suffix('.replacement'); "
            "replacement.write_text('replacement'); replacement.replace(target)",
            "shutil.rmtree(target.parent)",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                store = JsonlSessionStore(workspace / "runs")
                session = Session("active")
                path = session_log_path(store.directory, session.session_id)
                with store.open_journal(session.session_id) as sink:
                    session.attach_journal_sink(sink)
                    session.add_item("user", "audit before command")
                    before = path.read_bytes()
                    result = subprocess.run(
                        [sys.executable, "-c",
                         "import shutil, sys; from pathlib import Path; "
                         "target = Path(sys.argv[1]); " + mutation, str(path)],
                        capture_output=True, encoding="utf-8", timeout=15,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PermissionError", result.stderr)
                    self.assertEqual(path.read_bytes(), before)
                    session.add_item("assistant", "audit after command")
                    self.assertEqual(len(store.load("active").items), 2)
                session.attach_journal_sink(None)
                self.assertTrue(store.delete_session("active"))

    def test_appends_and_replays_journal_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            session = Session("session-1")
            session.begin_turn("turn-1")
            session.add_item("user", "你好")
            session.add_item("assistant", "你好！")
            session.finish_turn("completed")
            persist(store, session)

            path = session_log_path(store.directory, session.session_id)
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["seq"] for record in records], [1, 2, 3, 4])
            self.assertTrue(all(record["event_id"] for record in records))
            self.assertEqual(
                [item.content for item in store.load("session-1").items],
                ["你好", "你好！"],
            )

    def test_lists_runs_by_first_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            for session_id, text in (("one", "first"), ("two", "second")):
                session = Session(session_id)
                session.begin_turn("t")
                session.add_item("user", text)
                session.finish_turn("completed")
                persist(store, session)

            self.assertEqual(
                {item["input_preview"] for item in store.list_sessions()},
                {"first", "second"},
            )

    def test_lists_session_with_attachments_by_first_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            session = Session("attachment-session")
            session.begin_turn("t")
            session.add_item(
                "user",
                "分析这张图片",
                attachments=(
                    ImagePart(
                        path="attachments/image.png",
                        filename="image.png",
                        size_bytes=12,
                    ),
                ),
            )
            session.finish_turn("completed")
            persist(store, session)

            self.assertEqual(
                store.list_sessions(),
                [{
                    "session_id": "attachment-session", "input_preview": "分析这张图片",
                    "started_at": session.journal[0].timestamp_utc.isoformat().replace("+00:00", "Z"),
                }],
            )

    def test_previews_attachment_only_sessions_from_their_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            single = Session("single-attachment")
            single.begin_turn("t")
            single.add_item(
                "user",
                "",
                attachments=(
                    ImagePart(
                        path="attachments/design.png",
                        filename="design.png",
                        size_bytes=12,
                    ),
                ),
            )
            single.finish_turn("completed")
            persist(store, single)

            multiple = Session("multiple-attachments")
            multiple.begin_turn("t")
            multiple.add_item(
                "user",
                "",
                attachments=(
                    FilePart(
                        path="attachments/brief.pdf",
                        filename="brief.pdf",
                        mime_type="application/pdf",
                        size_bytes=24,
                    ),
                    ImagePart(
                        path="attachments/reference.png",
                        filename="reference.png",
                        size_bytes=12,
                    ),
                ),
            )
            multiple.finish_turn("completed")
            persist(store, multiple)

            previews = {
                item["session_id"]: item["input_preview"]
                for item in store.list_sessions()
            }
            self.assertEqual(previews["single-attachment"], "design.png")
            self.assertEqual(
                previews["multiple-attachments"],
                "brief.pdf",
            )

    def test_lists_sessions_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            for session_id in ("older", "newer"):
                session = Session(session_id)
                session.begin_turn("t")
                session.add_item("user", session_id)
                session.finish_turn("completed")
                persist(store, session)

            older_path = session_log_path(store.directory, "older")
            newer_path = session_log_path(store.directory, "newer")
            os.utime(older_path, (100, 100))
            os.utime(newer_path, (200, 200))

            listed = store.list_sessions()
            self.assertEqual(
                [item["session_id"] for item in listed], ["newer", "older"]
            )

    def test_ignores_session_directories_without_a_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            orphan = store.session_directory("orphan")
            orphan.mkdir(parents=True)

            self.assertEqual(store.list_sessions(), [])
            self.assertFalse(store.has_journal("orphan"))

    def test_stores_provider_in_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = JsonlSessionStore(Path(directory) / "runs")

            store.set_provider("session-1", "second")

            reopened = JsonlSessionStore(store.directory)
            self.assertEqual(reopened.provider_for("session-1"), "second")
            self.assertEqual(reopened.list_sessions(), [])

    def test_replay_restores_tool_and_turn_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            session = Session("session-1")
            session.begin_turn("turn-1")
            call = ToolCall("call-1", "read", {"path": "README.md"})
            session.add_item("assistant", None, tool_calls=(call,))
            session.tool_started(call)
            session.tool_finished(call, "completed")
            session.add_item("tool", "ok", tool_call_id=call.id)
            session.finish_turn("completed")
            persist(store, session)

            loaded = store.load("session-1")
            self.assertEqual(loaded.turns["turn-1"].status, "completed")
            self.assertEqual(loaded.tool_executions["call-1"].status, "completed")

    def test_load_missing_and_reject_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlSessionStore(Path(directory) / "runs")
            self.assertEqual(store.load("missing").items, [])
            with self.assertRaises(ValueError):
                store.load("../escape")

    def test_delete_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "runs")
            session = Session("session-1")
            session.begin_turn("t")
            session.finish_turn("completed")
            persist(store, session)

            session_directory = session_log_path(
                store.directory, "session-1"
            ).parent
            runs_directory = session_directory.parent
            self.assertTrue(store.delete_session("session-1"))
            self.assertFalse(session_directory.exists())
            self.assertTrue(runs_directory.is_dir())
            self.assertFalse(store.delete_session("session-1"))


if __name__ == "__main__":
    unittest.main()
