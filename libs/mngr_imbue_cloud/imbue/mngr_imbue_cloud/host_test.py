import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr_imbue_cloud.host import build_combined_inject_command
from imbue.mngr_imbue_cloud.host import normalize_inject_args


def test_build_combined_inject_command_minimal() -> None:
    agent_id = AgentId.generate()
    cmd = build_combined_inject_command(
        agent_id=agent_id,
        agent_env_path="/mngr/agents/X/env",
        host_env_path="/mngr/env",
        minds_api_key="secret-api-key",
        anthropic_api_key=None,
        anthropic_base_url=None,
        mngr_prefix=None,
    )
    assert cmd is not None
    assert "MINDS_API_KEY=secret-api-key" in cmd
    assert "ANTHROPIC_API_KEY" not in cmd
    assert "MNGR_PREFIX" not in cmd


def test_build_combined_inject_command_returns_none_when_nothing() -> None:
    agent_id = AgentId.generate()
    cmd = build_combined_inject_command(
        agent_id=agent_id,
        agent_env_path="/x",
        host_env_path="/y",
        minds_api_key=None,
        anthropic_api_key=None,
        anthropic_base_url=None,
        mngr_prefix=None,
    )
    assert cmd is None


def test_build_combined_inject_command_chains_with_ampersand() -> None:
    agent_id = AgentId.generate()
    cmd = build_combined_inject_command(
        agent_id=agent_id,
        agent_env_path="/mngr/agents/X/env",
        host_env_path="/mngr/env",
        minds_api_key="k1",
        anthropic_api_key="k2",
        anthropic_base_url="https://example.com",
        mngr_prefix="mngr-",
    )
    assert cmd is not None
    # Should have multiple steps joined by &&
    assert cmd.count(" && ") >= 3


def test_normalize_inject_args_rejects_newlines() -> None:
    with pytest.raises(ValueError):
        normalize_inject_args(
            minds_api_key="evil\ninjection",
            anthropic_api_key=None,
            anthropic_base_url=None,
            mngr_prefix=None,
            extra_env=None,
        )


def test_normalize_inject_args_rejects_invalid_env_keys() -> None:
    with pytest.raises(ValueError):
        normalize_inject_args(
            minds_api_key=None,
            anthropic_api_key=None,
            anthropic_base_url=None,
            mngr_prefix=None,
            extra_env={"BAD=KEY": "value"},
        )


def test_normalize_inject_args_passes_clean_input_through() -> None:
    cleaned = normalize_inject_args(
        minds_api_key="abc",
        anthropic_api_key=None,
        anthropic_base_url=None,
        mngr_prefix="mngr-",
        extra_env={"FOO": "bar"},
    )
    assert cleaned["minds_api_key"] == "abc"
    assert cleaned["mngr_prefix"] == "mngr-"
    assert cleaned["extra_env"] == {"FOO": "bar"}
