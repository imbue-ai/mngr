"""Unit tests for mngr_transcript_lib.sh.

Every test runs the library with GNU-only binaries poisoned on PATH (the
``posix_only_path`` fixture), so a reintroduced ``tac`` dependency fails here on
Linux too, not only on macOS.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

_LIB = Path(__file__).parent / "mngr_transcript_lib.sh"


def _write_jsonl(path: Path, uuids: list[str]) -> None:
    path.write_text("".join(json.dumps({"uuid": uuid}) + "\n" for uuid in uuids))


def _reconcile_offset(bash: str, env: dict[str, str], session_file: Path, output_file: Path) -> int:
    script = f"""
        source {shlex.quote(str(_LIB))}
        declare -A _MNGR_TRANSCRIPT_ID_SET
        mngr_transcript_build_id_set {shlex.quote(str(output_file))} uuid
        mngr_transcript_reconcile_offset {shlex.quote(str(session_file))} uuid
    """
    result = subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, f"lib exited {result.returncode}: {result.stderr}"
    assert result.stderr == "", f"lib wrote to stderr (a missing binary?): {result.stderr}"
    return int(result.stdout.strip())


@pytest.mark.parametrize("emitted_count", [0, 1, 2, 3, 4, 5])
def test_reconcile_offset_returns_the_last_already_emitted_line(
    tmp_path: Path, posix_only_path: dict[str, str], bash_with_associative_arrays: str, emitted_count: int
) -> None:
    """The offset is the 1-indexed line of the last session entry already present in the output file."""
    uuids = [f"id-{index}" for index in range(1, 6)]
    session_file = tmp_path / "session.jsonl"
    output_file = tmp_path / "output.jsonl"
    _write_jsonl(session_file, uuids)
    _write_jsonl(output_file, uuids[:emitted_count])

    offset = _reconcile_offset(bash_with_associative_arrays, posix_only_path, session_file, output_file)
    assert offset == emitted_count


def test_reconcile_offset_ignores_an_unterminated_final_line(
    tmp_path: Path, posix_only_path: dict[str, str], bash_with_associative_arrays: str
) -> None:
    """A session file whose last line is still being appended reconciles to the last complete line.

    ``mngr_transcript_build_id_set`` and ``mngr_transcript_emit_lines_range`` both
    ignore a final line with no trailing newline, so the offset must agree.
    """
    session_file = tmp_path / "session.jsonl"
    output_file = tmp_path / "output.jsonl"
    session_file.write_text('{"uuid": "id-1"}\n{"uuid": "id-2"}')
    output_file.write_text('{"uuid": "id-1"}\n{"uuid": "id-2"}')

    offset = _reconcile_offset(bash_with_associative_arrays, posix_only_path, session_file, output_file)
    assert offset == 1


def test_reconcile_offset_returns_the_highest_matching_line_not_the_first(
    tmp_path: Path, posix_only_path: dict[str, str], bash_with_associative_arrays: str
) -> None:
    """With a gap in the emitted set, the offset is the last matching line, not the first."""
    session_file = tmp_path / "session.jsonl"
    output_file = tmp_path / "output.jsonl"
    _write_jsonl(session_file, ["id-1", "id-2", "id-3"])
    _write_jsonl(output_file, ["id-1", "id-3"])

    offset = _reconcile_offset(bash_with_associative_arrays, posix_only_path, session_file, output_file)
    assert offset == 3
