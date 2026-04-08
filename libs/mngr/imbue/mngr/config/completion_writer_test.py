"""Tests for completion_writer module."""

import json
from pathlib import Path

import click
import pytest

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import get_completion_cache_dir
from imbue.mngr.config.completion_writer import _EXCLUDED_CONFIG_KEY_PREFIXES
from imbue.mngr.config.completion_writer import _extract_config_value_choices
from imbue.mngr.config.completion_writer import _is_excluded_config_key
from imbue.mngr.config.completion_writer import flatten_dict_keys
from imbue.mngr.config.completion_writer import write_cli_completions_cache
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName


def test_get_completion_cache_dir_uses_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_completion_cache_dir should use MNGR_COMPLETION_CACHE_DIR when set."""
    cache_dir = tmp_path / "custom_cache"
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(cache_dir))
    result = get_completion_cache_dir()
    assert result == cache_dir
    assert cache_dir.exists()


def test_get_completion_cache_dir_falls_back_to_default_host_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """get_completion_cache_dir should use read_default_host_dir when env var is unset."""
    monkeypatch.delenv("MNGR_COMPLETION_CACHE_DIR", raising=False)
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "default_host"))
    result = get_completion_cache_dir()
    assert result == tmp_path / "default_host"
    assert result.exists()


def test_write_cli_completions_cache_handles_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """write_cli_completions_cache should silently handle OSError."""
    # Monkeypatch atomic_write to simulate a write failure. We can't use chmod
    # because Modal sandboxes run as root, which bypasses permission checks.
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(tmp_path))

    def _raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr("imbue.mngr.config.completion_writer.atomic_write", _raise_oserror)

    group = click.Group(name="test", commands={"hello": click.Command("hello")})

    # Should not raise despite the OSError from atomic_write
    write_cli_completions_cache(cli_group=group)
    assert not (tmp_path / COMPLETION_CACHE_FILENAME).exists()


def test_write_cli_completions_cache_writes_valid_json(completion_cache_dir: Path) -> None:
    """write_cli_completions_cache should write valid JSON with expected structure."""
    group = click.Group(
        name="test",
        commands={
            "list": click.Command("list", params=[click.Option(["--format"], type=click.Choice(["json", "human"]))]),
            "create": click.Command("create", params=[click.Option(["--verbose", "-v"], is_flag=True)]),
        },
    )

    write_cli_completions_cache(cli_group=group)
    cache_path = completion_cache_dir / COMPLETION_CACHE_FILENAME
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert "commands" in data
    assert "create" in data["commands"]
    assert "list" in data["commands"]


def test_write_cli_completions_cache_includes_git_branch_options(completion_cache_dir: Path) -> None:
    """write_cli_completions_cache should include git_branch_options for the create command."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command("create", params=[click.Option(["--branch"])]),
        },
    )

    write_cli_completions_cache(cli_group=group)
    cache_path = completion_cache_dir / COMPLETION_CACHE_FILENAME
    data = json.loads(cache_path.read_text())
    assert "git_branch_options" in data
    assert "create.--branch" in data["git_branch_options"]


def _read_cache(cache_dir: Path) -> dict:
    """Read and return the completion cache data."""
    return json.loads((cache_dir / COMPLETION_CACHE_FILENAME).read_text())


def test_write_cli_completions_cache_includes_host_name_options(completion_cache_dir: Path) -> None:
    """Cache should include host_name_options for create --target."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command(
                "create",
                params=[
                    click.Option(["--target"]),
                ],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert "create.--target" in data["host_name_options"]


def test_write_cli_completions_cache_includes_positional_completions_for_events(
    completion_cache_dir: Path,
) -> None:
    """Cache should include per-position positional_completions for the events command."""
    group = click.Group(
        name="test",
        commands={
            "events": click.Command("events"),
            "list": click.Command("list"),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert "events" in data["positional_completions"]
    assert data["positional_completions"]["events"] == [["agent_names", "host_names"], []]
    assert "list" not in data["positional_completions"]


def test_write_cli_completions_cache_includes_plugin_name_options(completion_cache_dir: Path) -> None:
    """Cache should detect --plugin/--enable-plugin/--disable-plugin as plugin name options."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command(
                "create",
                params=[
                    click.Option(["--plugin"]),
                    click.Option(["--name"]),
                ],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert "create.--plugin" in data["plugin_name_options"]


