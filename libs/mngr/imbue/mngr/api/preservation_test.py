from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import get_preserved_agent_dir
from imbue.mngr.api.preservation import preserve_agent_data
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.hosts.offline_host import make_readable_offline_host
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.providers.local.instance import LocalProviderInstance


def _claude_like_items() -> list[PreservedItem]:
    return [
        PreservedItem(rel_path="plugin/claude/anthropic/projects", kind=VolumeFileType.DIRECTORY),
        PreservedItem(rel_path="logs/claude_transcript", kind=VolumeFileType.DIRECTORY),
        PreservedItem(rel_path="claude_session_id_history", kind=VolumeFileType.FILE),
        # An item that does not exist on the source -- must be skipped silently.
        PreservedItem(rel_path="does/not/exist", kind=VolumeFileType.DIRECTORY),
    ]


def _populate_state_dir(state_dir: Path) -> None:
    """Write a representative set of files into an agent state directory."""
    (state_dir / "plugin" / "claude" / "anthropic" / "projects" / "proj").mkdir(parents=True, exist_ok=True)
    (state_dir / "plugin" / "claude" / "anthropic" / "projects" / "proj" / "session.jsonl").write_text(
        '{"event": 1}\n'
    )
    (state_dir / "logs" / "claude_transcript").mkdir(parents=True, exist_ok=True)
    (state_dir / "logs" / "claude_transcript" / "events.jsonl").write_text("raw\n")
    (state_dir / "claude_session_id_history").write_text("sess-1\nsess-2\n")


def _assert_mirrored(dest_root: Path) -> None:
    """Assert the preserved files mirror the agent-state-dir layout verbatim."""
    projects_file = dest_root / "plugin" / "claude" / "anthropic" / "projects" / "proj" / "session.jsonl"
    assert projects_file.read_text() == '{"event": 1}\n'
    assert (dest_root / "logs" / "claude_transcript" / "events.jsonl").read_text() == "raw\n"
    assert (dest_root / "claude_session_id_history").read_text() == "sess-1\nsess-2\n"
    # The non-existent item must not have produced anything.
    assert not (dest_root / "does").exists()


def test_get_preserved_agent_dir_layout() -> None:
    agent_id = AgentId.generate()
    path = get_preserved_agent_dir(Path("/host"), AgentName("amy"), agent_id)
    assert path == Path(f"/host/preserved/amy--{agent_id}")


@pytest.mark.rsync
def test_preserve_agent_data_online_mirrors_layout(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Preserving from an online (local) host copies files to a mirrored layout."""
    state_dir = tmp_path / "state"
    _populate_state_dir(state_dir)
    dest_root = tmp_path / "dest"

    preserve_agent_data(_claude_like_items(), local_host, state_dir, dest_root, temp_mngr_ctx)

    _assert_mirrored(dest_root)


def _make_offline_with_volume(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> OfflineHostWithVolume:
    offline = OfflineHost(
        id=local_provider.host_id,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
        certified_host_data=CertifiedHostData(
            host_id=str(local_provider.host_id),
            host_name="local",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
    )
    readable = make_readable_offline_host(offline)
    assert isinstance(readable, OfflineHostWithVolume), "local provider should expose a readable volume"
    return readable


def test_offline_host_with_volume_reads_via_host_dir_paths(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """OfflineHostWithVolume reads files addressed by absolute paths under host_dir."""
    host = _make_offline_with_volume(local_provider, temp_mngr_ctx)
    agent_id = AgentId.generate()
    state_dir = host.host_dir / "agents" / str(agent_id)
    _populate_state_dir(state_dir)

    assert host.read_file(state_dir / "claude_session_id_history") == b"sess-1\nsess-2\n"
    assert host.read_text_file(state_dir / "claude_session_id_history") == "sess-1\nsess-2\n"
    assert host.path_exists(state_dir / "logs" / "claude_transcript")
    assert not host.path_exists(state_dir / "nope")

    listed = host.list_directory(state_dir / "logs" / "claude_transcript", recursive=True)
    listed_paths = {entry.path for entry in listed}
    assert str(state_dir / "logs" / "claude_transcript" / "events.jsonl") in listed_paths
    assert all(Path(entry.path).is_absolute() for entry in listed)

    mtime = host.get_file_mtime(state_dir / "claude_session_id_history")
    assert isinstance(mtime, datetime)


def test_preserve_agent_data_offline_mirrors_layout(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Preserving from a volume-backed offline host walks the volume and mirrors layout."""
    host = _make_offline_with_volume(local_provider, temp_mngr_ctx)
    agent_id = AgentId.generate()
    state_dir = host.host_dir / "agents" / str(agent_id)
    _populate_state_dir(state_dir)
    dest_root = tmp_path / "offline_dest"

    # The offline host is a read-only file reader, not an online host.
    assert isinstance(host, HostFileReadInterface)

    preserve_agent_data(_claude_like_items(), host, state_dir, dest_root, temp_mngr_ctx)

    _assert_mirrored(dest_root)
