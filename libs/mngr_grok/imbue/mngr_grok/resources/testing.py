import importlib.resources
import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from imbue.mngr import resources as mngr_resources
from imbue.mngr_grok.grok_sessions import GROK_SESSIONS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import ROOTS_RELATIVE_PATH
from imbue.mngr_grok.plugin import COMMON_TRANSCRIPT_SCRIPT_NAMES
from imbue.mngr_grok.plugin import RAW_TRANSCRIPT_SCRIPT_NAMES

_RESOURCES_DIR: Final[Path] = Path(__file__).parent
_COMMON_TRANSCRIPT_LOCK_LIBRARY_NAME: Final[str] = "mngr_common_transcript_lib.sh"
_MNGR_LOG_LIBRARY_NAME: Final[str] = "mngr_log.sh"

TEST_FIXTURES_DIR: Final[Path] = _RESOURCES_DIR / "test_fixtures"
SESSION_FIXTURES_DIR: Final[Path] = TEST_FIXTURES_DIR / "sessions"
HOOK_FIXTURES_DIR: Final[Path] = TEST_FIXTURES_DIR / "hooks"
CONFIG_FIXTURES_DIR: Final[Path] = TEST_FIXTURES_DIR / "config"
# The session bucket that stage_fixture_sessions places every captured session in.
FIXTURE_SESSION_BUCKET: Final[str] = "%2Fwork"
TRANSCRIPT_SCRIPT_NAMES: Final[tuple[str, ...]] = RAW_TRANSCRIPT_SCRIPT_NAMES + COMMON_TRANSCRIPT_SCRIPT_NAMES
_COMMON_TRANSCRIPT_RELATIVE_PATH: Final[Path] = Path("events", "grok", "common_transcript", "events.jsonl")
_AGENT_SCRIPT_TIMEOUT_SECONDS: Final[float] = 60.0
_TURN_COMPLETED_UPDATE_MARKER: Final[bytes] = b'"sessionUpdate":"turn_completed"'


def install_transcript_scripts(state_dir: Path, script_names: Sequence[str], mngr_log_sh: str | None = None) -> None:
    """Copy mngr's common-transcript lock library and the named grok scripts into ``state_dir/commands/``.

    ``mngr_log_sh``, when given, is written as the ``commands/mngr_log.sh`` the scripts source.
    """
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    lock_library = importlib.resources.files(mngr_resources).joinpath(_COMMON_TRANSCRIPT_LOCK_LIBRARY_NAME)
    (commands_dir / _COMMON_TRANSCRIPT_LOCK_LIBRARY_NAME).write_text(lock_library.read_text())
    for script_name in script_names:
        shutil.copy(_RESOURCES_DIR / script_name, commands_dir / script_name)
    if mngr_log_sh is not None:
        (commands_dir / _MNGR_LOG_LIBRARY_NAME).write_text(mngr_log_sh)


def run_agent_command_script(
    state_dir: Path, script_name: str, *arguments: str, extra_env: Mapping[str, str] | None = None
) -> None:
    """Run ``state_dir/commands/<script_name>`` under bash for that agent state dir; it must succeed and print nothing."""
    result = subprocess.run(
        ["bash", str(state_dir / "commands" / script_name), *arguments],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir), **(extra_env or {})},
        capture_output=True,
        text=True,
        timeout=_AGENT_SCRIPT_TIMEOUT_SECONDS,
    )
    outcome = (result.returncode, result.stdout, result.stderr)
    assert outcome == (0, "", ""), outcome


def stage_fixture_sessions(state_dir: Path, scenario: str) -> None:
    """Copy a captured scenario's sessions into the agent's grok home and its `roots` into `plugin/grok`."""
    bucket_dir = state_dir / GROK_SESSIONS_RELATIVE_PATH / FIXTURE_SESSION_BUCKET
    scenario_dir = SESSION_FIXTURES_DIR / scenario
    for session_dir in sorted(path for path in scenario_dir.iterdir() if path.is_dir()):
        shutil.copytree(session_dir, bucket_dir / session_dir.name)
    shutil.copy(scenario_dir / "roots", state_dir / ROOTS_RELATIVE_PATH)


def cut_before_first_turn_completed(session_bytes: bytes) -> bytes:
    """An `updates.jsonl`'s bytes up to the start of the line holding its first `turn_completed` record."""
    marker_idx = session_bytes.index(_TURN_COMPLETED_UPDATE_MARKER)
    return session_bytes[: session_bytes.rfind(b"\n", 0, marker_idx) + 1]


def tag_native_lines(
    session_id: str, parent_session_id: str | None, native_lines: Sequence[bytes]
) -> list[dict[str, Any]]:
    """The raw-transcript entries the streamer writes for consecutive `updates.jsonl` lines, each given with its newline."""
    entries: list[dict[str, Any]] = []
    offset = 0
    for line in native_lines:
        entries.append(
            {
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "offset": offset,
                "record": json.loads(line),
            }
        )
        offset += len(line)
    return entries


def render_jsonl(records: Iterable[Mapping[str, Any]]) -> str:
    """The records as JSONL text, one line each."""
    return "".join(json.dumps(record) + "\n" for record in records)


def read_files_under(root: Path) -> dict[str, bytes]:
    """The contents of every file under ``root``, by path relative to it."""
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def read_complete_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """The records on a JSONL file's complete lines, leaving out a last line still being written; none when missing."""
    if not path.is_file():
        return []
    text = path.read_text()
    complete_text = text[: text.rfind("\n") + 1]
    return [json.loads(line) for line in complete_text.splitlines() if line.strip()]


def read_common_transcript_records(state_dir: Path) -> list[dict[str, Any]]:
    """The records in the agent's common transcript, or none when it has not been written."""
    return read_complete_jsonl_records(state_dir / _COMMON_TRANSCRIPT_RELATIVE_PATH)