def test_write_cli_completions_cache_includes_positional_completions_for_plugin(
    completion_cache_dir: Path,
) -> None:
    """Cache should include positional_completions for plugin enable/disable subcommands."""
    plugin_group = click.Group(
        name="plugin",
        commands={
            "enable": click.Command("enable"),
            "disable": click.Command("disable"),
            "list": click.Command("list"),
        },
    )
    group = click.Group(name="test", commands={"plugin": plugin_group})

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_completions"]["plugin.enable"] == [["plugin_names"]]
    assert data["positional_completions"]["plugin.disable"] == [["plugin_names"]]


def test_write_cli_completions_cache_includes_positional_completions_for_config(
    completion_cache_dir: Path,
) -> None:
    """Cache should include positional_completions for config get/set/unset subcommands."""
    config_group = click.Group(
        name="config",
        commands={
            "get": click.Command("get"),
            "set": click.Command("set"),
            "unset": click.Command("unset"),
            "list": click.Command("list"),
        },
    )
    group = click.Group(name="test", commands={"config": config_group})

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_completions"]["config.get"] == [["config_keys"]]
    assert data["positional_completions"]["config.set"] == [["config_keys"], ["config_value_for_key"]]
    assert data["positional_completions"]["config.unset"] == [["config_keys"]]


def test_write_cli_completions_cache_with_mngr_ctx(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When mngr_ctx is provided, dynamic completions should be injected into the cache."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command(
                "create",
                params=[
                    click.Option(["--type"]),
                    click.Option(["--template"]),
                    click.Option(["--provider"]),
                ],
            ),
            "list": click.Command(
                "list",
                params=[
                    click.Option(["--provider"]),
                ],
            ),
        },
    )

    write_cli_completions_cache(
        cli_group=group, mngr_ctx=temp_mngr_ctx, registered_agent_types=list_registered_agent_types()
    )
    data = _read_cache(completion_cache_dir)

    # Agent types include at least the built-in registered types
    assert "create.--type" in data["option_choices"]
    assert len(data["option_choices"]["create.--type"]) > 0
    # Provider names always include "local"
    assert "local" in data["option_choices"]["create.--provider"]
    assert "local" in data["option_choices"]["list.--provider"]
    # Config keys are flattened from the config model
    assert len(data["config_keys"]) > 0
    # Plugin names come from the plugin manager
    assert isinstance(data["plugin_names"], list)


