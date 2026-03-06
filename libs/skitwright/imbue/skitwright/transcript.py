from imbue.skitwright.data_types import CommandResult


class Transcript:
    """Accumulates command results and formats them as an annotated text transcript."""

    def __init__(self) -> None:
        self._entries: list[CommandResult] = []

    def record(self, result: CommandResult) -> None:
        """Record a command result."""
        self._entries.append(result)

    def format(self) -> str:
        """Format all recorded entries as an annotated transcript."""
        lines: list[str] = []
        for entry in self._entries:
            lines.append(f"$ {entry.command}")

            for stdout_line in entry.stdout.splitlines():
                lines.append(f"  {stdout_line}")

            for stderr_line in entry.stderr.splitlines():
                lines.append(f"! {stderr_line}")

            lines.append(f"? {entry.exit_code}")
        return "\n".join(lines) + "\n" if lines else ""
