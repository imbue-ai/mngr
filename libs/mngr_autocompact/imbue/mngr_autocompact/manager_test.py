import fcntl
import itertools
import json
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast
from uuid import uuid4

import pytest
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupState
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.mngr.agents.base_agent import SEND_COMMAND_TIMEOUT_SECONDS
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.compaction_transcript import COMPACTION_TRANSCRIPT_CACHE
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptScanner
from imbue.mngr.agents.mock_host_file_read_test import InMemoryHostFileReader
from imbue.mngr.agents.mock_host_test import HangingTmuxHost
from imbue.mngr.api import providers as providers_module
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.agent import read_idle_since_for_compaction
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import CertifiedHostData
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import file_lock_held_by_another_process
from imbue.mngr_autocompact import manager as manager_module
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode
from imbue.mngr_autocompact.config import DEFAULT_AUTOCOMPACT_MIN_CONTEXT_TOKENS
from imbue.mngr_autocompact.manager import COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS
from imbue.mngr_autocompact.manager import compact_agent_if_stale
from imbue.mngr_autocompact.manager import compact_stale_agents
from imbue.mngr_autocompact.manager import compact_stale_agents_by_name
from imbue.mngr_autocompact.manager import get_compaction_agents
from imbue.mngr_autocompact.manager import get_stale_agents
from imbue.mngr_autocompact.manager import get_stale_idle_since
from imbue.mngr_autocompact.manager import is_agent_stale_for_compaction
from imbue.mngr_autocompact.manager import trigger_compaction

_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS = DEFAULT_AUTOCOMPACT_MIN_CONTEXT_TOKENS + 50_000


class _DummyNonCompactionAgent:
    """Dummy agent without compaction capability."""

    def __init__(self, id: AgentId, name: AgentName, agent_type: AgentTypeName, running: bool = True) -> None:
        self.id = id
        self.name = name
        self.agent_type = agent_type
        self.running = running

    def is_running(self) -> bool:
        return self.running


class _DummyCompactionAgent(HasCompactionMixin):
    """Dummy agent implementing HasCompactionMixin."""

    def __init__(
        self,
        id: AgentId,
        name: AgentName,
        agent_type: AgentTypeName,
        running: bool = True,
        cache_ttl: int | None = 60,
        context_tokens: int | None = _INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt: datetime | None = None,
        mngr_ctx: MngrContext | None = None,
        raise_on_is_running: Exception | None = None,
        raise_on_request_compaction: Exception | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.agent_type = agent_type
        self.running = running
        self.cache_ttl = cache_ttl
        self.context_tokens = context_tokens
        self.idle_since_dt = idle_since_dt
        self.compaction_count = 0
        self.last_instructions: str | None = None
        self.mngr_ctx = mngr_ctx
        self.raise_on_is_running = raise_on_is_running
        self.raise_on_request_compaction = raise_on_request_compaction
        self.is_running_call_count = 0

    def is_running(self) -> bool:
        self.is_running_call_count += 1
        if self.raise_on_is_running is not None:
            raise self.raise_on_is_running
        return self.running

    def request_compaction(
        self,
        instructions: str | None = None,
        message_lock_timeout_seconds: float | None = None,
        expected_idle_since: datetime | None = None,
    ) -> None:
        if self.raise_on_request_compaction is not None:
            raise self.raise_on_request_compaction
        self.compaction_count += 1
        self.last_instructions = instructions
        self.idle_since_dt = None

    def get_cache_ttl_minutes(self) -> int | None:
        return self.cache_ttl

    def get_context_tokens(self) -> int | None:
        return self.context_tokens

    def get_idle_since(self) -> datetime | None:
        return self.idle_since_dt


def test_is_agent_stale_non_compaction_agent() -> None:
    agent = _DummyNonCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("other"),
        running=True,
    )
    config = AutoCompactPluginConfig(mode=ContextCompactionMode.ON_NEXT_PROMPT)
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.ON_NEXT_PROMPT
    )
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER
    )


def test_is_agent_stale_disabled_mode() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(mode=ContextCompactionMode.DISABLED)
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.ON_NEXT_PROMPT, now=now
    )


def test_is_agent_stale_not_running_or_not_idle() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=False,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER)
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )

    agent.running = True
    agent.idle_since_dt = None
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )


def test_is_agent_stale_unknown_ttl() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=None,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    # No override, no agent ttl -> False
    config = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER, cache_ttl_minutes=None)
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )

    # Config override -> True
    config_with_ttl = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER, cache_ttl_minutes=60)
    assert is_agent_stale_for_compaction(
        cast(Any, agent), config_with_ttl, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )


def test_is_agent_stale_context_tokens_gating() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=50_000,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)

    # Gated at 100k, agent has 50k -> not stale
    config_100k = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER, min_context_tokens=100_000)
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config_100k, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )

    # Gating disabled (0) -> stale
    config_0 = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER, min_context_tokens=0)
    assert is_agent_stale_for_compaction(
        cast(Any, agent), config_0, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )

    # Agent reports None for context tokens -> not stale
    agent.context_tokens = None
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config_100k, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )
    # Gating disabled with None tokens -> still stale
    assert is_agent_stale_for_compaction(
        cast(Any, agent), config_0, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )


def test_is_agent_stale_timing_logic() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        min_context_tokens=100_000,
    )
    # Default epsilon is 3 minutes -> delay is 57 minutes

    # 30 minutes idle -> not stale
    assert not is_agent_stale_for_compaction(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=datetime(2026, 8, 27, 12, 30, 0, tzinfo=timezone.utc),
    )

    # 56m 59s idle -> not stale
    assert not is_agent_stale_for_compaction(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=datetime(2026, 8, 27, 12, 56, 59, tzinfo=timezone.utc),
    )

    # 57m idle -> stale
    assert is_agent_stale_for_compaction(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=datetime(2026, 8, 27, 12, 57, 0, tzinfo=timezone.utc),
    )


def test_is_agent_stale_on_next_prompt_mode() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.ON_NEXT_PROMPT,
        min_context_tokens=100_000,
    )
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)

    # When expected_mode is PROACTIVE_TIMER -> not stale for compaction
    assert not is_agent_stale_for_compaction(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=now,
    )

    # When expected_mode matches ON_NEXT_PROMPT -> stale
    assert is_agent_stale_for_compaction(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.ON_NEXT_PROMPT,
        now=now,
    )


