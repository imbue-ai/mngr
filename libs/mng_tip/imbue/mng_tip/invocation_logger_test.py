import json
from pathlib import Path

from imbue.mng_tip.invocation_logger import get_tip_data_dir
from imbue.mng_tip.invocation_logger import log_invocation
from imbue.mng_tip.invocation_logger import read_recent_invocations


class TestGetTipDataDir:
    def test_returns_tip_subdirectory_of_host_dir(self, temp_host_dir: Path) -> None:
        result = get_tip_data_dir()
        assert result == temp_host_dir / "tip"

    def test_uses_mng_host_dir_env(self, temp_host_dir: Path) -> None:
        result = get_tip_data_dir()
        assert str(temp_host_dir) in str(result)


class TestLogInvocation:
    def test_creates_tip_dir_and_file(self, temp_host_dir: Path) -> None:
        log_invocation("list", {"output_format": "human"})
        invocations_path = get_tip_data_dir() / "invocations.jsonl"
        assert invocations_path.exists()

    def test_writes_valid_jsonl(self, temp_host_dir: Path) -> None:
        log_invocation("list", {"output_format": "json"})
        invocations_path = get_tip_data_dir() / "invocations.jsonl"
        lines = invocations_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["command"] == "list"
        assert "timestamp" in record
        assert "argv" in record

    def test_appends_multiple_invocations(self, temp_host_dir: Path) -> None:
        log_invocation("list", {})
        log_invocation("create", {"name": "agent-1"})
        log_invocation("connect", {"agent_name": "agent-1"})
        invocations_path = get_tip_data_dir() / "invocations.jsonl"
        lines = invocations_path.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["command"] == "list"
        assert json.loads(lines[1])["command"] == "create"
        assert json.loads(lines[2])["command"] == "connect"


class TestReadRecentInvocations:
    def test_returns_empty_list_when_no_file(self, temp_host_dir: Path) -> None:
        assert read_recent_invocations() == []

    def test_reads_all_invocations(self, temp_host_dir: Path) -> None:
        log_invocation("list", {})
        log_invocation("create", {})
        records = read_recent_invocations()
        assert len(records) == 2
        assert records[0]["command"] == "list"
        assert records[1]["command"] == "create"

    def test_respects_max_lines(self, temp_host_dir: Path) -> None:
        for i in range(10):
            log_invocation(f"cmd-{i}", {})
        records = read_recent_invocations(max_lines=3)
        assert len(records) == 3
        assert records[0]["command"] == "cmd-7"
        assert records[2]["command"] == "cmd-9"

    def test_skips_malformed_lines(self, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        invocations_path = tip_dir / "invocations.jsonl"
        invocations_path.write_text('not-json\n{"command": "list"}\n')
        records = read_recent_invocations()
        assert len(records) == 1
        assert records[0]["command"] == "list"
