import json
import stat
from pathlib import Path

import pytest

from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr_forward.errors import ForwardSubprocessError
from imbue.mngr_forward.snapshot import _parse_snapshot
from imbue.mngr_forward.snapshot import mngr_list_snapshot
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2


def _write_fake_mngr(script_path: Path, argv_record_path: Path, stdout_payload: str, exit_code: int) -> None:
    """Write an executable fake `mngr` that records its argv and emits a fixed payload.

    Lets the snapshot tests exercise the real subprocess + exit-code path
    without mocking: `mngr_list_snapshot` accepts the binary path directly.
    """
    script_path.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" > {argv_record_path}\n'
        f"cat <<'PAYLOAD_EOF'\n{stdout_payload}\nPAYLOAD_EOF\n"
        f"exit {exit_code}\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _single_agent_payload() -> str:
    return json.dumps({"agents": [{"id": str(TEST_AGENT_ID_1), "labels": {}}], "errors": []})


def test_parse_empty_snapshot_returns_no_agents() -> None:
    result = _parse_snapshot("")
    assert result.agents == ()


def test_parse_snapshot_with_local_agent() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "labels": {"workspace": "true"},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    assert len(result.agents) == 1
    entry = result.agents[0]
    assert entry.agent_id == TEST_AGENT_ID_1
    assert entry.ssh_info is None
    assert entry.labels == {"workspace": "true"}


def test_parse_snapshot_with_remote_agent() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "host": {
                        "ssh": {
                            "user": "root",
                            "host": "1.2.3.4",
                            "port": 22,
                            "key_path": "/tmp/k",
                        }
                    },
                    "labels": {},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    [entry] = result.agents
    assert entry.ssh_info is not None
    assert entry.ssh_info.host == "1.2.3.4"
    assert entry.ssh_info.port == 22
    assert entry.ssh_info.key_path == Path("/tmp/k")


def test_parse_snapshot_skips_agents_without_id() -> None:
    payload = json.dumps({"agents": [{"labels": {}}, {"id": str(TEST_AGENT_ID_2)}]})
    result = _parse_snapshot(payload)
    assert len(result.agents) == 1
    assert result.agents[0].agent_id == TEST_AGENT_ID_2


def test_parse_snapshot_extracts_name_host_id_provider_name() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "name": "my-agent",
                    "host": {
                        "id": "host-1",
                        "provider_name": "modal",
                    },
                    "labels": {},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    [entry] = result.agents
    assert entry.agent_name == "my-agent"
    assert entry.host_id == "host-1"
    assert entry.provider_name == "modal"


def test_parse_snapshot_defaults_missing_filter_fields_to_empty_string() -> None:
    """An older mngr list payload that lacks the filter fields still parses."""
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "labels": {},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    [entry] = result.agents
    assert entry.agent_name == ""
    assert entry.host_id == ""
    assert entry.provider_name == ""


def test_mngr_list_snapshot_omits_on_error_under_abort(tmp_path: Path) -> None:
    script = tmp_path / "fake_mngr"
    argv_record = tmp_path / "argv.txt"
    _write_fake_mngr(script, argv_record, _single_agent_payload(), exit_code=0)
    mngr_list_snapshot(mngr_binary=str(script), error_behavior=ErrorBehavior.ABORT)
    assert "--on-error" not in argv_record.read_text()


def test_mngr_list_snapshot_passes_on_error_continue(tmp_path: Path) -> None:
    script = tmp_path / "fake_mngr"
    argv_record = tmp_path / "argv.txt"
    _write_fake_mngr(script, argv_record, _single_agent_payload(), exit_code=0)
    mngr_list_snapshot(mngr_binary=str(script), error_behavior=ErrorBehavior.CONTINUE)
    assert "--on-error continue" in argv_record.read_text()


def test_mngr_list_snapshot_tolerates_provider_inaccessible_exit_under_continue(tmp_path: Path) -> None:
    script = tmp_path / "fake_mngr"
    argv_record = tmp_path / "argv.txt"
    _write_fake_mngr(script, argv_record, _single_agent_payload(), exit_code=EXIT_CODE_PROVIDER_INACCESSIBLE)
    result = mngr_list_snapshot(mngr_binary=str(script), error_behavior=ErrorBehavior.CONTINUE)
    assert len(result.agents) == 1
    assert result.agents[0].agent_id == TEST_AGENT_ID_1


def test_mngr_list_snapshot_raises_on_provider_inaccessible_exit_under_abort(tmp_path: Path) -> None:
    script = tmp_path / "fake_mngr"
    argv_record = tmp_path / "argv.txt"
    _write_fake_mngr(script, argv_record, _single_agent_payload(), exit_code=EXIT_CODE_PROVIDER_INACCESSIBLE)
    with pytest.raises(ForwardSubprocessError):
        mngr_list_snapshot(mngr_binary=str(script), error_behavior=ErrorBehavior.ABORT)


def test_mngr_list_snapshot_raises_on_other_nonzero_exit_under_continue(tmp_path: Path) -> None:
    script = tmp_path / "fake_mngr"
    argv_record = tmp_path / "argv.txt"
    _write_fake_mngr(script, argv_record, _single_agent_payload(), exit_code=1)
    with pytest.raises(ForwardSubprocessError):
        mngr_list_snapshot(mngr_binary=str(script), error_behavior=ErrorBehavior.CONTINUE)
