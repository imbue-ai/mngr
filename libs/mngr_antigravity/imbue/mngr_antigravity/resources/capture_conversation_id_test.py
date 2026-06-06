"""Tests for capture_conversation_id.sh, the PreInvocation capture hook.

The script reads an agy hook payload (JSON) on stdin and appends the
`conversationId` to the per-agent conversation-ids file when it differs from
the last recorded id. Its output drives both conversation resume
(assemble_command) and transcript scoping (stream_transcript.sh), so the
tests pin: extraction, append-on-change dedup, switch-back ordering,
no-clobber on a missing id, stdout silence (agy treats PreInvocation stdout
as injected steps), and tolerance of a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent / "capture_conversation_id.sh"

_CONV_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONV_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _payload(conv_id: str) -> str:
    """A minimal agy hook payload shaped like the real one (verified live)."""
    brain = f"/home/u/.gemini/antigravity-cli/brain/{conv_id}"
    return (
        f'{{"artifactDirectoryPath":"{brain}","conversationId":"{conv_id}",'
        f'"invocationNum":0,"transcriptPath":"{brain}/.system_generated/logs/transcript.jsonl",'
        f'"workspacePaths":["/tmp/ws"]}}'
    )


def _run(state_dir: Path, payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=payload,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def _ids_file(state_dir: Path) -> Path:
    return state_dir / "antigravity_conversation_ids"


def _recorded(state_dir: Path) -> list[str]:
    path = _ids_file(state_dir)
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line]


def test_extracts_and_records_conversation_id(tmp_path: Path) -> None:
    result = _run(tmp_path, _payload(_CONV_A))
    assert _recorded(tmp_path) == [_CONV_A]
    # PreInvocation handlers must stay silent: non-empty stdout = injected steps.
    assert result.stdout == ""


def test_does_not_reappend_when_id_unchanged(tmp_path: Path) -> None:
    """Repeated invocations on the same conversation append the id only once."""
    _run(tmp_path, _payload(_CONV_A))
    _run(tmp_path, _payload(_CONV_A))
    _run(tmp_path, _payload(_CONV_A))
    assert _recorded(tmp_path) == [_CONV_A]


def test_records_each_distinct_conversation_in_order(tmp_path: Path) -> None:
    """A new conversation (e.g. after /clear) is appended after the previous one."""
    _run(tmp_path, _payload(_CONV_A))
    _run(tmp_path, _payload(_CONV_B))
    assert _recorded(tmp_path) == [_CONV_A, _CONV_B]


def test_switch_back_does_not_reappend(tmp_path: Path) -> None:
    """Revisiting an earlier conversation does not re-append it.

    The file is a set for transcript scoping (`sort -u`), so order and recency
    are irrelevant -- resume tracks the main conversation in root_conversation,
    not here. Recording each distinct id once keeps the file small.
    """
    _run(tmp_path, _payload(_CONV_A))
    _run(tmp_path, _payload(_CONV_B))
    _run(tmp_path, _payload(_CONV_A))
    assert _recorded(tmp_path) == [_CONV_A, _CONV_B]
    assert set(_recorded(tmp_path)) == {_CONV_A, _CONV_B}


def test_payload_without_conversation_id_does_not_clobber(tmp_path: Path) -> None:
    """A payload missing conversationId records nothing and preserves prior ids."""
    _run(tmp_path, _payload(_CONV_A))
    _run(tmp_path, '{"invocationNum":1,"workspacePaths":["/tmp/ws"]}')
    assert _recorded(tmp_path) == [_CONV_A]


def test_garbage_stdin_is_tolerated(tmp_path: Path) -> None:
    """Non-JSON stdin extracts no id and exits cleanly (never disrupts agy)."""
    result = _run(tmp_path, "not json at all\n")
    assert _recorded(tmp_path) == []
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    """An unset MNGR_AGENT_STATE_DIR is a wiring error: fail loudly, not silently.

    mngr always sets it, and agy invokes this script via a path that embeds it,
    so reaching the body unset means misconfiguration -- we surface it (stderr,
    non-zero exit) rather than swallow it or write to the filesystem root. We
    still keep stdout empty so a PreInvocation hook never injects steps.
    """
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=_payload(_CONV_A),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
