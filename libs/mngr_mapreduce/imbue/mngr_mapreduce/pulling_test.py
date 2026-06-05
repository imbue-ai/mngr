"""Unit tests for the framework's outputs-archive pulling helpers."""

import io
import tarfile
from pathlib import Path

from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.archive import ARCHIVE_SUBPATH
from imbue.mngr_mapreduce.pulling import is_agent_outputs_ready
from imbue.mngr_mapreduce.pulling import pull_agent_outputs


def _make_outputs_archive(file_contents_by_name: dict[str, bytes]) -> bytes:
    """Build an in-memory ``.tar.gz`` with the given member files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for member_name, content in file_contents_by_name.items():
            info = tarfile.TarInfo(name=member_name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _publish_outputs_archive(
    mngr_ctx: MngrContext,
    host_id: HostId,
    agent_id: AgentId,
    archive_bytes: bytes,
) -> None:
    """Write an outputs archive to an agent's state volume exactly where the
    framework polls for it, mirroring how a real agent publishes its tarball."""
    provider = get_provider_instance(ProviderInstanceName("local"), mngr_ctx)
    host_volume = provider.get_volume_for_host(host_id)
    assert host_volume is not None
    agent_volume = host_volume.get_agent_volume(agent_id)
    agent_volume.write_files({ARCHIVE_SUBPATH: archive_bytes})


def test_is_agent_outputs_ready_returns_false_when_archive_missing(temp_mngr_ctx: MngrContext) -> None:
    """No agent has actually published anything to this volume yet."""
    ready = is_agent_outputs_ready(
        mngr_ctx=temp_mngr_ctx,
        provider_name=ProviderInstanceName("local"),
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
    )
    assert ready is False


def test_pull_agent_outputs_returns_none_when_archive_missing(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """No archive => no extraction directory."""
    result = pull_agent_outputs(
        mngr_ctx=temp_mngr_ctx,
        provider_name=ProviderInstanceName("local"),
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("a"),
        destination_dir=tmp_path,
    )
    assert result is None


def test_is_agent_outputs_ready_returns_true_when_archive_published(temp_mngr_ctx: MngrContext) -> None:
    """Once the agent has written the final archive, readiness flips to True."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _publish_outputs_archive(temp_mngr_ctx, host_id, agent_id, _make_outputs_archive({"report.txt": b"hi"}))

    ready = is_agent_outputs_ready(
        mngr_ctx=temp_mngr_ctx,
        provider_name=ProviderInstanceName("local"),
        host_id=host_id,
        agent_id=agent_id,
    )
    assert ready is True


def test_pull_agent_outputs_extracts_archive_contents_under_agent_name(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A published archive is downloaded and extracted under
    ``<destination_dir>/<agent_name>/`` with its members intact."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    agent_name = AgentName("mapper-7")
    _publish_outputs_archive(
        temp_mngr_ctx,
        host_id,
        agent_id,
        _make_outputs_archive({"summary.md": b"all good", "nested/data.txt": b"42"}),
    )

    result = pull_agent_outputs(
        mngr_ctx=temp_mngr_ctx,
        provider_name=ProviderInstanceName("local"),
        host_id=host_id,
        agent_id=agent_id,
        agent_name=agent_name,
        destination_dir=tmp_path,
    )

    assert result is not None
    assert result == tmp_path / "mapper-7"
    assert (result / "summary.md").read_bytes() == b"all good"
    assert (result / "nested" / "data.txt").read_bytes() == b"42"
