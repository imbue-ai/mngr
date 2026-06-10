from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.offline_host import OfflineHostWithVolume
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.docker.host_store import HostRecord
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.testing import make_docker_provider_with_local_volume
from imbue.mngr_file.cli.target import ResolveFileTargetResult
from imbue.mngr_file.cli.target import _compute_agent_base_path
from imbue.mngr_file.cli.target import resolve_full_path
from imbue.mngr_file.data_types import PathRelativeTo

_HOST_ID = "host-00000000000000000000000000000001"


def _make_readable_offline_host(
    provider: DockerProviderInstance,
    host_id: HostId,
) -> OfflineHostWithVolume:
    """Build a volume-backed readable offline host, as resolve_file_target would."""
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="h",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    host = provider._create_host_from_host_record(record)
    assert isinstance(host, OfflineHostWithVolume)
    return host


# --- resolve_full_path ---


def test_resolve_full_path_with_relative_path() -> None:
    assert resolve_full_path(Path("/home/user/work"), "config.toml") == Path("/home/user/work/config.toml")


def test_resolve_full_path_with_nested_relative_path() -> None:
    assert resolve_full_path(Path("/home/user/work"), "subdir/file.txt") == Path("/home/user/work/subdir/file.txt")


def test_resolve_full_path_with_absolute_path_ignores_base() -> None:
    assert resolve_full_path(Path("/home/user/work"), "/etc/hostname") == Path("/etc/hostname")


def test_resolve_full_path_with_dot_relative_path() -> None:
    assert resolve_full_path(Path("/home/user/work"), "./local/file.txt") == Path("/home/user/work/local/file.txt")


# --- _compute_agent_base_path ---


def test_compute_agent_base_path_work() -> None:
    work_dir = Path("/agent/work")
    result = _compute_agent_base_path(PathRelativeTo.WORK, work_dir, Path("/home/.mngr"), AgentId.generate())
    assert result == work_dir


def test_compute_agent_base_path_state() -> None:
    host_dir = Path("/home/user/.mngr")
    agent_id = AgentId.generate()
    result = _compute_agent_base_path(PathRelativeTo.STATE, Path("/work"), host_dir, agent_id)
    assert result == host_dir / "agents" / str(agent_id)


def test_compute_agent_base_path_host() -> None:
    host_dir = Path("/home/user/.mngr")
    result = _compute_agent_base_path(PathRelativeTo.HOST, Path("/work"), host_dir, AgentId.generate())
    assert result == host_dir


# --- ResolveFileTargetResult ---


def test_resolve_file_target_result_is_online_false_for_offline_host(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    provider = make_docker_provider_with_local_volume(temp_mngr_ctx, tmp_path)
    host = _make_readable_offline_host(provider, HostId(_HOST_ID))

    result = ResolveFileTargetResult(
        host=host,
        base_path=host.host_dir,
        is_agent=False,
        agent_id=None,
        relative_to=PathRelativeTo.HOST,
    )

    assert result.is_online is False
    assert not isinstance(result.host, OnlineHostInterface)
    # The readable offline host exposes a real host_dir and reads by absolute path.
    assert result.base_path == host.host_dir
