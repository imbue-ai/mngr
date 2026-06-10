"""Unit tests for the framework's outputs-archive pulling helpers."""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.pulling import is_agent_outputs_ready
from imbue.mngr_mapreduce.pulling import pull_agent_outputs


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