def test_compact_agent_if_stale_on_next_prompt_mode() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.ON_NEXT_PROMPT,
        min_context_tokens=100_000,
    )
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)

    # When expected_mode is PROACTIVE_TIMER -> compaction not triggered
    assert not compact_agent_if_stale(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=now,
    )
    assert agent.compaction_count == 0

    # With expected_mode=ON_NEXT_PROMPT -> compaction triggered
    assert compact_agent_if_stale(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.ON_NEXT_PROMPT,
        now=now,
    )
    assert agent.compaction_count == 1


def test_trigger_compaction_non_compaction_agent() -> None:
    agent = _DummyNonCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("other"),
        running=True,
    )
    assert not trigger_compaction(cast(Any, agent))


def test_trigger_compaction_success() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
    )
    assert trigger_compaction(cast(Any, agent))
    assert agent.compaction_count == 1


def test_check_and_compact_agent_not_stale() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        epsilon_offset_minutes=2,
    )
    # Only 10m idle -> not stale
    now = datetime(2026, 8, 27, 12, 10, 0, tzinfo=timezone.utc)
    assert not compact_agent_if_stale(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=now,
    )
    assert agent.compaction_count == 0


def test_is_agent_stale_for_compaction_stale() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        epsilon_offset_minutes=2,
    )
    now = datetime(2026, 8, 27, 13, 0, 0, tzinfo=timezone.utc)
    # Staleness check should return True without calling request_compaction
    assert is_agent_stale_for_compaction(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=now,
    )
    assert agent.compaction_count == 0


def test_compact_agent_if_stale_triggers() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        epsilon_offset_minutes=2,
    )
    now = datetime(2026, 8, 27, 13, 0, 0, tzinfo=timezone.utc)
    assert compact_agent_if_stale(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=now,
    )
    assert agent.compaction_count == 1
    assert agent.last_instructions is None


def test_compact_agent_if_stale_with_instructions() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        epsilon_offset_minutes=2,
    )
    now = datetime(2026, 8, 27, 13, 0, 0, tzinfo=timezone.utc)
    assert compact_agent_if_stale(
        cast(Any, agent),
        config,
        expected_mode=ContextCompactionMode.PROACTIVE_TIMER,
        now=now,
        instructions="preserve errors",
    )
    assert agent.compaction_count == 1
    assert agent.last_instructions == "preserve errors"


def test_get_stale_agents_empty(temp_mngr_ctx: MngrContext) -> None:
    stale = get_stale_agents(temp_mngr_ctx)
    assert stale == []


def test_compact_stale_agents_empty(temp_mngr_ctx: MngrContext) -> None:
    compacted = compact_stale_agents(temp_mngr_ctx)
    assert compacted == []


class _UnexpectedAgentError(Exception):
    pass


def test_get_stale_agents_propagates_unexpected_worker_error_unwrapped(temp_mngr_ctx: MngrContext) -> None:
    mngr_ctx = MngrContext(
        config=MngrConfig(
            plugins={
                PluginName("autocompact"): AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER),
            },
        ),
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("broken-agent"),
        agent_type=AgentTypeName("claude"),
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        mngr_ctx=mngr_ctx,
        raise_on_is_running=_UnexpectedAgentError("boom"),
    )
    with pytest.raises(_UnexpectedAgentError, match="boom"):
        get_stale_agents(mngr_ctx, agents=[cast(Any, agent)])


def _test_agents_for_refs(test_agents: Sequence[Any], agent_refs: Sequence[DiscoveredAgent]) -> list[Any]:
    requested_agent_ids = {agent_ref.agent_id for agent_ref in agent_refs}
    return [agent for agent in test_agents if agent.id in requested_agent_ids]


class _FakeOnlineHost(Host):
    test_agents: list[Any] = Field(default_factory=list)

    def load_agents_from_refs(self, agent_refs: Sequence[DiscoveredAgent]) -> list[Any]:
        return _test_agents_for_refs(self.test_agents, agent_refs)


class _ReadCountingHost(Host):
    """Real host that records which agents' data.json it reads and which agent objects it builds."""

    data_file_reads: list[tuple[AgentId, ...] | None] = Field(default_factory=list)
    built_agent_ids: list[AgentId] = Field(default_factory=list)

    def _read_agent_data_files(
        self, timeout_seconds: float | None, agent_ids: Sequence[AgentId] | None
    ) -> dict[str, str] | None:
        self.data_file_reads.append(None if agent_ids is None else tuple(agent_ids))
        return super()._read_agent_data_files(timeout_seconds=timeout_seconds, agent_ids=agent_ids)

    def _build_agent_from_data(self, data: Mapping[str, Any]) -> AgentInterface:
        agent = super()._build_agent_from_data(data)
        self.built_agent_ids.append(agent.id)
        return agent


class _FakeDiscoveryProvider(LocalProviderInstance):
    mock_agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = Field(default_factory=dict)
    mock_hosts: dict[HostId, HostInterface] = Field(default_factory=dict)
    failing_host_ids: set[HostId] = Field(default_factory=set)

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        return self.mock_agents_by_host

    def get_host(self, host: HostId | HostName) -> Host:
        if host in self.failing_host_ids:
            raise OSError("simulated host failure")
        if isinstance(host, HostId) and host in self.mock_hosts:
            return cast(Host, self.mock_hosts[host])
        return super().get_host(host)


class _DummyContext:
    pass


class _DummyShuttingDownCG:
    state = ConcurrencyGroupState.ACTIVE

    def is_shutting_down(self) -> bool:
        return True


class _DummyShuttingDownCtx:
    concurrency_group = _DummyShuttingDownCG()


def test_is_concurrency_group_active_without_cg() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("no-cg-agent"),
        agent_type=AgentTypeName("claude"),
        mngr_ctx=cast(Any, _DummyContext()),
    )
    assert manager_module._is_concurrency_group_active(cast(Any, agent))


