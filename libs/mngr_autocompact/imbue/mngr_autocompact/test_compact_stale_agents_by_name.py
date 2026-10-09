from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.agent import read_idle_since_for_compaction
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode
from imbue.mngr_autocompact.manager import compact_stale_agents_by_name

_IDLE_SINCE_FILENAME = "test_idle_since"
_COMPACTION_REQUESTED_FILENAME = "test_compaction_requested"


class _FileStateCompactionAgent(SendKeysAgent[AgentTypeConfig], HasCompactionMixin):
    """Real tmux-backed agent whose idle epoch lives in a state-dir file, so a test can make it eligible."""

    def request_compaction(
        self,
        instructions: str | None = None,
        message_lock_timeout_seconds: float | None = None,
        expected_idle_since: datetime | None = None,
    ) -> None:
        with self._message_lock(timeout_seconds=message_lock_timeout_seconds):
            read_idle_since_for_compaction(self, self.name, expected_idle_since)
            self.host.write_text_file(self._get_agent_dir() / _COMPACTION_REQUESTED_FILENAME, "requested")

    def get_cache_ttl_minutes(self) -> int | None:
        return 60

    def get_context_tokens(self) -> int | None:
        return 500_000

    def get_idle_since(self) -> datetime | None:
        agent_dir = self._get_agent_dir()
        if self.host.path_exists(agent_dir / _COMPACTION_REQUESTED_FILENAME):
            return None
        if not self.host.path_exists(agent_dir / _IDLE_SINCE_FILENAME):
            return None
        return datetime.fromisoformat(self.host.read_text_file(agent_dir / _IDLE_SINCE_FILENAME))


def _make_proactive_timer_mngr_ctx(temp_mngr_ctx: MngrContext) -> MngrContext:
    config = temp_mngr_ctx.config.model_copy_update(
        to_update(
            temp_mngr_ctx.config.field_ref().plugins,
            {
                **temp_mngr_ctx.config.plugins,
                PluginName("autocompact"): AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER),
            },
        ),
    )
    return make_mngr_ctx(
        config, temp_mngr_ctx.pm, temp_mngr_ctx.profile_dir, concurrency_group=temp_mngr_ctx.concurrency_group
    )


def _create_agent_idle_since(
    host: Host, work_dir: Path, agent_type: AgentTypeName, idle_since: datetime
) -> AgentInterface:
    agent = host.create_agent_state(
        work_dir,
        CreateAgentOptions(
            name=AgentName(f"agent-{uuid4().hex}"),
            agent_type=agent_type,
            command=CommandString("sleep 84617"),
        ),
    )
    host.write_text_file(
        get_agent_state_dir_path(host.host_dir, agent.id) / _IDLE_SINCE_FILENAME, idle_since.isoformat()
    )
    return agent


def _was_compaction_requested(host: Host, agent: AgentInterface) -> bool:
    return host.path_exists(get_agent_state_dir_path(host.host_dir, agent.id) / _COMPACTION_REQUESTED_FILENAME)


@pytest.mark.tmux
def test_compact_stale_agents_by_name_compacts_an_eligible_running_local_agent(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    agent_type = AgentTypeName(f"file-state-compaction-{uuid4().hex}")
    register_agent_class(str(agent_type), _FileStateCompactionAgent)
    register_agent_config(str(agent_type), AgentTypeConfig)
    mngr_ctx = _make_proactive_timer_mngr_ctx(temp_mngr_ctx)
    host = get_provider_instance(ProviderInstanceName("local"), mngr_ctx).create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    now = datetime.now(timezone.utc)

    eligible_agent = _create_agent_idle_since(host, temp_work_dir, agent_type, now - timedelta(minutes=100))
    recently_idle_agent = _create_agent_idle_since(host, temp_work_dir, agent_type, now - timedelta(minutes=5))
    stopped_agent = _create_agent_idle_since(host, temp_work_dir, agent_type, now - timedelta(minutes=100))
    unrequested_agent = _create_agent_idle_since(host, temp_work_dir, agent_type, now - timedelta(minutes=100))
    running_agents = [eligible_agent, recently_idle_agent, unrequested_agent]
    host.start_agents([agent.id for agent in running_agents])
    try:
        wait_for(
            lambda: all(agent.is_running() for agent in running_agents),
            timeout=30.0,
            poll_interval=0.5,
            error_message="agents did not start running",
        )

        compacted = compact_stale_agents_by_name(
            mngr_ctx, names=[eligible_agent.name, recently_idle_agent.name, stopped_agent.name], now=now
        )

        assert compacted == [eligible_agent.name]
        assert _was_compaction_requested(host, eligible_agent)
        assert not _was_compaction_requested(host, recently_idle_agent)
        assert not _was_compaction_requested(host, stopped_agent)
        assert not _was_compaction_requested(host, unrequested_agent)
    finally:
        for agent in [*running_agents, stopped_agent]:
            host.destroy_agent(agent)
