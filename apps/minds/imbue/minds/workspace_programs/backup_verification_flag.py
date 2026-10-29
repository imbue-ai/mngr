"""Read or set the workspace's backup-verification flag in ``data/system/backup.toml``.

The flag is a fact about one workspace that every computer should agree on (user-profile-state
spec, section 7.4), so it lives in the workspace's own backup config -- a key the backup
service tolerates and ignores -- rather than in each Imbue Studio install. The app reads it on
every backup check (``backup_check`` answers it on the way) and writes it through this program
when the user toggles verification.

The write is a careful text edit rather than a TOML re-render: ``backup.toml`` is a
user-authored file, so every other line (settings, comments, tables) is kept byte for byte.
Only a top-level ``verification_disabled = ...`` line (one before the first table header) is
replaced; absent one, the key is inserted at the top of the file.
"""

import os
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from typing import Final

from imbue.minds.workspace_programs.backup_protocol import VERIFICATION_FLAG_RESULT_MARKER
from imbue.minds.workspace_programs.program_support import argv_value
from imbue.minds.workspace_programs.program_support import emit_marker_json

BACKUP_TOML_PATH: Final[str] = "data/system/backup.toml"
VERIFICATION_DISABLED_KEY: Final[str] = "verification_disabled"

# ``--set``'s values: the flag states a set writes.
SET_DISABLED_VALUE: Final[str] = "disabled"
SET_ENABLED_VALUE: Final[str] = "enabled"


def read_verification_disabled(backup_toml_path: str = BACKUP_TOML_PATH) -> bool:
    """Whether the workspace's backup config turns the app's verification off (absent or unreadable reads enabled)."""
    try:
        with open(backup_toml_path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return False
    return raw.get(VERIFICATION_DISABLED_KEY) is True


def _is_flag_line(line: str) -> bool:
    return line.split("=", 1)[0].strip() == VERIFICATION_DISABLED_KEY


def render_backup_toml_with_flag(existing_text: str, is_disabled: bool) -> str:
    """``backup.toml``'s text with the flag line set, every other line kept as it is.

    Only the root table's own flag line (one before the first ``[table]`` header) is replaced;
    the new line goes at the top so it always lands in the root table.
    """
    lines = existing_text.splitlines()
    kept: list[str] = []
    is_in_root_table = True
    for line in lines:
        if line.lstrip().startswith("["):
            is_in_root_table = False
        if is_in_root_table and _is_flag_line(line):
            continue
        kept.append(line)
    flag_line = f"{VERIFICATION_DISABLED_KEY} = {'true' if is_disabled else 'false'}"
    body = "\n".join([flag_line, *kept])
    return body if body.endswith("\n") else body + "\n"


def write_verification_disabled(is_disabled: bool, backup_toml_path: str = BACKUP_TOML_PATH) -> None:
    try:
        with open(backup_toml_path, encoding="utf-8") as handle:
            existing_text = handle.read()
    except FileNotFoundError:
        existing_text = ""
    new_text = render_backup_toml_with_flag(existing_text, is_disabled)
    directory = os.path.dirname(backup_toml_path) or "."
    os.makedirs(directory, exist_ok=True)
    # A unique scratch per write (never a fixed `<path>.tmp`): two computers toggling the
    # same workspace's flag concurrently must not share a tmp path.
    handle, scratch = tempfile.mkstemp(dir=directory, prefix=".backup.toml.", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(new_text)
    os.replace(scratch, backup_toml_path)


def main(argv: Sequence[str]) -> None:
    requested = argv_value(argv, "--set", "")
    is_set_applied = False
    if requested in (SET_DISABLED_VALUE, SET_ENABLED_VALUE):
        write_verification_disabled(requested == SET_DISABLED_VALUE)
        is_set_applied = True
    emit_marker_json(
        VERIFICATION_FLAG_RESULT_MARKER,
        {
            "schema": 1,
            VERIFICATION_DISABLED_KEY: read_verification_disabled(),
            "is_set_applied": is_set_applied,
        },
    )


if __name__ == "__main__":
    main(sys.argv[1:])
