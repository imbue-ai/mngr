from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_grok.resources.testing import SESSION_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import TRANSCRIPT_SCRIPT_NAMES
from imbue.mngr_grok.resources.testing import install_transcript_scripts
from imbue.mngr_grok.resources.testing import read_common_transcript_records
from imbue.mngr_grok.resources.testing import run_agent_command_script
from imbue.mngr_grok.resources.testing import stage_fixture_sessions

_SCENARIOS: Final[tuple[str, ...]] = tuple(
    sorted(path.name for path in SESSION_FIXTURES_DIR.iterdir() if path.is_dir())
)


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_emitted_common_records_conform_to_canonical_schema(
    tmp_path: Path, stub_mngr_log_sh: str, scenario: str
) -> None:
    """Every record the real streamer and converter emit for a captured grok session validates."""
    state_dir = tmp_path / "agent-state"
    install_transcript_scripts(state_dir, TRANSCRIPT_SCRIPT_NAMES, stub_mngr_log_sh)
    stage_fixture_sessions(state_dir, scenario)

    run_agent_command_script(state_dir, "stream_transcript.sh", "--single-pass")
    run_agent_command_script(state_dir, "common_transcript.sh", "--single-pass")

    records = read_common_transcript_records(state_dir)
    assert records[0]["type"] == "header"
    assert {record["type"] for record in records} <= {"header", "step", "observation"}
    assert {record["source"] for record in records if record["type"] == "step"} == {"user", "agent"}
    assert all(record["emitter"] == "grok/common_transcript" for record in records)
    assert len({record["event_id"] for record in records}) == len(records)
    for record in records:
        assert validate_common_transcript_record(record) is None, record
