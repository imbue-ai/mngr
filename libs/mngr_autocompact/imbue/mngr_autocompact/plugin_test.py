from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import get_plugin_config_class
from imbue.mngr.interfaces.agent import HasCompactionMixin
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import PluginName
from imbue.mngr_autocompact.cli import autocompact_group
from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode
from imbue.mngr_autocompact.plugin import on_before_send_message
from imbue.mngr_autocompact.plugin import register_cli_commands


class _DummyNonCompactionAgent:
    def __init__(self, id: AgentId, name: AgentName, agent_type: AgentTypeName, running: bool = True) -> None:
        self.id = id
        self.name = name
        self.agent_type = agent_type
        self.running = running

    def is_running(self) -> bool:
        return self.running


class _TestAgent(HasCompactionMixin):
    def __init__(
        self,
        id: AgentId,
        name: AgentName,
        agent_type: AgentTypeName,
        mngr_ctx: MngrContext,
        host: FakeHost,
        running: bool = True,
        cache_ttl: int | None = 60,
        context_tokens: int | None = 150_000,
        idle_since_dt: datetime | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.agent_type = agent_type
        self.mngr_ctx = mngr_ctx
        self.host = host
        self.running = running
        self.cache_ttl = cache_ttl
        self.context_tokens = context_tokens
        self.idle_since_dt = idle_since_dt
        self.compaction_count = 0

    def is_running(self) -> bool:
        return self.running

    def request_compaction(self, instructions: str | None = None) -> None:
        self.compaction_count += 1
        self.idle_since_dt = None

    def get_cache_ttl_minutes(self) -> int | None:
        return self.cache_ttl

    def get_context_tokens(self) -> int | None:
        return self.context_tokens

    def get_idle_since(self) -> datetime | None:
        return self.idle_since_dt


def test_plugin_config_is_registered() -> None:
    config_class = get_plugin_config_class("autocompact")
    assert config_class is AutoCompactPluginConfig


def test_register_cli_commands() -> None:
    commands = register_cli_commands()
    assert len(commands) == 1
    assert commands[0] is autocompact_group


def test_on_before_send_message_triggers_compaction_if_stale(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = FakeHost(host_dir=tmp_path)
    plugin_config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.ON_NEXT_PROMPT,
        cache_ttl_minutes=60,
        epsilon_offset_minutes=2,
    )
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("autocompact"): plugin_config})
    )
    ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))

    # Idle for 2 hours -> stale
    agent = _TestAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("test"),
        mngr_ctx=ctx,
        host=host,
        running=True,
        cache_ttl=60,
        context_tokens=150_000,
        idle_since_dt=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    on_before_send_message(cast(Any, agent), cast(Any, host), "hello")
    assert agent.compaction_count == 1


def test_on_before_send_message_skips_when_not_stale(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = FakeHost(host_dir=tmp_path)
    plugin_config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.ON_NEXT_PROMPT,
        cache_ttl_minutes=60,
        epsilon_offset_minutes=2,
    )
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("autocompact"): plugin_config})
    )
    ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))

    # Idle for only 5 minutes -> not stale
    agent = _TestAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("test"),
        mngr_ctx=ctx,
        host=host,
        running=True,
        cache_ttl=60,
        context_tokens=150_000,
        idle_since_dt=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    on_before_send_message(cast(Any, agent), cast(Any, host), "hello")
    assert agent.compaction_count == 0


def test_on_before_send_message_skips_when_disabled(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = FakeHost(host_dir=tmp_path)
    plugin_config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.DISABLED,
        cache_ttl_minutes=60,
        epsilon_offset_minutes=2,
    )
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("autocompact"): plugin_config})
    )
    ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))

    agent = _TestAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("test"),
        mngr_ctx=ctx,
        host=host,
        running=True,
        cache_ttl=60,
        context_tokens=150_000,
        idle_since_dt=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    on_before_send_message(cast(Any, agent), cast(Any, host), "hello")
    assert agent.compaction_count == 0


def test_on_before_send_message_skips_when_proactive_timer(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = FakeHost(host_dir=tmp_path)
    plugin_config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        cache_ttl_minutes=60,
        epsilon_offset_minutes=2,
    )
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("autocompact"): plugin_config})
    )
    ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))

    agent = _TestAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("test"),
        mngr_ctx=ctx,
        host=host,
        running=True,
        cache_ttl=60,
        context_tokens=150_000,
        idle_since_dt=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    on_before_send_message(cast(Any, agent), cast(Any, host), "hello")
    assert agent.compaction_count == 0


def test_on_before_send_message_non_compaction_agent(tmp_path: Path) -> None:
    host = FakeHost(host_dir=tmp_path)
    agent = _DummyNonCompactionAgent(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("other"),
        running=True,
    )
    # Should safely return without error
    on_before_send_message(cast(Any, agent), cast(Any, host), "hello")
