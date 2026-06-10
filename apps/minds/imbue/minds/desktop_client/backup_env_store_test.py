"""Unit tests for the canonical per-workspace restic env store."""

import stat
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_env_store import canonical_env_path
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.mngr.primitives import AgentId


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    assert read_canonical_env(_paths(tmp_path), agent_id) is None
    assert has_canonical_env(_paths(tmp_path), agent_id) is False


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n")
    assert has_canonical_env(paths, agent_id) is True
    assert read_canonical_env(paths, agent_id) == "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n"


def test_write_overwrites_existing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=old\n")
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=new\n")
    assert read_canonical_env(paths, agent_id) == "RESTIC_REPOSITORY=new\n"


def test_written_file_is_owner_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:r\n")
    mode = stat.S_IMODE(canonical_env_path(paths, agent_id).stat().st_mode)
    assert mode == 0o600


def test_distinct_workspaces_get_distinct_files(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = AgentId.generate()
    second = AgentId.generate()
    assert canonical_env_path(paths, first) != canonical_env_path(paths, second)


def test_parse_restic_env_handles_export_quotes_and_comments() -> None:
    parsed = parse_restic_env(
        "# header\nexport RESTIC_REPOSITORY=s3:r\nAWS_ACCESS_KEY_ID=\"a b\"\n\n# c\nRESTIC_PASSWORD='p'\n"
    )
    assert parsed == {
        "RESTIC_REPOSITORY": "s3:r",
        "AWS_ACCESS_KEY_ID": "a b",
        "RESTIC_PASSWORD": "p",
    }
