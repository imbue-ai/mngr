from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.desktop_client.latchkey_gateway_store import LatchkeyGatewayRecord
from imbue.minds.desktop_client.latchkey_gateway_store import delete_gateway_record
from imbue.minds.desktop_client.latchkey_gateway_store import gateway_log_path
from imbue.minds.desktop_client.latchkey_gateway_store import list_gateway_records
from imbue.minds.desktop_client.latchkey_gateway_store import load_gateway_record
from imbue.minds.desktop_client.latchkey_gateway_store import save_gateway_record
from imbue.mngr.primitives import AgentId


def _make_record(agent_id: AgentId | None = None) -> LatchkeyGatewayRecord:
    return LatchkeyGatewayRecord(
        agent_id=agent_id or AgentId(),
        host="127.0.0.1",
        port=19999,
        pid=12345,
        started_at=datetime.now(timezone.utc),
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    record = _make_record()
    save_gateway_record(tmp_path, record)
    loaded = load_gateway_record(tmp_path, record.agent_id)
    assert loaded == record


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_gateway_record(tmp_path, AgentId()) is None


def test_load_returns_none_when_malformed(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = tmp_path / "agents" / str(agent_id) / "latchkey_gateway.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert load_gateway_record(tmp_path, agent_id) is None


def test_delete_is_idempotent(tmp_path: Path) -> None:
    agent_id = AgentId()
    delete_gateway_record(tmp_path, agent_id)
    record = _make_record(agent_id)
    save_gateway_record(tmp_path, record)
    delete_gateway_record(tmp_path, agent_id)
    delete_gateway_record(tmp_path, agent_id)
    assert load_gateway_record(tmp_path, agent_id) is None


def test_list_gateway_records_returns_all_valid_records(tmp_path: Path) -> None:
    record_a = _make_record()
    record_b = _make_record()
    save_gateway_record(tmp_path, record_a)
    save_gateway_record(tmp_path, record_b)

    # Also drop a malformed record to make sure list skips it.
    rogue_agent = AgentId()
    rogue_path = tmp_path / "agents" / str(rogue_agent) / "latchkey_gateway.json"
    rogue_path.parent.mkdir(parents=True, exist_ok=True)
    rogue_path.write_text("definitely not json")

    records = list_gateway_records(tmp_path)
    assert {r.agent_id for r in records} == {record_a.agent_id, record_b.agent_id}


def test_list_gateway_records_returns_empty_when_agents_dir_missing(tmp_path: Path) -> None:
    assert list_gateway_records(tmp_path) == []


def test_gateway_log_path_uses_per_agent_subdirectory(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = gateway_log_path(tmp_path, agent_id)
    assert path == tmp_path / "agents" / str(agent_id) / "latchkey_gateway.log"
