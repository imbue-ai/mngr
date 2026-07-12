class ConcurrencyGroupError(Exception):
    """Base exception for all concurrency group errors."""

    ...


class ProcessError(ConcurrencyGroupError):
    """Raised when a process fails with a non-zero exit code."""

    def __init__(
        self,
        command: tuple[str, ...],
        stdout: str,
        stderr: str,
        returncode: int | None = None,
        is_output_already_logged: bool = False,
        message: str = "Command failed with non-zero exit code",
        display_name: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        # A log-safe label for the command, supplied by the spawning
        # ``RunningProcess``' ``name``. When set it is what appears in the
        # rendered error message (and anywhere else callers render the failure),
        # so secret argument values never surface; ``command`` still holds the
        # real argv for programmatic use. Falls back to the joined command.
        self.display_name = display_name
        self.is_output_already_logged = is_output_already_logged
        self.message = message
        super().__init__(self._format_message())

    @property
    def display_command(self) -> str:
        """The log-safe rendering of the failed command (the ``name`` when supplied)."""
        return self.display_name if self.display_name is not None else " ".join(self.command)

    def _format_message(self) -> str:
        msg = f"{self.message} {self.returncode}. command=`{self.display_command}`"
        if not self.is_output_already_logged:
            output = self.stdout + "\n" + self.stderr
            if len(output) > 8000:
                output = output[:4000] + "\n... OUTPUT TRUNCATED ...\n" + output[-4000:]
            msg += f"\noutput:\n{output}"
        return msg

    def __str__(self) -> str:
        return self._format_message()


class ProcessTimeoutError(ProcessError):
    """Raised when a process times out."""

    def __init__(
        self,
        command: tuple[str, ...],
        stdout: str,
        stderr: str,
        is_output_already_logged: bool = False,
        display_name: str | None = None,
    ) -> None:
        super().__init__(
            command,
            stdout,
            stderr,
            None,
            is_output_already_logged=is_output_already_logged,
            message="Command timed out",
            display_name=display_name,
        )


class ProcessSetupError(ProcessError):
    """Raised when a process fails to start."""

    def __init__(
        self,
        command: tuple[str, ...],
        stdout: str,
        stderr: str,
        is_output_already_logged: bool = False,
        display_name: str | None = None,
    ) -> None:
        super().__init__(
            command,
            stdout,
            stderr,
            None,
            is_output_already_logged=is_output_already_logged,
            message="Command failed to start",
            display_name=display_name,
        )


class EnvironmentStoppedError(ConcurrencyGroupError):
    """Raised when the environment is stopped."""

    ...


class SingleExceptionExpectedError(ConcurrencyGroupError, ValueError):
    """Raised when an exception group does not contain exactly one exception."""

    ...
