from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import cast

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupState
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.mngr.api import providers as providers_module
from imbue.mngr.api.providers import reset_provider_instances
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import CertifiedHostData
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_autocompact import manager as manager_module
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode
from imbue.mngr_autocompact.manager import compact_agent_if_stale
from imbue.mngr_autocompact.manager import compact_all_agents
from imbue.mngr_autocompact.manager import get_stale_agents
from imbue.mngr_autocompact.manager import is_agent_stale_for_compaction
from imbue.mngr_autocompact.manager import trigger_compaction


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
        context_tokens: int | None = 150_000,
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

    def is_running(self) -> bool:
        if self.raise_on_is_running is not None:
            raise self.raise_on_is_running
        return self.running

    def request_compaction(self, instructions: str | None = None) -> None:
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
        context_tokens=150_000,
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
        context_tokens=150_000,
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
        context_tokens=150_000,
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
        context_tokens=150_000,
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
        context_tokens=150_000,
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
        context_tokens=150_000,
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
        context_tokens=150_000,
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


def test_compact_all_agents_empty(temp_mngr_ctx: MngrContext) -> None:
    compacted = compact_all_agents(temp_mngr_ctx)
    assert compacted == []


class _FakeOnlineHost(Host):
    test_agents: list[Any] = Field(default_factory=list)

    def get_agents(self) -> list[Any]:
        return self.test_agents


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
        context_tokens=150_000,
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
            context_tokens=150_000,
            idle_since_dt=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert not is_agent_stale_for_compaction(
            cast(Any, agent), config, expected_mode=ContextCompactionMode.PROACTIVE_TIMER, now=now
        )


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


def test_get_stale_agents_and_compact_all_agents(temp_mngr_ctx: MngrContext) -> None:
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
        stale_names = get_stale_agents(mngr_ctx, now=now)
        assert stale_names == [AgentName("stale-agent")]

        compacted_names = compact_all_agents(mngr_ctx, now=now)
        assert compacted_names == [AgentName("stale-agent")]
        assert stale_agent.compaction_count == 1
        assert on_prompt_agent.compaction_count == 0
    finally:
        reset_provider_instances()
