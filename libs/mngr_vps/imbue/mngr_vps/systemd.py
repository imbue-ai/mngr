from collections.abc import Mapping
from collections.abc import Sequence

from imbue.imbue_common.pure import pure

# An ordered list of ``Key``/``Value`` pairs for one systemd unit section. A list
# of pairs (rather than a dict) so a key may repeat -- systemd allows e.g. several
# ``Environment=`` or ``ExecStart=`` lines in one section.
SystemdSection = Sequence[tuple[str, str]]


@pure
def render_systemd_unit(sections: Mapping[str, SystemdSection]) -> str:
    """Render a systemd unit file from ordered sections of ``Key=Value`` entries.

    Each section becomes a ``[Name]`` header followed by its entry lines, in
    iteration order. Centralizes the unit-file format so callers never hand-assemble
    the ``[Section]\\nKey=Value\\n`` text.

    Keep shell commands out of the unit: an ``ExecStart=/bin/sh -c '...'`` embeds a
    command inside two layers of quoting (systemd's own ``ExecStart`` tokenizer, then
    the shell), which is fragile for any interpolated path or URI. Install the command
    as a script and point ``ExecStart`` at the script path instead.
    """
    lines: list[str] = []
    for section_name, entries in sections.items():
        lines.append(f"[{section_name}]")
        lines.extend(f"{key}={value}" for key, value in entries)
    return "\n".join(lines) + "\n"