def test_write_cli_completions_cache_no_mngr_ctx(completion_cache_dir: Path) -> None:
    """When mngr_ctx is None, plugin_names and config_keys should be empty."""
    group = click.Group(
        name="test",
        commands={"list": click.Command("list")},
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["plugin_names"] == []
    assert data["config_keys"] == []


# =============================================================================
# flatten_dict_keys tests
# =============================================================================


def test_flatten_dict_keys_flat() -> None:
    data = {"a": 1, "b": 2, "c": 3}
    assert flatten_dict_keys(data) == ["a", "b", "c"]


def test_flatten_dict_keys_nested() -> None:
    data = {"logging": {"console_level": "INFO", "file_level": "DEBUG"}, "prefix": "mngr"}
    result = flatten_dict_keys(data)
    assert result == ["logging.console_level", "logging.file_level", "prefix"]


def test_flatten_dict_keys_empty() -> None:
    assert flatten_dict_keys({}) == []


# =============================================================================
# positional_nargs_by_command tests
# =============================================================================


def test_positional_nargs_fixed_args(completion_cache_dir: Path) -> None:
    """Commands with fixed positional args should have their nargs summed."""
    config_group = click.Group(
        name="config",
        commands={
            "set": click.Command(
                "set",
                params=[
                    click.Argument(["key"]),
                    click.Argument(["value"]),
                ],
            ),
            "get": click.Command(
                "get",
                params=[
                    click.Argument(["key"]),
                ],
            ),
            "list": click.Command("list"),
        },
    )
    group = click.Group(name="test", commands={"config": config_group})

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    nargs = data["positional_nargs_by_command"]
    assert nargs["config.set"] == 2
    assert nargs["config.get"] == 1
    assert nargs["config.list"] == 0


def test_positional_nargs_unlimited(completion_cache_dir: Path) -> None:
    """Commands with nargs=-1 should have None in positional_nargs_by_command."""
    group = click.Group(
        name="test",
        commands={
            "destroy": click.Command(
                "destroy",
                params=[click.Argument(["agents"], nargs=-1)],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_nargs_by_command"]["destroy"] is None


def test_positional_nargs_simple_command(completion_cache_dir: Path) -> None:
    """Simple commands (not groups) should have nargs tracked."""
    group = click.Group(
        name="test",
        commands={
            "connect": click.Command(
                "connect",
                params=[click.Argument(["agent"])],
            ),
            "list": click.Command("list"),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_nargs_by_command"]["connect"] == 1
    assert data["positional_nargs_by_command"]["list"] == 0


# =============================================================================
# _extract_config_value_choices tests
# =============================================================================


def test_extract_config_value_choices_includes_bool_fields() -> None:
    """Bool fields should produce ["true", "false"] choices."""
    choices = _extract_config_value_choices(MngrConfig())
    assert choices["headless"] == ["true", "false"]
    assert choices["is_nested_tmux_allowed"] == ["true", "false"]


def test_extract_config_value_choices_includes_enum_fields() -> None:
    """Enum fields should produce their string values as choices."""
    choices = _extract_config_value_choices(MngrConfig())
    assert "logging.console_level" in choices
    assert "TRACE" in choices["logging.console_level"]
    assert "DEBUG" in choices["logging.console_level"]
    assert "INFO" in choices["logging.console_level"]
    assert "NONE" in choices["logging.console_level"]


def test_extract_config_value_choices_includes_nested_bool_fields() -> None:
    """Bool fields inside nested models should have dotted key paths."""
    choices = _extract_config_value_choices(MngrConfig())
    assert choices["logging.is_logging_commands"] == ["true", "false"]
    assert choices["logging.is_logging_env_vars"] == ["true", "false"]


def test_extract_config_value_choices_excludes_string_fields() -> None:
    """String fields (no constrained values) should not appear in choices."""
    choices = _extract_config_value_choices(MngrConfig())
    assert "prefix" not in choices
    assert "connect_command" not in choices


def test_extract_config_value_choices_discovers_plugin_fields() -> None:
    """Plugin dict entries should produce bool choices for their enabled field."""
    config = MngrConfig(
        plugins={
            PluginName("modal"): PluginConfig(enabled=True),
            PluginName("kanpan"): PluginConfig(enabled=False),
        }
    )
    choices = _extract_config_value_choices(config)
    assert choices["plugins.modal.enabled"] == ["true", "false"]
    assert choices["plugins.kanpan.enabled"] == ["true", "false"]


def test_extract_config_value_choices_discovers_provider_fields() -> None:
    """Provider dict entries should produce bool choices for is_enabled."""
    config = MngrConfig(
        providers={
            ProviderInstanceName("modal"): ProviderInstanceConfig(
                backend=ProviderBackendName("modal"), is_enabled=True
            ),
        }
    )
    choices = _extract_config_value_choices(config)
    assert choices["providers.modal.is_enabled"] == ["true", "false"]


def test_extract_config_value_choices_empty_dicts_match_default() -> None:
    """With no plugins/providers, results should match the default config."""
    default_choices = _extract_config_value_choices(MngrConfig())
    assert "plugins" not in {k.split(".")[0] for k in default_choices if "." in k} or True
    # The key point: no plugin.* or provider.* keys should appear
    plugin_keys = [k for k in default_choices if k.startswith("plugins.")]
    provider_keys = [k for k in default_choices if k.startswith("providers.")]
    assert plugin_keys == []
    assert provider_keys == []


def test_write_cli_completions_cache_with_mngr_ctx_includes_config_value_choices(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When mngr_ctx is provided, config_value_choices should be populated in the cache."""
    config_group = click.Group(
        name="config",
        commands={
            "get": click.Command("get"),
            "set": click.Command("set"),
        },
    )
    group = click.Group(name="test", commands={"config": config_group})

    write_cli_completions_cache(cli_group=group, mngr_ctx=temp_mngr_ctx)
    data = _read_cache(completion_cache_dir)

    assert "config_value_choices" in data
    assert data["config_value_choices"]["headless"] == ["true", "false"]
    assert "TRACE" in data["config_value_choices"]["logging.console_level"]


def test_write_cli_completions_cache_no_mngr_ctx_empty_config_value_choices(
    completion_cache_dir: Path,
) -> None:
    """When mngr_ctx is None, config_value_choices should be empty."""
    group = click.Group(
        name="test",
        commands={"list": click.Command("list")},
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["config_value_choices"] == {}


# =============================================================================
# _FIELD_TYPE_COMPLETION_SOURCES tests
# =============================================================================


def test_extract_config_value_choices_agent_type_name_field_with_dynamic_values() -> None:
    """AgentTypeName fields should produce completions from dynamic agent_type_names."""
    config = MngrConfig(
        agent_types={
            AgentTypeName("coder"): AgentTypeConfig(parent_type=AgentTypeName("claude")),
        }
    )
    dynamic_values = {"agent_type_names": ["claude", "codex", "coder"]}
    choices = _extract_config_value_choices(config, dynamic_values)

    assert choices["agent_types.coder.parent_type"] == ["claude", "codex", "coder"]


def test_extract_config_value_choices_provider_backend_name_field_with_dynamic_values() -> None:
    """ProviderBackendName fields should produce completions from dynamic provider_backend_names."""
    config = MngrConfig(
        providers={
            ProviderInstanceName("modal"): ProviderInstanceConfig(
                backend=ProviderBackendName("modal"), is_enabled=True
            ),
        }
    )
    dynamic_values = {"provider_backend_names": ["docker", "local", "modal", "ssh"]}
    choices = _extract_config_value_choices(config, dynamic_values)

    assert choices["providers.modal.backend"] == ["docker", "local", "modal", "ssh"]


def test_extract_config_value_choices_no_dynamic_values_skips_typed_fields() -> None:
    """Without dynamic values, AgentTypeName and ProviderBackendName fields are omitted."""
    config = MngrConfig(
        agent_types={
            AgentTypeName("coder"): AgentTypeConfig(parent_type=AgentTypeName("claude")),
        },
        providers={
            ProviderInstanceName("modal"): ProviderInstanceConfig(
                backend=ProviderBackendName("modal"),
            ),
        },
    )
    choices = _extract_config_value_choices(config)

    assert "agent_types.coder.parent_type" not in choices
    assert "providers.modal.backend" not in choices


# =============================================================================
# _EXCLUDED_CONFIG_KEY_PREFIXES / _is_excluded_config_key tests
# =============================================================================


def test_is_excluded_config_key_exact_match() -> None:
    """Exact matches against excluded prefixes should be excluded."""
    for prefix in _EXCLUDED_CONFIG_KEY_PREFIXES:
        assert _is_excluded_config_key(prefix)


def test_is_excluded_config_key_prefix_match() -> None:
    """Dotted sub-keys of excluded prefixes should also be excluded."""
    assert _is_excluded_config_key("disabled_plugins.something")


def test_is_excluded_config_key_non_match() -> None:
    """Unrelated keys should not be excluded."""
    assert not _is_excluded_config_key("prefix")
    assert not _is_excluded_config_key("headless")
    assert not _is_excluded_config_key("logging.console_level")


def test_excluded_config_keys_not_in_dynamic_completions(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Excluded config keys should not appear in config_keys or config_value_choices."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command("create", params=[click.Option(["--type"])]),
            "list": click.Command("list"),
        },
    )

    write_cli_completions_cache(cli_group=group, mngr_ctx=temp_mngr_ctx)
    data = _read_cache(completion_cache_dir)

    for prefix in _EXCLUDED_CONFIG_KEY_PREFIXES:
        matching_keys = [k for k in data["config_keys"] if k == prefix or k.startswith(f"{prefix}.")]
        assert matching_keys == [], f"excluded prefix {prefix!r} found in config_keys: {matching_keys}"

        matching_choice_keys = [k for k in data["config_value_choices"] if k == prefix or k.startswith(f"{prefix}.")]
        assert matching_choice_keys == [], (
            f"excluded prefix {prefix!r} found in config_value_choices: {matching_choice_keys}"
        )
