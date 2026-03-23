from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.data_types import OutputSource

_TranscriptEntry = tuple[CommandResult, str | None]


class Transcript:
    """Accumulates command results and formats them as an annotated text transcript."""

    def __init__(self) -> None:
        self._entries: list[_TranscriptEntry] = []

    def record(self, result: CommandResult, comment: str | None = None) -> None:
        """Record a command result with an optional comment."""
        self._entries.append((result, comment))

    def format(self) -> str:
        """Format all recorded entries as an annotated transcript.

        Comment lines are prefixed with "# " and appear above the command.
        Uses the interleaved output_lines to preserve the real-time ordering
        of stdout and stderr lines.
        """
        lines: list[str] = []
        for result, comment in self._entries:
            if comment is not None:
                for comment_line in comment.splitlines():
                    lines.append(f"# {comment_line}")

            lines.append(f"$ {result.command}")

            for output_line in result.output_lines:
                if output_line.source == OutputSource.STDOUT:
                    lines.append(f"  {output_line.text}")
                else:
                    lines.append(f"! {output_line.text}")

            lines.append(f"? {result.exit_code}")
        return "\n".join(lines) + "\n" if lines else ""