def test_is_concurrency_group_active_shutting_down() -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("shutting-down-agent"),
        agent_type=AgentTypeName("claude"),
        mngr_ctx=cast(Any, _DummyShuttingDownCtx()),
    )
    assert not manager_module._is_concurrency_group_active(cast(Any, agent))


def test_concurrency_group_inactive_skips_staleness_and_compaction(temp_mngr_ctx: MngrContext) -> None:
    with ConcurrencyGroup(name="stopped-group") as cg:
        pass
    inactive_ctx = MngrContext(
        config=temp_mngr_ctx.config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=cg,
    )

    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("inactive-agent"),
        agent_type=AgentTypeName("claude"),
        running=True,
        cache_ttl=60,
        context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
        idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        mngr_ctx=inactive_ctx,
    )
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        epsilon_offset_minutes=2,
    )
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    assert not is_agent_stale_for_compaction(
        cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
    )
    assert not trigger_compaction(cast(Any, agent))
    assert agent.compaction_count == 0


def test_is_agent_stale_exceptions() -> None:
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        epsilon_offset_minutes=2,
    )
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)

    for exc in (
        InvalidConcurrencyGroupStateError("inactive"),
        MngrError("mngr failure"),
        OSError("disk error"),
    ):
        agent = _DummyCompactionAgent(
            id=AgentId.generate(),
            name=AgentName("error-agent"),
            agent_type=AgentTypeName("claude"),
            raise_on_is_running=exc,
            cache_ttl=60,
            context_tokens=_INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
            idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert not is_agent_stale_for_compaction(
            cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
        )


_PROBE_TEST_NOW = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
_PROBE_TEST_CONFIG = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER, min_context_tokens=100_000)


@pytest.mark.parametrize(
    ("idle_minutes", "context_tokens", "running", "expected_is_stale", "expected_is_running_call_count"),
    [
        pytest.param(5, 500_000, True, False, 0, id="idle-too-briefly"),
        pytest.param(100, 1_000, True, False, 0, id="under-min-context-tokens"),
        pytest.param(100, 500_000, True, True, 1, id="eligible-and-running"),
        pytest.param(100, 500_000, False, False, 1, id="eligible-but-stopped"),
    ],
)
def test_is_agent_stale_probes_running_state_only_for_an_agent_passing_every_cheaper_check(
    idle_minutes: int,
    context_tokens: int,
    running: bool,
    expected_is_stale: bool,
    expected_is_running_call_count: int,
) -> None:
    agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName(f"probe-{uuid4().hex}"),
        agent_type=AgentTypeName("claude"),
        running=running,
        cache_ttl=60,
        context_tokens=context_tokens,
        idle_since_dt=_PROBE_TEST_NOW - timedelta(minutes=idle_minutes),
    )

    is_stale = is_agent_stale_for_compaction(
        cast(Any, agent), _PROBE_TEST_CONFIG, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=_PROBE_TEST_NOW
    )

    assert is_stale is expected_is_stale
    assert agent.is_running_call_count == expected_is_running_call_count


def _is_agent_stale_for_compaction_with_running_check_first(
    agent: Any,
    config: AutoCompactPluginConfig,
    expected_mode: ContextCompactionMode,
    now: datetime,
) -> bool:
    """The staleness check as it was before the running check moved last, kept to prove the reorder changes no answer."""
    if not isinstance(agent, HasCompactionMixin):
        return False

    if config.mode != expected_mode:
        return False

    try:
        if not manager_module._is_concurrency_group_active(cast(Any, agent)):
            return False

        if not agent.is_running():
            return False

        idle_since = agent.get_idle_since()
        if idle_since is None:
            return False

        effective_ttl = config.cache_ttl_minutes or agent.get_cache_ttl_minutes()
        if effective_ttl is None:
            return False

        if config.min_context_tokens > 0:
            context_tokens = agent.get_context_tokens()
            if context_tokens is None:
                return False
            if context_tokens < config.min_context_tokens:
                return False

        trigger_delay_seconds = config.get_trigger_delay_seconds(effective_ttl)
        idle_duration_seconds = (now - idle_since).total_seconds()
        return idle_duration_seconds >= trigger_delay_seconds
    except InvalidConcurrencyGroupStateError:
        return False
    except (MngrError, OSError):
        return False


class _IdleEpochCompactionAgent(_DummyCompactionAgent):
    """Compaction agent that derives its idle epoch from an active marker and a last-compacted time, as the harnesses do."""

    def __init__(
        self,
        is_active_marker_present: bool,
        last_idle_at: datetime | None,
        last_compacted_idle_since: datetime | None,
        cache_ttl: int | None,
        context_tokens: int | None,
        running: bool,
        raise_on_is_running: Exception | None,
    ) -> None:
        super().__init__(
            id=AgentId.generate(),
            name=AgentName(f"matrix-{uuid4().hex}"),
            agent_type=AgentTypeName("claude"),
            running=running,
            cache_ttl=cache_ttl,
            context_tokens=context_tokens,
            idle_since_dt=last_idle_at,
            raise_on_is_running=raise_on_is_running,
        )
        self.is_active_marker_present = is_active_marker_present
        self.last_compacted_idle_since = last_compacted_idle_since

    def get_idle_since(self) -> datetime | None:
        if self.is_active_marker_present or self.idle_since_dt is None:
            return None
        if self.last_compacted_idle_since is not None and self.last_compacted_idle_since >= self.idle_since_dt:
            return None
        return self.idle_since_dt


