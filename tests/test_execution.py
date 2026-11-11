import os
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    TurnControl,
    CommandCancelled,
    HostCommandExecutor,
)
from agent_core.execution.process import (
    MAX_COMMAND_OUTPUT_CHARS,
    CommandOutputSpool,
)


def _python_script_command(working_directory: Path, script: str) -> str:
    """Write a helper script into the workspace and run it by name.

    Running a script file keeps the command line free of the quoting that
    differs between shells.
    """
    (working_directory / "command.py").write_text(script, encoding="utf-8")
    if os.name == "nt":
        return f'& "{sys.executable}" command.py'
    return f'"{sys.executable}" command.py'


class HostCommandExecutorTest(unittest.TestCase):
    # Shell syntax follows the platform launcher selected by the executor.
    def test_uses_the_injected_shell_launcher(self) -> None:
        calls = []

        def launcher(command: str, working_directory: Path) -> list[str]:
            calls.append((command, working_directory))
            return [sys.executable, "-c", "print('launcher')"]

        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            result = HostCommandExecutor(
                working_directory,
                shell_launcher=launcher,
            ).execute("ignored")

        self.assertEqual(result.stdout.strip(), "launcher")
        self.assertEqual(calls, [("ignored", working_directory)])

    def test_executes_command_in_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)

            command = (
                "[Console]::Write('hello'); "
                "[Console]::Error.Write('warning'); "
                "Set-Content -NoNewline -Path command-output.txt -Value marker"
                if os.name == "nt"
                else "printf 'hello'; printf 'warning' >&2; "
                "printf 'marker' > command-output.txt"
            )
            result = executor.execute(command)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "hello")
            self.assertEqual(result.stderr, "warning")
            self.assertFalse(result.timed_out)
            self.assertEqual(result.timeout_seconds, 60)
            self.assertEqual(
                (working_directory / "command-output.txt").read_text(
                    encoding="utf-8"
                ),
                "marker",
            )

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_host_powershell_output_is_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = HostCommandExecutor(Path(directory)).execute(
                "Get-Process | Select-Object -First 1 | Format-Table"
            )

        self.assertNotIn("\x1b[", result.stdout)
        self.assertNotIn("\x1b[", result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_powershell_stops_when_working_directory_initialization_fails(self) -> None:
        from agent_core.execution.process import _windows_powershell_argv

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = _windows_powershell_argv(
                "Set-Content -LiteralPath 'command-reached' -Value yes",
                root / "missing-workspace",
            )
            result = subprocess.run(argv, cwd=root, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "command-reached").exists())
            self.assertTrue(result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_powershell_working_directory_is_a_literal_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace's [literal]"
            root.mkdir()
            result = HostCommandExecutor(root).execute("Set-Content -NoNewline -LiteralPath result.txt -Value ok")
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "ok")

    @unittest.skipUnless(os.name == "nt", "Windows shell test")
    def test_powershell_stops_before_cleanup_after_setup_errors(self) -> None:
        # Only a marker follows the failing setup; never replay the incident's
        # destructive command against the actual PowerShell HOME variable.
        commands = (
            "$home = Join-Path $PWD 'task-temp'",
            "Get-Item -Path './missing-setup-file'",
            "$cleanupTarget = $undefinedCleanupTarget.FullName",
            f'& "{sys.executable}" -c "import sys; sys.exit(9)"',
        )
        for setup in commands:
            with self.subTest(setup=setup), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = HostCommandExecutor(root).execute(
                    setup + "\nSet-Content -Path 'cleanup-reached' -Value yes"
                )
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                self.assertFalse((root / "cleanup-reached").exists())
                self.assertTrue(result.stderr)

    def test_returns_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = HostCommandExecutor(Path(directory))

            command = (
                "[Console]::Error.Write('failed'); exit 7"
                if os.name == "nt"
                else "printf 'failed' >&2; exit 7"
            )
            result = executor.execute(command)

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "failed")

    def test_times_out_and_kills_the_command_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(3)\n"
                "Path('child-output.txt').write_text('alive')\n",
            )

            result = executor.execute(command, timeout_seconds=1)
            time.sleep(3.5)

            self.assertTrue(result.timed_out)
            self.assertEqual(result.timeout_seconds, 1)
            self.assertFalse(
                (working_directory / "child-output.txt").exists()
            )

    def test_cancellation_kills_the_command_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
            cancellation = TurnControl()
            command = _python_script_command(
                working_directory,
                "import time\n"
                "from pathlib import Path\n"
                "Path('child-ready').touch()\n"
                "time.sleep(3)\n"
                "Path('child-output.txt').write_text('alive')\n",
            )
            errors = []

            def execute() -> None:
                try:
                    executor.execute(command, cancellation=cancellation)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=execute)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not (working_directory / "child-ready").exists() and thread.is_alive():
                    self.assertLess(time.monotonic(), deadline, "child process did not start")
                    time.sleep(0.01)
                self.assertTrue((working_directory / "child-ready").exists())
            finally:
                cancellation.cancel()
                thread.join(5)
            time.sleep(3.2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], CommandCancelled)
            self.assertFalse(
                (working_directory / "child-output.txt").exists()
            )

    def test_limits_each_output_stream_and_keeps_both_ends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.write('START-OUT' + 'o' * 70000 + 'END-OUT')\n"
                "sys.stderr.write('START-ERR' + 'e' * 70000 + 'END-ERR')\n",
            )

            result = executor.execute(command)

            self.assertLessEqual(len(result.stdout), MAX_COMMAND_OUTPUT_CHARS)
            self.assertLessEqual(len(result.stderr), MAX_COMMAND_OUTPUT_CHARS)
            self.assertTrue(result.stdout.startswith("START-OUT"))
            self.assertTrue(result.stdout.endswith("END-OUT"))
            self.assertTrue(result.stderr.startswith("START-ERR"))
            self.assertTrue(result.stderr.endswith("END-ERR"))
            self.assertRegex(
                result.stdout,
                r"\[truncated \d+ characters\]",
            )
            self.assertRegex(
                result.stderr,
                r"\[truncated \d+ characters\]",
            )
            if result.stdout_spool is not None:
                result.stdout_spool.cleanup()
            if result.stderr_spool is not None:
                result.stderr_spool.cleanup()

    def test_retains_oversized_streams_in_spools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.write('START-' + 'o' * 70000 + '-END')\n",
            )

            result = executor.execute(command)

            self.assertIsNotNone(result.stdout_spool)
            assert result.stdout_spool is not None
            self.assertEqual(
                result.stdout_spool.path.read_text(encoding="utf-8"),
                "START-" + "o" * 70000 + "-END",
            )
            result.stdout_spool.cleanup()
            self.assertFalse(result.stdout_spool.path.exists())

    def test_spool_cleanup_ignores_windows_sharing_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locked.txt"
            path.write_text("data", encoding="utf-8")
            spool = CommandOutputSpool(path, 4, "utf-8")
            original_unlink = Path.unlink

            def locked_unlink(self, missing_ok=False):
                if self == path:
                    raise PermissionError(13, "sharing violation")
                return original_unlink(self, missing_ok=missing_ok)

            from unittest.mock import patch

            with patch.object(Path, "unlink", locked_unlink):
                spool.cleanup()

            self.assertTrue(path.exists())

    def test_decodes_output_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            executor = HostCommandExecutor(working_directory)
            command = _python_script_command(
                working_directory,
                "import sys\n"
                "sys.stdout.buffer.write(b'\\xd6\\xd0\\xce\\xc4')\n"
                "sys.stderr.buffer.write(b'\\xd2\\xbb')\n",
            )

            result = executor.execute(command)

            self.assertEqual(result.exit_code, 0)
            self.assertNotEqual(result.stdout, "")
            self.assertNotEqual(result.stderr, "")
