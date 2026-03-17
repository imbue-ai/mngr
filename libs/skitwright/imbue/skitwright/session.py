import os
from pathlib import Path

from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.runner import run_command
from imbue.skitwright.transcript import Transcript

_DEFAULT_TIMEOUT: float = 30.0


class Session:
    """End-to-end test session that runs commands and records a transcript."""

    def __init__(
        self,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ.copy()
        self._cwd = cwd if cwd is not None else Path.cwd()
        self._transcript = Transcript()

    def run(self, command: str, timeout: float = _DEFAULT_TIMEOUT) -> CommandResult:
        """Run a shell command and return the result."""
        result = run_command(
            command=command,
            env=self._env,
            cwd=self._cwd,
            timeout=timeout,
        )
        self._transcript.record(result)
        return result

    @property
    def transcript(self) -> str:
        """The accumulated transcript of all commands run in this session."""
        return self._transcript.format()