def test_is_agent_stale_gives_the_same_answer_as_checking_running_state_first() -> None:
    now = _PROBE_TEST_NOW
    last_idle_options = (None, now - timedelta(minutes=5), now - timedelta(minutes=100))
    is_stale_count = 0
    case_count = 0
    for (
        mode,
        min_context_tokens,
        cache_ttl_override,
        is_active_marker_present,
        last_idle_at,
        last_compacted_offset_minutes,
        agent_cache_ttl,
        context_tokens,
        running,
        is_running_raises,
    ) in itertools.product(
        (ContextCompactionMode.PROACTIVE_TIMER, ContextCompactionMode.ON_NEXT_PROMPT),
        (0, 100_000),
        (None, 30),
        (False, True),
        last_idle_options,
        (None, 0, -1),
        (None, 60),
        (None, 1_000, 500_000),
        (False, True),
        (False, True),
    ):
        last_compacted_idle_since = (
            None
            if last_idle_at is None or last_compacted_offset_minutes is None
            else last_idle_at + timedelta(minutes=last_compacted_offset_minutes)
        )
        config = AutoCompactPluginConfig(
            mode=mode, min_context_tokens=min_context_tokens, cache_ttl_minutes=cache_ttl_override
        )

        agent = _IdleEpochCompactionAgent(
            is_active_marker_present=is_active_marker_present,
            last_idle_at=last_idle_at,
            last_compacted_idle_since=last_compacted_idle_since,
            cache_ttl=agent_cache_ttl,
            context_tokens=context_tokens,
            running=running,
            raise_on_is_running=MngrError("probe failed") if is_running_raises else None,
        )

        expected = _is_agent_stale_for_compaction_with_running_check_first(
            agent, config, ContextCompactionMode.PROACTIVE_TIMER, now
        )
        actual = is_agent_stale_for_compaction(
            cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
        )

        assert actual == expected, (
            f"mismatch for mode={mode} min_tokens={min_context_tokens} ttl_override={cache_ttl_override} "
            f"active={is_active_marker_present} last_idle_at={last_idle_at} "
            f"last_compacted={last_compacted_idle_since} agent_ttl={agent_cache_ttl} tokens={context_tokens} "
            f"running={running} is_running_raises={is_running_raises}"
        )
        is_stale_count += int(actual)
        case_count += 1

    # The matrix must cover both outcomes for the comparison to mean anything
    assert 0 < is_stale_count < case_count


def test_trigger_compaction_exceptions() -> None:
    for exc in (
        InvalidConcurrencyGroupStateError("inactive"),
        MngrError("mngr failure"),
        OSError("disk error"),
    ):
        agent = _DummyCompactionAgent(
            id=AgentId.generate(),
            name=AgentName("error-agent"),
            agent_type=AgentTypeName("claude"),
            running=True,
            raise_on_request_compaction=exc,
        )
        assert not trigger_compaction(cast(Any, agent))
        assert agent.compaction_count == 0


