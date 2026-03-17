from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.data_types import OutputSource


class Transcript:
    """Accumulates command results and formats them as an annotated text transcript."""

    def __init__(self) -> None:
        self._entries: list[CommandResult] = []

    def record(self, result: CommandResult) -> None:
        """Record a command result."""
        self._entries.append(result)

    def format(self) -> str:
        """Format all recorded entries as an annotated transcript.

        Uses the interleaved output_lines to preserve the real-time ordering
        of stdout and stderr lines.
        """
        lines: list[str] = []
        for entry in self._entries:
            lines.append(f"$ {entry.command}")

            for output_line in entry.output_lines:
                if output_line.source == OutputSource.STDOUT:
                    lines.append(f"  {output_line.text}")
                else:
                    lines.append(f"! {output_line.text}")

            lines.append(f"? {entry.exit_code}")
        return "\n".join(lines) + "\n" if lines else ""
