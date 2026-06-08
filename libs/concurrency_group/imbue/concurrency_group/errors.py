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
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        self.is_output_already_logged = is_output_already_logged
        self.message = message
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        command_str = " ".join(self.command)
        msg = f"{self.message} {self.returncode}. command=`{command_str}`"
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
    ) -> None:
        super().__init__(
            command,
            stdout,
            stderr,
            None,
            is_output_already_logged=is_output_already_logged,
            message="Command timed out",
        )


class ProcessSetupError(ProcessError):
    """Raised when a process fails to start."""

    def __init__(
        self,
        command: tuple[str, ...],
        stdout: str,
        stderr: str,
        is_output_already_logged: bool = False,
    ) -> None:
        super().__init__(
            command,
            stdout,
            stderr,
            None,
            is_output_already_logged=is_output_already_logged,
            message="Command failed to start",
        )


class EnvironmentStoppedError(ConcurrencyGroupError):
    """Raised when the environment is stopped."""

    ...
