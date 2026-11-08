import asyncio
import builtins
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - exercised without [server]
    TestClient = None

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore
from interfaces import application_host
from interfaces.application_host import ApplicationHost, application_lock


class ApplicationHostCommandTest(unittest.TestCase):
    def test_server_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(application_host, "run_application_host") as launch:
                application_host.main(["--workspace", str(root), "--config-directory", str(root)])
                launch.assert_called_once_with(root, root)

    def test_error_interrupt_and_signal_restoration(self):
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(application_host, "run_application_host", side_effect=KeyboardInterrupt):
            application_host.main([])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        with patch.object(application_host, "run_application_host", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(SystemExit, "Failed to start RADE server: failed"):
                application_host.main([])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_initializes_configuration_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            with (
                patch.object(ApplicationHost, "serve", new_callable=AsyncMock) as serve,
                patch("builtins.print"),
            ):
                application_host.run_application_host(root, config)
            serve.assert_awaited_once_with()
            self.assertTrue((config / "provider_config.json").is_file())
            with application_lock(config):
                pass

    def test_duplicate_is_rejected_before_initialization_or_host_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                application_lock(root),
                patch.object(application_host, "initialize_config_directory") as initialize,
                patch.object(application_host, "ApplicationHost") as host,
            ):
                with self.assertRaises(SystemExit) as failure:
                    application_host.main([
                        "--workspace", str(root), "--config-directory", str(root),
                    ])
                self.assertEqual(
                    str(failure.exception),
                    f"Failed to start RADE server: already running for config directory: {root.resolve()}",
                )
                initialize.assert_not_called()
                host.assert_not_called()

class ApplicationLockTest(unittest.TestCase):
    def test_config_directories_are_independent_and_lock_is_released_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "stop"):
                with application_lock(root / "a"), application_lock(root / "b"):
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        with application_lock(root / "a"):
                            self.fail("duplicate acquired the lock")
                    raise ValueError("stop")
            with application_lock(root / "a"):
                pass

    def test_cross_process_lock_and_release_after_process_exit(self):
        code = """
import sys, time
from pathlib import Path
from interfaces.application_host import application_lock
with application_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code, str(root)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), b"locked")
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with application_lock(root):
                        self.fail("another process acquired the lock")
            finally:
                process.kill()
                process.communicate(timeout=10)
            self.assertTrue((root / "server.lock").is_file())
            with application_lock(root):
                pass

class ApplicationHostLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = Mock(start=AsyncMock(), close_all=AsyncMock())
        self.enterContext(patch.object(application_host, "ActiveSessionRegistry", return_value=self.registry))
        self.host = ApplicationHost(
            Workspace(self.root), JsonlSessionStore(self.root / "sessions"), SettingsStore(self.root),
        )

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_unrelated_http_import_errors_are_not_relabelled(self):
        original_import = builtins.__import__
        for error in (
            ImportError("cannot import name 'serve_http'"),
            ModuleNotFoundError("No module named 'interfaces.gui.missing'", name="interfaces.gui.missing"),
        ):
            with self.subTest(error=error):
                def import_http(name, *args, **kwargs):
                    if name == "interfaces.gui.server":
                        raise error
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=import_http):
                    with self.assertRaises(type(error)) as failure:
                        await self.host._serve_http()
                self.assertIs(failure.exception, error)

    async def test_http_shutdown_precedes_bridge_shutdown(self):
        http_ready = asyncio.Event()
        order = []

        async def serve_http():
            http_ready.set()
            try:
                await asyncio.Future()
            finally:
                order.append("http")

        self.registry.close_all.side_effect = lambda: order.append("bridge")
        with patch.object(self.host, "_serve_http", side_effect=serve_http):
            task = asyncio.create_task(self.host.serve())
            self.addAsyncCleanup(self.stop, task)
            await asyncio.wait_for(http_ready.wait(), 2)
            self.registry.start.assert_awaited_once()
            await self.stop(task)

        self.registry.close_all.assert_awaited_once()
        self.assertEqual(order, ["http", "bridge"])

    async def test_bridge_startup_failure_releases_resources(self):
        self.registry.start.side_effect = RuntimeError("bridge unavailable")
        with self.assertRaisesRegex(RuntimeError, "bridge unavailable"):
            await self.host.serve()
        self.registry.close_all.assert_awaited_once()


@unittest.skipIf(TestClient is None, "Install the server extra to test HTTP")
class HttpAdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from interfaces.gui import server

        self.server = server
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = Mock(start=AsyncMock(), close_all=AsyncMock())
        self.store = JsonlSessionStore(self.root / "sessions")
        self.workspace = Workspace(self.root)
        self.settings = SettingsStore(self.root)

    async def test_fastapi_lifespan_does_not_own_shared_resources(self):
        app = self.server.create_app(self.workspace, self.store, self.settings, self.registry)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.assertEqual(client.get("/api/sessions").json(), [])
        self.registry.start.assert_not_awaited()
        self.registry.close_all.assert_not_awaited()

    async def test_http_cancellation_gracefully_closes_listener_without_closing_sessions(self):
        import httpx

        instances = []
        server_type = self.server.HostedServer

        def create_server(config):
            instance = server_type(config)
            instances.append(instance)
            return instance

        with (
            patch.object(self.server, "PORT", 0),
            patch.object(self.server, "HostedServer", side_effect=create_server),
            patch("builtins.print"),
        ):
            task = asyncio.create_task(self.server.serve_http(
                self.workspace, self.store, self.settings, self.registry,
            ))
            try:
                async def wait_started():
                    while not instances or not instances[0].started:
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_started(), 5)
                port = instances[0].servers[0].sockets[0].getsockname()[1]
                response = await asyncio.to_thread(
                    httpx.get, f"http://127.0.0.1:{port}/api/sessions", trust_env=False,
                )
                self.assertEqual(response.status_code, 200)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
        self.registry.close_all.assert_not_awaited()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))

    def test_reports_missing_websocket_backend(self):
        with patch.object(self.server.importlib, "import_module", side_effect=ModuleNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "uv sync --extra server"):
                self.server.require_websocket_backend()