def test_get_stale_agents_and_compact_stale_agents(temp_mngr_ctx: MngrContext) -> None:
    now = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)
    config = MngrConfig(
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
        plugins={
            PluginName("autocompact"): AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER),
        },
    )
    mngr_ctx = MngrContext(
        config=config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )

    provider = _FakeDiscoveryProvider(
        name=ProviderInstanceName("local"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
    )

    # 1. Offline host
    off_hid = HostId.generate()
    off_host = OfflineHost(
        id=off_hid,
        certified_host_data=CertifiedHostData(
            host_id=str(off_hid),
            host_name="off-host",
            created_at=now,
            updated_at=now,
        ),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
    )
    provider.mock_hosts[off_hid] = off_host
    off_ref = DiscoveredHost(
        host_id=off_hid,
        host_name=HostName("off-host"),
        provider_name=provider.name,
        host_state=HostState.STOPPED,
    )
    provider.mock_agents_by_host[off_ref] = []

    # 2. Failing host (raises OSError)
    fail_hid = HostId.generate()
    provider.failing_host_ids.add(fail_hid)
    fail_ref = DiscoveredHost(
        host_id=fail_hid,
        host_name=HostName("fail-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.mock_agents_by_host[fail_ref] = []

    # 3. Online host with various agents
    on_hid = HostId.generate()
    missing_aid = AgentId.generate()
    missing_aref = DiscoveredAgent(
        agent_id=missing_aid,
        agent_name=AgentName("missing-agent"),
        host_id=on_hid,
        provider_name=provider.name,
    )

    noncompact_aid = AgentId.generate()
    noncompact_agent = _DummyNonCompactionAgent(
        id=noncompact_aid,
        name=AgentName("noncompact-agent"),
        agent_type=AgentTypeName("raw"),
    )
    noncompact_aref = DiscoveredAgent(
        agent_id=noncompact_aid,
        agent_name=AgentName("noncompact-agent"),
        host_id=on_hid,
        provider_name=provider.name,
    )

    fresh_aid = AgentId.generate()
    fresh_agent = _DummyCompactionAgent(
        id=fresh_aid,
        name=AgentName("fresh-agent"),
        agent_type=AgentTypeName("claude"),
        mngr_ctx=mngr_ctx,
        idle_since_dt=now - timedelta(minutes=5),
    )
    fresh_aref = DiscoveredAgent(
        agent_id=fresh_aid,
        agent_name=AgentName("fresh-agent"),
        host_id=on_hid,
        provider_name=provider.name,
    )

    stale_aid = AgentId.generate()
    stale_agent = _DummyCompactionAgent(
        id=stale_aid,
        name=AgentName("stale-agent"),
        agent_type=AgentTypeName("claude"),
        mngr_ctx=mngr_ctx,
        idle_since_dt=now - timedelta(minutes=100),
    )
    stale_aref = DiscoveredAgent(
        agent_id=stale_aid,
        agent_name=AgentName("stale-agent"),
        host_id=on_hid,
        provider_name=provider.name,
    )

    # Agent with ON_NEXT_PROMPT mode is idle long enough, but should NOT be considered stale for proactive compaction
    on_prompt_aid = AgentId.generate()
    on_prompt_config = MngrConfig(
        providers={
            ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
        },
        plugins={
            PluginName("autocompact"): AutoCompactPluginConfig(mode=ContextCompactionMode.ON_NEXT_PROMPT),
        },
    )
    on_prompt_ctx = MngrContext(
        config=on_prompt_config,
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    on_prompt_agent = _DummyCompactionAgent(
        id=on_prompt_aid,
        name=AgentName("on-prompt-agent"),
        agent_type=AgentTypeName("claude"),
        mngr_ctx=on_prompt_ctx,
        idle_since_dt=now - timedelta(minutes=100),
    )
    on_prompt_aref = DiscoveredAgent(
        agent_id=on_prompt_aid,
        agent_name=AgentName("on-prompt-agent"),
        host_id=on_hid,
        provider_name=provider.name,
    )

    on_host = _FakeOnlineHost(
        id=on_hid,
        host_name=HostName("on-host"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
        test_agents=[noncompact_agent, fresh_agent, stale_agent, on_prompt_agent],
    )
    provider.mock_hosts[on_hid] = on_host
    on_ref = DiscoveredHost(
        host_id=on_hid,
        host_name=HostName("on-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.mock_agents_by_host[on_ref] = [
        missing_aref,
        noncompact_aref,
        fresh_aref,
        stale_aref,
        on_prompt_aref,
    ]

    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider
    try:
        discovered_agents = get_compaction_agents(mngr_ctx)
        assert len(discovered_agents) == 3
        assert {a.name for a in discovered_agents} == {
            AgentName("fresh-agent"),
            AgentName("stale-agent"),
            AgentName("on-prompt-agent"),
        }

        stale_names = get_stale_agents(mngr_ctx, now=now)
        assert stale_names == [AgentName("stale-agent")]

        stale_explicit = get_stale_agents(mngr_ctx, agents=[cast(Any, fresh_agent), cast(Any, stale_agent)], now=now)
        assert stale_explicit == [AgentName("stale-agent")]

        compacted_names = compact_stale_agents(mngr_ctx, agents=[cast(Any, stale_agent)], now=now)
        assert compacted_names == [AgentName("stale-agent")]
        assert stale_agent.compaction_count == 1
        assert on_prompt_agent.compaction_count == 0

        # Compaction ends the idle epoch, so a discovery-driven pass has nothing left to compact
        compacted_via_discovery = compact_stale_agents(mngr_ctx, now=now)
        assert compacted_via_discovery == []
        assert stale_agent.compaction_count == 1
        assert on_prompt_agent.compaction_count == 0
    finally:
        reset_provider_instances()


_BY_NAME_NOW = datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc)


def test_get_stale_idle_since_returns_the_idle_start_only_for_a_stale_agent() -> None:
    config = AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER)
    stale_idle_since = _BY_NAME_NOW - timedelta(minutes=100)
    stale_agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("stale-agent"),
        agent_type=AgentTypeName("claude"),
        idle_since_dt=stale_idle_since,
    )
    fresh_agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("fresh-agent"),
        agent_type=AgentTypeName("claude"),
        idle_since_dt=_BY_NAME_NOW - timedelta(minutes=5),
    )

    assert (
        get_stale_idle_since(
            cast(Any, stale_agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=_BY_NAME_NOW
        )
        == stale_idle_since
    )
    assert (
        get_stale_idle_since(
            cast(Any, fresh_agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=_BY_NAME_NOW
        )
        is None
    )


def _register_by_name_discovery_provider(temp_mngr_ctx: MngrContext) -> _FakeDiscoveryProvider:
    """Register a fake local provider, with autocompact in proactive_timer mode, for its own mngr context."""
    mngr_ctx = MngrContext(
        config=MngrConfig(
            providers={
                ProviderInstanceName("local"): ProviderInstanceConfig(backend=ProviderBackendName("local")),
            },
            plugins={
                PluginName("autocompact"): AutoCompactPluginConfig(mode=ContextCompactionMode.PROACTIVE_TIMER),
            },
        ),
        pm=temp_mngr_ctx.pm,
        profile_dir=temp_mngr_ctx.profile_dir,
        concurrency_group=temp_mngr_ctx.concurrency_group,
    )
    provider = _FakeDiscoveryProvider(
        name=ProviderInstanceName("local"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=mngr_ctx,
    )
    providers_module._instance_cache[(provider.name, id(mngr_ctx))] = provider
    return provider


def _make_by_name_agent(
    provider: _FakeDiscoveryProvider,
    name_prefix: str,
    idle_minutes: int,
    running: bool = True,
) -> _DummyCompactionAgent:
    return _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName(f"{name_prefix}-{uuid4().hex}"),
        agent_type=AgentTypeName("claude"),
        running=running,
        mngr_ctx=provider.mngr_ctx,
        idle_since_dt=_BY_NAME_NOW - timedelta(minutes=idle_minutes),
    )


def _add_discovered_host(
    provider: _FakeDiscoveryProvider,
    host: HostInterface | None,
    agents: list[Any],
    is_failing: bool = False,
) -> None:
    """Make the provider discover a host holding the given agents, and serve that host from get_host."""
    host_id = host.id if host is not None else HostId.generate()
    if host is not None:
        provider.mock_hosts[host_id] = host
    if is_failing:
        provider.failing_host_ids.add(host_id)
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(f"host-{uuid4().hex}"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.mock_agents_by_host[host_ref] = [
        DiscoveredAgent(agent_id=agent.id, agent_name=agent.name, host_id=host_id, provider_name=provider.name)
        for agent in agents
    ]


def _make_online_host(provider: _FakeDiscoveryProvider, agents: list[Any]) -> _FakeOnlineHost:
    return _FakeOnlineHost(
        id=HostId.generate(),
        host_name=HostName(f"online-{uuid4().hex}"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
        test_agents=agents,
    )


def test_compact_stale_agents_by_name_compacts_only_stale_named_agents(temp_mngr_ctx: MngrContext) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    stale_agent = _make_by_name_agent(provider, "stale", idle_minutes=100)
    fresh_agent = _make_by_name_agent(provider, "fresh", idle_minutes=5)
    unnamed_stale_agent = _make_by_name_agent(provider, "unnamed-stale", idle_minutes=100)
    agents = [stale_agent, fresh_agent, unnamed_stale_agent]
    _add_discovered_host(provider, _make_online_host(provider, agents), agents)

    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[stale_agent.name, fresh_agent.name], now=_BY_NAME_NOW
    )

    assert compacted == [stale_agent.name]
    assert stale_agent.compaction_count == 1
    assert fresh_agent.compaction_count == 0
    assert unnamed_stale_agent.compaction_count == 0


def test_compact_stale_agents_by_name_skips_unknown_name(temp_mngr_ctx: MngrContext) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    stale_agent = _make_by_name_agent(provider, "stale", idle_minutes=100)
    _add_discovered_host(provider, _make_online_host(provider, [stale_agent]), [stale_agent])

    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[AgentName(f"destroyed-{uuid4().hex}"), stale_agent.name], now=_BY_NAME_NOW
    )

    assert compacted == [stale_agent.name]
    assert stale_agent.compaction_count == 1


def test_compact_stale_agents_by_name_skips_stopped_agent_and_offline_host(temp_mngr_ctx: MngrContext) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    stopped_agent = _make_by_name_agent(provider, "stopped", idle_minutes=100, running=False)
    _add_discovered_host(provider, _make_online_host(provider, [stopped_agent]), [stopped_agent])
    offline_host_id = HostId.generate()
    offline_host = OfflineHost(
        id=offline_host_id,
        certified_host_data=CertifiedHostData(
            host_id=str(offline_host_id),
            host_name=f"offline-{uuid4().hex}",
            created_at=_BY_NAME_NOW,
            updated_at=_BY_NAME_NOW,
        ),
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
    )
    offline_agent = _make_by_name_agent(provider, "offline", idle_minutes=100)
    _add_discovered_host(provider, offline_host, [offline_agent])

    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[stopped_agent.name, offline_agent.name], now=_BY_NAME_NOW
    )

    assert compacted == []
    assert stopped_agent.compaction_count == 0
    assert offline_agent.compaction_count == 0


def test_compact_stale_agents_by_name_skips_agent_without_compaction_support(temp_mngr_ctx: MngrContext) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    noncompact_agent = _DummyNonCompactionAgent(
        id=AgentId.generate(),
        name=AgentName(f"noncompact-{uuid4().hex}"),
        agent_type=AgentTypeName("raw"),
    )
    stale_agent = _make_by_name_agent(provider, "stale", idle_minutes=100)
    agents = [noncompact_agent, stale_agent]
    _add_discovered_host(provider, _make_online_host(provider, agents), agents)

    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[noncompact_agent.name, stale_agent.name], now=_BY_NAME_NOW
    )

    assert compacted == [stale_agent.name]


def test_compact_stale_agents_by_name_isolates_failing_host(temp_mngr_ctx: MngrContext) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    unreachable_agent = _make_by_name_agent(provider, "unreachable", idle_minutes=100)
    _add_discovered_host(provider, None, [unreachable_agent], is_failing=True)
    stale_agent = _make_by_name_agent(provider, "stale", idle_minutes=100)
    _add_discovered_host(provider, _make_online_host(provider, [stale_agent]), [stale_agent])

    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[unreachable_agent.name, stale_agent.name], now=_BY_NAME_NOW
    )

    assert compacted == [stale_agent.name]
    assert unreachable_agent.compaction_count == 0
    assert stale_agent.compaction_count == 1


def test_compact_stale_agents_by_name_builds_only_the_named_agents_from_discovered_data(
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    host = _ReadCountingHost(
        id=HostId.generate(),
        host_name=HostName(f"counting-{uuid4().hex}"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
    )
    agents = [
        host.create_agent_state(
            temp_work_dir,
            CreateAgentOptions(
                name=AgentName(f"agent-{uuid4().hex}"),
                agent_type=AgentTypeName("generic"),
                command=CommandString("sleep 61937"),
            ),
        )
        for _ in range(7)
    ]
    requested_agents = agents[2:4]
    host_ref = DiscoveredHost(
        host_id=host.id, host_name=host.host_name, provider_name=provider.name, host_state=HostState.RUNNING
    )
    provider.mock_hosts[host.id] = host
    provider.mock_agents_by_host[host_ref] = host.discover_agents()
    host.data_file_reads.clear()
    host.built_agent_ids.clear()

    compact_stale_agents_by_name(provider.mngr_ctx, names=[agent.name for agent in requested_agents], now=_BY_NAME_NOW)

    assert sorted(host.built_agent_ids) == sorted(agent.id for agent in requested_agents)
    assert host.data_file_reads == []


def test_compact_stale_agents_by_name_with_no_names_compacts_nothing(temp_mngr_ctx: MngrContext) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    stale_agent = _make_by_name_agent(provider, "stale", idle_minutes=100)
    _add_discovered_host(provider, _make_online_host(provider, [stale_agent]), [stale_agent])

    assert compact_stale_agents_by_name(provider.mngr_ctx, names=[], now=_BY_NAME_NOW) == []
    assert stale_agent.compaction_count == 0


class _SendKeysCompactionAgent(SendKeysAgent[AgentTypeConfig], HasCompactionMixin):
    """A real send-keys agent whose compaction follows the harnesses' bounded lock and idle re-check."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    idle_since_dt: datetime | None = None
    agent_dir: Path = Field(default_factory=Path)
    compaction_count: int = 0
    lock_wait_started: threading.Event = Field(default_factory=threading.Event)

    def _get_agent_dir(self) -> Path:
        return self.agent_dir

    def is_running(self) -> bool:
        return True

    def get_cache_ttl_minutes(self) -> int | None:
        return 60

    def get_context_tokens(self) -> int | None:
        return _INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS

    def get_idle_since(self) -> datetime | None:
        return None if (self.agent_dir / "active").exists() else self.idle_since_dt

    def request_compaction(
        self,
        instructions: str | None = None,
        message_lock_timeout_seconds: float | None = None,
        expected_idle_since: datetime | None = None,
    ) -> None:
        self.lock_wait_started.set()
        with self._message_lock(timeout_seconds=message_lock_timeout_seconds):
            read_idle_since_for_compaction(self, self.name, expected_idle_since)
            self._send_message_holding_lock("/compact")
            self.compaction_count += 1


class _TmuxRecordingOnlineHost(_FakeOnlineHost):
    """A real local host that records tmux commands and answers them successfully without running them."""

    tmux_commands: list[str] = Field(default_factory=list)

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> CommandResult:
        if command.startswith("tmux "):
            self.tmux_commands.append(command)
            return CommandResult(stdout="", stderr="", success=True)
        return super().execute_stateful_command(command, user, cwd, env, timeout_seconds, on_output)


class _HangingTmuxOnlineHost(HangingTmuxHost):
    test_agents: list[Any] = Field(default_factory=list)

    def load_agents_from_refs(self, agent_refs: Sequence[DiscoveredAgent]) -> list[Any]:
        return _test_agents_for_refs(self.test_agents, agent_refs)


def _make_send_keys_compaction_agent(
    host: _FakeOnlineHost | _HangingTmuxOnlineHost,
    state_root: Path,
    name_prefix: str,
    send_command_timeout_seconds: float = SEND_COMMAND_TIMEOUT_SECONDS,
) -> _SendKeysCompactionAgent:
    """Make a stale agent on the host, discoverable through it, with its own state dir."""
    name = AgentName(f"{name_prefix}-{uuid4().hex}")
    agent_dir = state_root / name
    agent_dir.mkdir(parents=True)
    agent = _SendKeysCompactionAgent.model_construct(
        id=AgentId.generate(),
        name=name,
        agent_type=AgentTypeName("claude"),
        work_dir=state_root,
        create_time=_BY_NAME_NOW,
        host_id=host.id,
        host=host,
        mngr_ctx=host.mngr_ctx,
        agent_config=AgentTypeConfig(command=CommandString("sleep 47193")),
        send_command_timeout_seconds=send_command_timeout_seconds,
        idle_since_dt=_BY_NAME_NOW - timedelta(minutes=100),
        agent_dir=agent_dir,
        compaction_count=0,
        lock_wait_started=threading.Event(),
    )
    host.test_agents.append(agent)
    return agent


def _make_tmux_recording_host(provider: _FakeDiscoveryProvider) -> _TmuxRecordingOnlineHost:
    return _TmuxRecordingOnlineHost(
        id=HostId.generate(),
        host_name=HostName(f"online-{uuid4().hex}"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
    )


@contextmanager
def _captured_log_lines() -> Iterator[list[str]]:
    """Capture every log line at DEBUG and above as '<LEVEL> <message>'."""
    lines: list[str] = []
    sink_id = logger.add(
        lambda message: lines.append(f"{message.record['level'].name} {message.record['message']}"), level="DEBUG"
    )
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def test_compact_stale_agents_by_name_skips_an_agent_whose_message_lock_another_process_holds(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    host = _make_tmux_recording_host(provider)
    busy_agent = _make_send_keys_compaction_agent(host, tmp_path, "busy")
    idle_agent = _make_send_keys_compaction_agent(host, tmp_path, "idle")
    _add_discovered_host(provider, host, [busy_agent, idle_agent])

    with (
        _captured_log_lines() as log_lines,
        file_lock_held_by_another_process(busy_agent.agent_dir / "message.lock", temp_mngr_ctx.concurrency_group),
    ):
        started_at = time.monotonic()
        compacted = compact_stale_agents_by_name(
            provider.mngr_ctx, names=[busy_agent.name, idle_agent.name], now=_BY_NAME_NOW
        )
        elapsed_seconds = time.monotonic() - started_at

    assert compacted == [idle_agent.name]
    assert busy_agent.compaction_count == 0
    assert idle_agent.compaction_count == 1
    assert elapsed_seconds < COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS + 10.0
    busy_lines = [line for line in log_lines if str(busy_agent.name) in line and "Skipped context compaction" in line]
    assert len(busy_lines) == 1
    assert busy_lines[0].startswith("DEBUG ")
    assert "is busy" in busy_lines[0]


def test_compact_stale_agents_by_name_does_not_compact_an_agent_that_became_active_while_waiting_for_its_lock(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    host = _make_tmux_recording_host(provider)
    agent = _make_send_keys_compaction_agent(host, tmp_path, "racing")
    _add_discovered_host(provider, host, [agent])
    compacted: list[AgentName] = []

    def run_sweep() -> None:
        compacted.extend(compact_stale_agents_by_name(provider.mngr_ctx, names=[agent.name], now=_BY_NAME_NOW))

    # A user's send holds the lock while the sweep, having found the agent stale, waits for it;
    # the send starts a turn and then releases the lock.
    lock_path = agent.agent_dir / "message.lock"
    with _captured_log_lines() as log_lines, open(lock_path, "w") as user_send_lock:
        fcntl.flock(user_send_lock.fileno(), fcntl.LOCK_EX)
        sweep_thread = threading.Thread(target=run_sweep, daemon=True)
        sweep_thread.start()
        is_waiting_for_lock = agent.lock_wait_started.wait(timeout=10.0)
        (agent.agent_dir / "active").touch()
        fcntl.flock(user_send_lock.fileno(), fcntl.LOCK_UN)
        sweep_thread.join(timeout=20.0)

    assert is_waiting_for_lock is True
    assert sweep_thread.is_alive() is False
    assert compacted == []
    assert agent.compaction_count == 0
    assert host.tmux_commands == []
    skip_lines = [line for line in log_lines if str(agent.name) in line and "Skipped context compaction" in line]
    assert len(skip_lines) == 1
    assert skip_lines[0].startswith("DEBUG ")
    assert "no longer idle" in skip_lines[0]


def test_compact_stale_agents_by_name_returns_when_one_agent_tmux_hangs(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    hanging_host = _HangingTmuxOnlineHost(
        id=HostId.generate(),
        host_name=HostName(f"hanging-{uuid4().hex}"),
        connector=PyinfraConnector(provider._create_local_pyinfra_host()),
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
    )
    wedged_agent = _make_send_keys_compaction_agent(hanging_host, tmp_path, "wedged", send_command_timeout_seconds=1.0)
    _add_discovered_host(provider, hanging_host, [wedged_agent])
    healthy_host = _make_tmux_recording_host(provider)
    healthy_agent = _make_send_keys_compaction_agent(healthy_host, tmp_path, "healthy")
    _add_discovered_host(provider, healthy_host, [healthy_agent])

    started_at = time.monotonic()
    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[wedged_agent.name, healthy_agent.name], now=_BY_NAME_NOW
    )
    elapsed_seconds = time.monotonic() - started_at

    assert compacted == [healthy_agent.name]
    assert wedged_agent.compaction_count == 0
    # The wedged send runs three bounded tmux commands (pane lookup, leaving copy-mode, the keys).
    assert elapsed_seconds < 15.0


class _NeverSatisfiedScanner(CompactionTranscriptScanner):
    """Reads no values, so a scan visits every line of a transcript."""

    def visit_older_record(self, record: dict[str, Any]) -> None:
        pass


def _read_transcript_bytes(host: InMemoryHostFileReader, host_id: HostId, agent_id: AgentId, path: Path) -> int:
    """Read a transcript through the shared cache, returning how many content bytes the read took."""
    bytes_before = host.content_bytes_read
    COMPACTION_TRANSCRIPT_CACHE.read_transcript(
        host=host, host_id=host_id, agent_id=agent_id, transcript_path=path, scanner_factory=_NeverSatisfiedScanner
    )
    return host.content_bytes_read - bytes_before


def test_stale_agent_evaluation_evicts_cached_transcripts_of_agents_it_no_longer_evaluates(
    temp_mngr_ctx: MngrContext,
) -> None:
    kept_agent = _DummyCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("kept-agent"),
        agent_type=AgentTypeName("claude"),
        mngr_ctx=temp_mngr_ctx,
    )
    removed_agent_id = AgentId.generate()
    kept_path = Path(f"/host/agents/{kept_agent.id}/transcript.jsonl")
    removed_path = Path(f"/host/agents/{removed_agent_id}/transcript.jsonl")
    line = b'{"type": "user"}\n'
    host = InMemoryHostFileReader(contents_by_path={kept_path: line, removed_path: line})
    host_id = HostId.generate()
    _read_transcript_bytes(host, host_id, kept_agent.id, kept_path)
    _read_transcript_bytes(host, host_id, removed_agent_id, removed_path)

    get_stale_agents(temp_mngr_ctx, agents=[cast(Any, kept_agent)])

    assert _read_transcript_bytes(host, host_id, kept_agent.id, kept_path) == 0
    assert _read_transcript_bytes(host, host_id, removed_agent_id, removed_path) == len(line)


class _CompactionAwareScanner(CompactionTranscriptScanner):
    """Reads "usage" records, treating a newer "compaction" record as leaving the context size unknown."""

    def visit_older_record(self, record: dict[str, Any]) -> None:
        if record.get("type") == "compaction" and not self.is_context_size_settled():
            self.is_context_size_unknown_since_compaction = True
        if record.get("type") == "usage":
            if self.latest_assistant_timestamp is None:
                self.latest_assistant_timestamp = datetime.fromisoformat(record["timestamp"])
            if not self.is_context_size_settled():
                self.latest_context_tokens = record["tokens"]


class _TranscriptReadingCompactionAgent(_DummyCompactionAgent):
    """Compaction agent whose context size is read from its transcript through the shared cache, as a harness does."""

    def __init__(
        self, provider: _FakeDiscoveryProvider, transcript_host: InMemoryHostFileReader, transcript: bytes
    ) -> None:
        super().__init__(
            id=AgentId.generate(),
            name=AgentName(f"transcript-{uuid4().hex}"),
            agent_type=AgentTypeName("claude"),
            mngr_ctx=provider.mngr_ctx,
            idle_since_dt=_BY_NAME_NOW - timedelta(minutes=100),
        )
        self.transcript_host = transcript_host
        self.transcript_host_id = HostId.generate()
        self.transcript_path = Path(f"/host/agents/{self.id}/transcript.jsonl")
        transcript_host.contents_by_path[self.transcript_path] = transcript

    def get_context_tokens(self) -> int | None:
        reading = COMPACTION_TRANSCRIPT_CACHE.read_transcript(
            host=self.transcript_host,
            host_id=self.transcript_host_id,
            agent_id=self.id,
            transcript_path=self.transcript_path,
            scanner_factory=_CompactionAwareScanner,
        )
        return None if reading is None else reading.latest_context_tokens


def test_compact_stale_agents_by_name_leaves_alone_an_agent_compacted_since_its_last_usage(
    temp_mngr_ctx: MngrContext,
) -> None:
    provider = _register_by_name_discovery_provider(temp_mngr_ctx)
    transcript_host = InMemoryHostFileReader()
    usage_line = (
        json.dumps(
            {
                "type": "usage",
                "timestamp": "2026-08-27T12:00:00+00:00",
                "tokens": _INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS,
            }
        ).encode()
        + b"\n"
    )
    compaction_line = json.dumps({"type": "compaction"}).encode() + b"\n"
    uncompacted_agent = _TranscriptReadingCompactionAgent(provider, transcript_host, usage_line)
    compacted_agent = _TranscriptReadingCompactionAgent(provider, transcript_host, usage_line + compaction_line)
    agents = [uncompacted_agent, compacted_agent]
    _add_discovered_host(provider, _make_online_host(provider, agents), agents)

    compacted = compact_stale_agents_by_name(
        provider.mngr_ctx, names=[uncompacted_agent.name, compacted_agent.name], now=_BY_NAME_NOW
    )

    assert compacted == [uncompacted_agent.name]
    assert uncompacted_agent.compaction_count == 1
    assert compacted_agent.compaction_count == 0


def test_run_workers_warns_once_naming_the_unfinished_workers_and_waits_for_them(temp_mngr_ctx: MngrContext) -> None:
    release_slow_workers = threading.Event()

    def work(item: str) -> str:
        if item.startswith("slow"):
            release_slow_workers.wait(timeout=30.0)
        return item.upper()

    # The warning releases the slow workers, so the results show the pass waited for them
    release_sink_id = logger.add(
        lambda _message: release_slow_workers.set(),
        level="WARNING",
        filter=lambda record: record["message"].startswith("Autocompact workers are still unfinished"),
    )
    try:
        with _captured_log_lines() as log_lines:
            results = manager_module._run_workers(
                mngr_ctx=temp_mngr_ctx,
                executor_name=f"slow-worker-test-{uuid4().hex}",
                items=["quick", "slow-one", "slow-two"],
                work=work,
                describe_item=lambda item: f"agent {item}",
                slow_worker_warning_seconds=1.0,
            )
    finally:
        logger.remove(release_sink_id)
        release_slow_workers.set()

    assert results == ["QUICK", "SLOW-ONE", "SLOW-TWO"]
    assert [line for line in log_lines if line.startswith(("WARNING ", "ERROR "))] == [
        "WARNING Autocompact workers are still unfinished after 1s for: agent slow-one, agent slow-two. "
        "This pass keeps waiting for them"
    ]
