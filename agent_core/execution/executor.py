import os
from pathlib import Path
from typing import Protocol

from . import process as command_process
from .process import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    CancellationSignal,
    CommandExecutionResult,
    ShellLauncher,
)


class CommandExecutor(Protocol):
    def execute(
        self,
        command: str | tuple[str, ...],
        timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
        *,
        stdin: bytes | None = None,
    ) -> CommandExecutionResult:
        raise NotImplementedError


class HostCommandExecutor:
    """Run commands with the current user's permissions in the configured directory."""

    def __init__(
        self,
        working_directory: Path,
        shell_launcher: ShellLauncher | None = None,
    ) -> None:
        self._working_directory = working_directory
        self._shell_launcher = (
            shell_launcher or command_process.platform_shell_launcher
        )

    def execute(
        self,
        command: str | tuple[str, ...],
        timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cancellation: CancellationSignal | None = None,
        *,
        stdin: bytes | None = None,
    ) -> CommandExecutionResult:
        environment = None
        if os.name == "nt":
            environment = command_process._windows_shell_environment(
                os.environ.copy()
            )
        argv = (
            list(command)
            if isinstance(command, tuple)
            else self._shell_launcher(command, self._working_directory)
        )
        return command_process._execute_process(
            argv,
            command=command_process.command_display(command),
            working_directory=self._working_directory,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            environment=environment,
            stdin=stdin,
        )
