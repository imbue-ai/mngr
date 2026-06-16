"""Unit tests for the core, agent-agnostic ``--adopt-session`` wiring."""

from pathlib import Path

import pytest

from imbue.mngr.agents.builtin_adopt_session import on_before_create
from imbue.mngr.agents.builtin_adopt_session import register_cli_options
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_register_cli_options_returns_adopt_session_for_create() -> None:
    result = register_cli_options(command_name="create")
    assert result is not None
    assert "Behavior" in result
    options = result["Behavior"]
    assert len(options) == 1
    assert "--adopt-session" in options[0].param_decls


def test_register_cli_options_returns_none_for_other_commands() -> None:
    assert register_cli_options(command_name="connect") is None
    assert register_cli_options(command_name="list") is None


def test_on_before_create_skips_when_no_adopt_session(temp_mngr_ctx: MngrContext) -> None:
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(agent_type=AgentTypeName("claude")),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    assert on_before_create(args=args, mngr_ctx=temp_mngr_ctx) is None


def test_on_before_create_passes_for_adoption_capable_agent(temp_mngr_ctx: MngrContext) -> None:
    """The agent-agnostic gate passes for an adoption-capable type (claude); the
    claude-specific session pre-resolution lives in claude's own on_before_create."""
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(
            agent_type=AgentTypeName("claude"),
            plugin_data={"adopt_session": ("some-id",)},
        ),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    assert on_before_create(args=args, mngr_ctx=temp_mngr_ctx) is None


def test_on_before_create_rejects_agent_without_adoption_support(temp_mngr_ctx: MngrContext) -> None:
    """A registered agent type that does not support session adoption is rejected."""
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(
            agent_type=AgentTypeName("command"),
            plugin_data={"adopt_session": ("some-id",)},
        ),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    with pytest.raises(UserInputError, match="supports session adoption"):
        on_before_create(args=args, mngr_ctx=temp_mngr_ctx)


def test_on_before_create_rejects_adopt_session_with_clone_source(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """``--adopt-session`` combined with a clone source (``--from``) is rejected:
    each is its own session-adoption directive."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    args = OnBeforeCreateArgs(
        agent_options=CreateAgentOptions(
            agent_type=AgentTypeName("claude"),
            plugin_data={"adopt_session": ("some-id",)},
            source_agent_state_location=HostLocation(host=host, path=tmp_path / "src"),
        ),
        target_host=NewHostOptions(provider=ProviderInstanceName("local")),
        create_work_dir=True,
    )
    with pytest.raises(UserInputError, match="incompatible with cloning via --from"):
        on_before_create(args=args, mngr_ctx=temp_mngr_ctx)
