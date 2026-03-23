from enum import Enum

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class OutputSource(str, Enum):
    """Source of an output line."""

    STDOUT = "stdout"
    STDERR = "stderr"


class OutputLine(FrozenModel):
    """A single line of output with its source (stdout or stderr)."""

    source: OutputSource = Field(description="Whether this line came from stdout or stderr")
    text: str = Field(description="The line content (without trailing newline)")


class CommandResult(FrozenModel):
    """Result of executing a shell command."""

    command: str = Field(description="The shell command that was executed")
    exit_code: int = Field(description="Exit code of the command")
    stdout: str = Field(description="Captured standard output")
    stderr: str = Field(description="Captured standard error")
    output_lines: tuple[OutputLine, ...] = Field(
        default=(),
        description="Interleaved stdout/stderr lines in the order they were produced",
    )
