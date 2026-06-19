"""Tests for config loader."""

from pathlib import Path
from typing import Any

import click
import pluggy
import pytest
from click.core import ParameterSource
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import apply_create_template
from imbue.mngr.config.agent_alias_registry import is_agent_alias
from imbue.mngr.config.agent_alias_registry import normalize_agent_type_name
from imbue.mngr.config.agent_alias_registry import register_agent_alias
from imbue.mngr.config.agent_alias_registry import reset_agent_alias_registry
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import TmuxConfig
from imbue.mngr.config.data_types import get_or_create_user_id
from imbue.mngr.config.loader import _FileSettingsSource
from imbue.mngr.config.loader import _NarrowingViolation
from imbue.mngr.config.loader import _SettingsSource
from imbue.mngr.config.loader import _apply_plugin_overrides
from imbue.mngr.config.loader import _collect_env_overrides
from imbue.mngr.config.loader import _collect_narrowing
from imbue.mngr.config.loader import _display_path
from imbue.mngr.config.loader import _normalize_tuple_fields_for_construct
from imbue.mngr.config.loader import _parse_agent_types
from imbue.mngr.config.loader import _parse_commands
from imbue.mngr.config.loader import _parse_create_templates
from imbue.mngr.config.loader import _parse_logging_config
from imbue.mngr.config.loader import _parse_mngr_env_overrides
from imbue.mngr.config.loader import _parse_plugins
from imbue.mngr.config.loader import _parse_providers
from imbue.mngr.config.loader import _parse_tmux_config
from imbue.mngr.config.loader import _record_provenance
from imbue.mngr.config.loader import block_disabled_plugins
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.config.loader import load_config
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.plugin_registry import _plugin_config_registry
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.config.pre_readers import OPT_IN_PLUGINS
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.registry import load_all_registries
from imbue.mngr.utils.logging import LoggingConfig
from imbue.overlay.markers import ScalarTuple

hookimpl = pluggy.HookimplMarker("mngr")


def _isolate_load_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the autouse ``setup_test_mngr_env`` fixture's MNGR_* settings so
    ``load_config`` resolves ``~/.mngr`` to ``tmp_path/.mngr`` (the user/profile
    config base) instead of the fixture-supplied host dir / prefix / root, and so
    ``root_name`` collapses to ``"mngr"`` (making the project config dir
    ``<git-root>/.mngr/``).

    Tests using this helper pair it with the ``temp_git_repo_cwd`` fixture, which
    chdirs into an isolated empty git repo so the loader's git-worktree-root walk
    resolves the project config dir to that repo (and not the developer's real
    checkout). Project/local config is written under ``<git-root>/.mngr/`` (or via
    ``MNGR_PROJECT_CONFIG_DIR``); the user/profile config stays HOME-based.

    HOME is already pointed at ``tmp_path`` by the autouse fixture (via
    ``isolate_home``), so no ``setenv("HOME", ...)`` is needed here. Tests with
    extra env tweaks (MNGR_HEADLESS, MNGR_ALLOW_UNKNOWN_CONFIG, MNGR__*,
    MNGR_PROJECT_CONFIG_DIR, PYTEST_CURRENT_TEST) apply those inline.
    """
    monkeypatch.delenv("MNGR_PREFIX", raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_ROOT_NAME", raising=False)


# =============================================================================
# Tests for _parse_mngr_env_overrides / _collect_env_overrides
# =============================================================================


def test_parse_mngr_env_overrides_builds_nested_dict() -> None:
    """A MNGR__X__Y env var lands at the nested path x.y in the raw dict."""
    environ = {"MNGR__COMMANDS__CREATE__BRANCH": "main:mngr/*"}
    raw = _parse_mngr_env_overrides(environ)
    assert raw == {"commands": {"create": {"branch": "main:mngr/*"}}}


def test_parse_mngr_env_overrides_json_parses_value() -> None:
    """Values are JSON-parsed first with raw-string fallback (matches --setting)."""
    environ = {
        "MNGR__COMMANDS__CREATE__CONNECT": "false",
        "MNGR__COMMANDS__CREATE__RETRY": "5",
        "MNGR__COMMANDS__CREATE__NAME": "myagent",
    }
    raw = _parse_mngr_env_overrides(environ)
    assert raw["commands"]["create"]["connect"] is False
    assert raw["commands"]["create"]["retry"] == 5
    assert raw["commands"]["create"]["name"] == "myagent"


def test_parse_mngr_env_overrides_skips_unrelated_vars() -> None:
    """Vars not matching the MNGR__ prefix are skipped."""
    environ = {
        "MNGR__COMMANDS__CREATE__NAME": "myagent",
        "MNGR_PREFIX": "test-",
        "PATH": "/usr/bin",
    }
    raw = _parse_mngr_env_overrides(environ)
    assert raw == {"commands": {"create": {"name": "myagent"}}}


def test_parse_mngr_env_overrides_rejects_mixed_case() -> None:
    """Lowercase letters in the segment portion mean the var is not recognized."""
    environ = {"MNGR__commands__create__name": "lowercase"}
    raw = _parse_mngr_env_overrides(environ)
    assert raw == {}


def test_parse_mngr_env_overrides_handles_extend_suffix() -> None:
    """A trailing __EXTEND collapses into a key__extend suffix at the leaf."""
    environ = {"MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS__EXTEND": '["--foo"]'}
    raw = _parse_mngr_env_overrides(environ)
    assert raw == {"agent_types": {"my_claude": {"cli_args__extend": ["--foo"]}}}


def test_parse_mngr_env_overrides_empty_environ() -> None:
    """Empty environ produces an empty dict."""
    assert _parse_mngr_env_overrides({}) == {}


def test_parse_mngr_env_overrides_skips_empty_segments() -> None:
    """The pattern's ``[A-Z0-9_]+`` lets shapes like ``MNGR__X__`` (trailing __)
    or ``MNGR____X`` (leading empty segment) slip through. Those produce an
    empty segment after ``split('__')``; the parser must skip them rather than
    silently materialising an unnamed key in the raw config dict.
    """
    environ = {
        "MNGR__X__": "trailing-double-underscore",
        "MNGR____X": "leading-double-underscore",
        "MNGR__COMMANDS__CREATE__": "trailing-in-middle",
    }
    raw = _parse_mngr_env_overrides(environ)
    assert raw == {}


def test_collect_env_overrides_synthesizes_preserved_aliases() -> None:
    """Preserved aliases (MNGR_PREFIX, MNGR_HOST_DIR, MNGR_HEADLESS) flow into the same raw dict."""
    environ = {
        "MNGR_PREFIX": "alias-",
        "MNGR_HOST_DIR": "/tmp/host",
        "MNGR_HEADLESS": "true",
    }
    raw = _collect_env_overrides(environ)
    assert raw["prefix"] == "alias-"
    assert raw["default_host_dir"] == "/tmp/host"
    assert raw["headless"] is True


def test_collect_env_overrides_raises_on_alias_canonical_conflict() -> None:
    """If both an alias and its canonical MNGR__* form are set with different values, raise."""
    environ = {
        "MNGR_PREFIX": "alias-",
        "MNGR__PREFIX": "canonical-",
    }
    with pytest.raises(ConfigParseError, match="Conflict: MNGR_PREFIX"):
        _collect_env_overrides(environ)


def test_collect_env_overrides_allows_alias_and_canonical_with_same_value() -> None:
    """Same value on both forms is fine (just redundant)."""
    environ = {
        "MNGR_HEADLESS": "true",
        "MNGR__HEADLESS": "true",
    }
    raw = _collect_env_overrides(environ)
    assert raw["headless"] is True


@pytest.mark.parametrize(
    ("alias_value", "canonical_value"),
    [
        ("yes", "yes"),
        ("yes", "true"),
        ("1", "true"),
        ("no", "false"),
    ],
)
def test_collect_env_overrides_accepts_semantically_equal_headless_spellings(
    alias_value: str, canonical_value: str
) -> None:
    """MNGR_HEADLESS and MNGR__HEADLESS that mean the same thing don't raise.

    The two env-var forms historically use different parsers (parse_bool_env
    vs JSON-with-string-fallback), so a raw string-equality check would
    treat e.g. MNGR_HEADLESS=yes and MNGR__HEADLESS=yes as conflicting even
    though they intend the same boolean. The conflict check normalises both
    sides through the alias parser before comparison.
    """
    environ = {
        "MNGR_HEADLESS": alias_value,
        "MNGR__HEADLESS": canonical_value,
    }
    raw = _collect_env_overrides(environ)
    expected = alias_value.lower() in {"1", "true", "yes"}
    assert raw["headless"] is expected


@pytest.mark.parametrize("value", ["yes", "1", "True", "TRUE"])
def test_collect_env_overrides_preserves_mngr_headless_truthy_spellings(value: str) -> None:
    """MNGR_HEADLESS keeps parse_bool_env semantics for backwards compatibility.

    Pre-existing scripts that set MNGR_HEADLESS=yes / =1 / =True must continue to
    yield headless=True; only the canonical MNGR__HEADLESS uses the
    JSON-parsed-with-string-fallback shape shared by the rest of MNGR__*.
    """
    raw = _collect_env_overrides({"MNGR_HEADLESS": value})
    assert raw["headless"] is True


@pytest.mark.parametrize("value", ["no", "false", "0", "", "anything-else"])
def test_collect_env_overrides_mngr_headless_falsy_values(value: str) -> None:
    """Anything not in parse_bool_env's truthy set resolves to headless=False."""
    raw = _collect_env_overrides({"MNGR_HEADLESS": value})
    assert raw["headless"] is False


# =============================================================================
# Tests for _parse_providers
# =============================================================================


def test_parse_providers_parses_valid_provider() -> None:
    """_parse_providers should parse valid provider configs."""
    raw = {"my-local": {"backend": "local"}}
    result = _parse_providers(raw, disabled_plugins=frozenset())
    assert ProviderInstanceName("my-local") in result
    assert result[ProviderInstanceName("my-local")].backend == ProviderBackendName("local")


def test_parse_providers_raises_on_unknown_backend() -> None:
    """_parse_providers should raise ConfigParseError for unknown backend."""
    raw = {"my-provider": {"some_field": "value"}}
    with pytest.raises(ConfigParseError, match="references unknown backend 'my-provider'"):
        _parse_providers(raw, disabled_plugins=frozenset())


def test_parse_providers_raises_on_unknown_fields() -> None:
    """_parse_providers should raise ConfigParseError for unknown fields by default."""
    raw = {"my-local": {"backend": "local", "typo_field": "value"}}
    with pytest.raises(ConfigParseError, match="Unknown fields in providers.my-local.*typo_field"):
        _parse_providers(raw, disabled_plugins=frozenset())


@pytest.mark.allow_warnings(match=r"Unknown fields in providers\.my-local.*typo_field")
def test_parse_providers_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_providers with strict=False should warn about unknown fields and not apply them to the model."""
    raw = {"my-local": {"backend": "local", "typo_field": "value"}}
    result = _parse_providers(raw, disabled_plugins=frozenset(), strict=False)
    assert ProviderInstanceName("my-local") in result
    assert "typo_field" not in result[ProviderInstanceName("my-local")].model_dump()
    assert any("typo_field" in msg and "providers.my-local" in msg for msg in log_warnings)


def test_parse_providers_skips_disabled_plugin() -> None:
    """_parse_providers should skip provider blocks whose plugin is disabled."""
    raw = {"modal": {"backend": "modal"}}
    result = _parse_providers(raw, disabled_plugins=frozenset({"modal"}))
    assert len(result) == 0


def test_parse_providers_keeps_non_disabled_providers() -> None:
    """_parse_providers should parse providers whose plugin is not disabled."""
    raw = {
        "my-local": {"backend": "local"},
        "modal": {"backend": "modal"},
    }
    result = _parse_providers(raw, disabled_plugins=frozenset({"modal"}))
    assert ProviderInstanceName("my-local") in result
    assert ProviderInstanceName("modal") not in result


def test_parse_providers_explicit_plugin_field_overrides_backend_for_skip() -> None:
    """_parse_providers should use explicit plugin field for disabled-plugin check."""
    raw = {"my-cloud": {"backend": "local", "plugin": "my-cloud-plugin"}}
    result = _parse_providers(raw, disabled_plugins=frozenset({"my-cloud-plugin"}))
    assert len(result) == 0


def test_parse_providers_explicit_plugin_field_not_disabled() -> None:
    """_parse_providers should parse provider when explicit plugin is not disabled."""
    raw = {"my-local": {"backend": "local", "plugin": "some-plugin"}}
    result = _parse_providers(raw, disabled_plugins=frozenset({"other-plugin"}))
    assert ProviderInstanceName("my-local") in result


def test_parse_providers_unknown_backend_mentions_disabled_plugins() -> None:
    """_parse_providers error message should mention disabled plugins when they exist."""
    raw = {"my-provider": {"backend": "nonexistent"}}
    with pytest.raises(ConfigParseError, match="Currently disabled plugins: modal"):
        _parse_providers(raw, disabled_plugins=frozenset({"modal"}))


def test_parse_providers_skips_disabled_provider_with_unknown_backend() -> None:
    """_parse_providers should skip providers with is_enabled=false when backend is unknown."""
    raw = {"my-cloud": {"backend": "nonexistent", "is_enabled": False}}
    result = _parse_providers(raw, disabled_plugins=frozenset())
    assert len(result) == 0


def test_parse_providers_preserves_disabled_provider_with_known_backend() -> None:
    """_parse_providers should preserve is_enabled=false when backend is known (for merge)."""
    raw = {"my-local": {"backend": "local", "is_enabled": False}}
    result = _parse_providers(raw, disabled_plugins=frozenset())
    assert ProviderInstanceName("my-local") in result
    assert result[ProviderInstanceName("my-local")].is_enabled is False


def test_parse_providers_still_raises_on_unknown_backend_when_enabled() -> None:
    """_parse_providers should still raise for unknown backends when is_enabled is not false."""
    raw = {"my-provider": {"backend": "nonexistent", "is_enabled": True}}
    with pytest.raises(ConfigParseError, match="references unknown backend"):
        _parse_providers(raw, disabled_plugins=frozenset())


def test_parse_providers_still_raises_on_unknown_backend_when_is_enabled_unset() -> None:
    """_parse_providers should still raise for unknown backends when is_enabled is not set."""
    raw = {"my-provider": {"backend": "nonexistent"}}
    with pytest.raises(ConfigParseError, match="references unknown backend"):
        _parse_providers(raw, disabled_plugins=frozenset())


# =============================================================================
# Tests for _parse_agent_types
# =============================================================================


def test_parse_agent_types_parses_valid_agent() -> None:
    """_parse_agent_types should parse valid agent type configs."""
    raw = {"claude": {"cli_args": "--verbose"}}
    result = _parse_agent_types(raw, disabled_plugins=frozenset())
    assert AgentTypeName("claude") in result
    assert result[AgentTypeName("claude")].cli_args == ("--verbose",)


def test_parse_agent_types_handles_empty_dict() -> None:
    """_parse_agent_types should handle empty dict."""
    result = _parse_agent_types({}, disabled_plugins=frozenset())
    assert result == {}


def test_parse_agent_types_raises_on_unknown_fields() -> None:
    """_parse_agent_types should raise ConfigParseError for unknown fields by default."""
    raw = {"claude": {"cli_args": "--verbose", "bogus_option": True}}
    with pytest.raises(ConfigParseError, match="Unknown fields in agent_types.claude.*bogus_option"):
        _parse_agent_types(raw, disabled_plugins=frozenset())


@pytest.mark.allow_warnings(match=r"Unknown fields in agent_types\.claude.*bogus_option")
def test_parse_agent_types_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_agent_types with strict=False should warn about unknown fields and not apply them to the model."""
    raw = {"claude": {"cli_args": "--verbose", "bogus_option": True}}
    result = _parse_agent_types(raw, disabled_plugins=frozenset(), strict=False)
    assert AgentTypeName("claude") in result
    assert result[AgentTypeName("claude")].cli_args == ("--verbose",)
    assert "bogus_option" not in result[AgentTypeName("claude")].model_dump()
    assert any("bogus_option" in msg and "agent_types.claude" in msg for msg in log_warnings)


class _TestParentConfig(AgentTypeConfig):
    """Test config subclass with an extra field for loader tests."""

    extra_field: bool = Field(default=False)


def test_parse_agent_types_uses_parent_type_config_class() -> None:
    """Custom types with parent_type should use the parent's config class for field validation."""
    reset_agent_config_registry()
    try:
        register_agent_config("test-parent", _TestParentConfig)

        # A custom type referencing parent_type should accept the parent's fields
        raw = {"worker": {"parent_type": "test-parent", "extra_field": True, "cli_args": "--verbose"}}
        result = _parse_agent_types(raw, disabled_plugins=frozenset())

        worker_config = result[AgentTypeName("worker")]
        assert isinstance(worker_config, _TestParentConfig)
        assert worker_config.extra_field is True
        assert worker_config.cli_args == ("--verbose",)
        assert worker_config.parent_type == AgentTypeName("test-parent")
    finally:
        reset_agent_config_registry()


def test_parse_agent_types_resolves_alias_parent_type_to_canonical() -> None:
    """A parent_type that is an alias is normalized to its canonical type."""
    reset_agent_config_registry()
    reset_agent_alias_registry()
    try:
        register_agent_config("test-parent", _TestParentConfig)
        register_agent_alias("tp", "test-parent")

        raw = {"worker": {"parent_type": "tp", "extra_field": True}}
        result = _parse_agent_types(raw, disabled_plugins=frozenset())

        worker_config = result[AgentTypeName("worker")]
        # The stored parent_type is the canonical name, not the alias.
        assert worker_config.parent_type == AgentTypeName("test-parent")
        assert isinstance(worker_config, _TestParentConfig)
        assert worker_config.extra_field is True
    finally:
        reset_agent_config_registry()
        reset_agent_alias_registry()


@pytest.mark.allow_warnings
def test_parse_agent_types_lets_custom_type_shadow_alias() -> None:
    """A custom type whose name collides with an alias wins; the alias is dropped."""
    reset_agent_alias_registry()
    try:
        register_agent_alias("agy", "antigravity")

        raw = {"agy": {"cli_args": "--verbose"}}
        result = _parse_agent_types(raw, disabled_plugins=frozenset())

        # The user's custom type is kept...
        assert AgentTypeName("agy") in result
        assert result[AgentTypeName("agy")].cli_args == ("--verbose",)
        # ...and the colliding alias was dropped, so the name now refers to the
        # custom type instead of resolving to the canonical 'antigravity'.
        assert not is_agent_alias("agy")
        assert normalize_agent_type_name("agy") == "agy"
    finally:
        reset_agent_alias_registry()


def test_parse_agent_types_unknown_field_hints_at_missing_plugin() -> None:
    """When the agent type has no registered config class and no plugins are
    disabled, the unknown-field error should suggest the providing plugin
    package may not be installed."""
    reset_agent_config_registry()
    try:
        # claude is intentionally not registered here -- simulates the plugin
        # not being installed while the user has [agent_types.claude] config.
        raw = {"claude": {"is_fast": True}}
        with pytest.raises(ConfigParseError) as exc_info:
            _parse_agent_types(raw, disabled_plugins=frozenset())
        msg = str(exc_info.value)
        assert "is_fast" in msg
        assert "plugin package that provides agent type 'claude' may not be installed" in msg
    finally:
        reset_agent_config_registry()


def test_parse_agent_types_unknown_field_hints_when_other_plugins_disabled() -> None:
    """When some plugins are disabled but not the unknown type itself, the
    error should list the disabled plugins so the user can spot a match."""
    reset_agent_config_registry()
    try:
        raw = {"claude": {"is_fast": True}}
        with pytest.raises(ConfigParseError) as exc_info:
            _parse_agent_types(raw, disabled_plugins=frozenset({"codex"}))
        msg = str(exc_info.value)
        assert "is_fast" in msg
        assert "Currently disabled plugins" in msg
        assert "codex" in msg
    finally:
        reset_agent_config_registry()


def test_parse_agent_types_no_plugin_hint_when_type_is_registered() -> None:
    """When the agent type IS registered, the hint about missing plugins
    should NOT be added -- the user really did make a typo."""
    reset_agent_config_registry()
    try:
        register_agent_config("claude", _TestParentConfig)

        raw = {"claude": {"bogus_option": True}}
        with pytest.raises(ConfigParseError) as exc_info:
            _parse_agent_types(raw, disabled_plugins=frozenset())
        msg = str(exc_info.value)
        assert "bogus_option" in msg
        assert "not installed" not in msg
    finally:
        reset_agent_config_registry()


def test_parse_agent_types_rejects_unknown_fields_even_with_parent_type() -> None:
    """Custom types with parent_type should still reject truly unknown fields."""
    reset_agent_config_registry()
    try:
        register_agent_config("test-parent", AgentTypeConfig)

        raw = {"worker": {"parent_type": "test-parent", "totally_bogus": True}}
        with pytest.raises(ConfigParseError, match="Unknown fields in agent_types.worker.*totally_bogus"):
            _parse_agent_types(raw, disabled_plugins=frozenset())
    finally:
        reset_agent_config_registry()


def test_parse_agent_types_skips_disabled_plugin_type() -> None:
    """_parse_agent_types should skip agent types whose name matches a disabled plugin."""
    raw = {
        "claude": {"cli_args": "--verbose"},
        "codex": {"cli_args": "--debug"},
    }
    result = _parse_agent_types(raw, disabled_plugins=frozenset({"claude"}))
    assert AgentTypeName("claude") not in result
    assert AgentTypeName("codex") in result


def test_parse_agent_types_skips_custom_type_with_disabled_parent() -> None:
    """_parse_agent_types should skip custom types whose parent_type is a disabled plugin."""
    reset_agent_config_registry()
    try:
        register_agent_config("test-parent", _TestParentConfig)

        raw = {"worker": {"parent_type": "test-parent", "extra_field": True}}
        result = _parse_agent_types(raw, disabled_plugins=frozenset({"test-parent"}))
        assert AgentTypeName("worker") not in result
    finally:
        reset_agent_config_registry()


def test_parse_agent_types_skips_type_with_disabled_grandparent() -> None:
    """_parse_agent_types should walk the full parent chain and skip if any ancestor is disabled."""
    raw = {
        "root-plugin": {"cli_args": "--root"},
        "mid-type": {"parent_type": "root-plugin"},
        "leaf-type": {"parent_type": "mid-type"},
        "unrelated": {"cli_args": "--ok"},
    }
    result = _parse_agent_types(raw, disabled_plugins=frozenset({"root-plugin"}))
    assert AgentTypeName("root-plugin") not in result
    assert AgentTypeName("mid-type") not in result
    assert AgentTypeName("leaf-type") not in result
    assert AgentTypeName("unrelated") in result


def test_parse_agent_types_uses_explicit_plugin_field() -> None:
    """_parse_agent_types should use an explicit plugin field to determine the owning plugin."""
    raw = {"my-type": {"plugin": "real-plugin", "cli_args": "--verbose"}}
    result = _parse_agent_types(raw, disabled_plugins=frozenset({"real-plugin"}))
    assert AgentTypeName("my-type") not in result


def test_parse_agent_types_explicit_plugin_overrides_name() -> None:
    """An explicit plugin field pointing to an enabled plugin should keep the type even if name matches a disabled plugin."""
    raw = {"disabled-name": {"plugin": "enabled-plugin", "cli_args": "--verbose"}}
    result = _parse_agent_types(raw, disabled_plugins=frozenset({"disabled-name"}))
    assert AgentTypeName("disabled-name") in result


# =============================================================================
# Tests for _parse_plugins
# =============================================================================


def test_parse_plugins_parses_valid_plugin() -> None:
    """_parse_plugins should parse valid plugin configs."""
    raw = {"my-plugin": {"enabled": True}}
    result = _parse_plugins(raw)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True


def test_parse_plugins_handles_empty_dict() -> None:
    """_parse_plugins should handle empty dict."""
    result = _parse_plugins({})
    assert result == {}


def test_parse_plugins_raises_on_unknown_fields() -> None:
    """_parse_plugins should raise ConfigParseError for unknown fields by default."""
    raw = {"my-plugin": {"enabled": True, "nonexistent_setting": "abc"}}
    with pytest.raises(ConfigParseError, match="Unknown fields in plugins.my-plugin.*nonexistent_setting"):
        _parse_plugins(raw)


@pytest.mark.allow_warnings(match=r"Unknown fields in plugins\.my-plugin.*nonexistent_setting")
def test_parse_plugins_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_plugins with strict=False should warn about unknown fields and not apply them to the model."""
    raw = {"my-plugin": {"enabled": True, "nonexistent_setting": "abc"}}
    result = _parse_plugins(raw, strict=False)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True
    assert "nonexistent_setting" not in result[PluginName("my-plugin")].model_dump()
    assert any("nonexistent_setting" in msg and "plugins.my-plugin" in msg for msg in log_warnings)


# =============================================================================
# Tests for _apply_plugin_overrides
# =============================================================================


def test_apply_plugin_overrides_enables_plugins() -> None:
    """_apply_plugin_overrides should enable plugins."""
    plugins: dict[PluginName, PluginConfig] = {}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=["my-plugin"], disabled_plugins=None)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True
    assert len(disabled) == 0


def test_apply_plugin_overrides_disables_plugins() -> None:
    """_apply_plugin_overrides should disable and filter out plugins."""
    plugins = {PluginName("my-plugin"): PluginConfig(enabled=True)}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=None, disabled_plugins=["my-plugin"])
    # Disabled plugins are filtered out
    assert PluginName("my-plugin") not in result
    assert "my-plugin" in disabled


def test_apply_plugin_overrides_filters_disabled_plugins() -> None:
    """_apply_plugin_overrides should filter out disabled plugins."""
    plugins = {
        PluginName("enabled-plugin"): PluginConfig(enabled=True),
        PluginName("disabled-plugin"): PluginConfig(enabled=False),
    }
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=None, disabled_plugins=None)
    assert PluginName("enabled-plugin") in result
    assert PluginName("disabled-plugin") not in result
    assert "disabled-plugin" in disabled


def test_apply_plugin_overrides_enables_existing_plugin() -> None:
    """_apply_plugin_overrides should enable existing disabled plugins."""
    plugins = {PluginName("my-plugin"): PluginConfig(enabled=False)}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=["my-plugin"], disabled_plugins=None)
    assert PluginName("my-plugin") in result
    assert result[PluginName("my-plugin")].enabled is True
    assert "my-plugin" not in disabled


# Marked flaky because session_cleanup occasionally blames this test for
# leaked subprocesses spawned by other tests in the same xdist sandbox (e.g.
# the documented `sleep 30` leak from concurrency_group_test). The test body
# is pure config-dict manipulation and cannot itself leak -- retry is safe.
@pytest.mark.flaky
def test_apply_plugin_overrides_creates_disabled_plugin() -> None:
    """_apply_plugin_overrides should create new disabled plugins."""
    plugins: dict[PluginName, PluginConfig] = {}
    result, disabled = _apply_plugin_overrides(plugins, enabled_plugins=None, disabled_plugins=["new-plugin"])
    # Disabled plugins are filtered out, so should not be in result
    assert PluginName("new-plugin") not in result
    assert "new-plugin" in disabled


# =============================================================================
# Tests for _parse_logging_config
# =============================================================================


def test_parse_logging_config_parses_valid_config() -> None:
    """_parse_logging_config should parse valid logging config."""
    raw = {"file_level": "TRACE", "max_log_size_mb": 20}
    result = _parse_logging_config(raw)
    assert isinstance(result, LoggingConfig)
    assert result.file_level == LogLevel.TRACE
    assert result.max_log_size_mb == 20


def test_parse_logging_config_handles_empty_dict() -> None:
    """_parse_logging_config should handle empty dict."""
    result = _parse_logging_config({})
    assert isinstance(result, LoggingConfig)


def test_parse_logging_config_raises_on_unknown_fields() -> None:
    """_parse_logging_config should raise ConfigParseError for unknown fields by default."""
    raw = {"file_level": "DEBUG", "unknown_log_option": 42}
    with pytest.raises(ConfigParseError, match="Unknown fields in logging.*unknown_log_option"):
        _parse_logging_config(raw)


@pytest.mark.allow_warnings(match=r"Unknown fields in logging.*unknown_log_option")
def test_parse_logging_config_warns_on_unknown_fields_when_not_strict(log_warnings: list[str]) -> None:
    """_parse_logging_config with strict=False should warn about unknown fields and not apply them to the model."""
    raw = {"file_level": "DEBUG", "unknown_log_option": 42}
    result = _parse_logging_config(raw, strict=False)
    assert isinstance(result, LoggingConfig)
    assert "unknown_log_option" not in result.model_dump()
    assert any("unknown_log_option" in msg for msg in log_warnings)


# =============================================================================
# Tests for _parse_tmux_config
# =============================================================================


def test_parse_tmux_config_marks_string_attach_args_as_scalar_tuple() -> None:
    """A string attach_args is shell-split and tagged ScalarTuple so narrowing
    detection treats a higher-precedence string replacement as scalar replacement."""
    result = _parse_tmux_config({"attach_args": "-CC -u"})
    assert result.attach_args == ("-CC", "-u")
    assert isinstance(result.attach_args, ScalarTuple)


def test_parse_tmux_config_keeps_list_attach_args_as_plain_tuple() -> None:
    """An explicit list attach_args is genuine aggregate intent, not a scalar string."""
    result = _parse_tmux_config({"attach_args": ["-CC", "-u"]})
    assert result.attach_args == ("-CC", "-u")
    assert not isinstance(result.attach_args, ScalarTuple)


def test_string_attach_args_replacement_is_not_flagged_as_narrowing() -> None:
    """Replacing one string attach_args with another (e.g. '-CC -u' then '-2') is scalar
    replacement, mirroring cli_args, so it must not trip the narrowing safety net."""
    base = MngrConfig(prefix="mngr-", tmux=_parse_tmux_config({"attach_args": "-CC -u"}))
    override = MngrConfig(prefix="mngr-", tmux=_parse_tmux_config({"attach_args": "-2"}))
    _, narrowings = base.merge_with(override)
    assert narrowings == []


def test_list_attach_args_replacement_is_flagged_as_narrowing() -> None:
    """A list override that drops base entries is aggregate narrowing and is flagged."""
    base = MngrConfig(prefix="mngr-", tmux=TmuxConfig(attach_args=("-CC", "-u")))
    override = MngrConfig(prefix="mngr-", tmux=_parse_tmux_config({"attach_args": ["-2"]}))
    _, narrowings = base.merge_with(override)
    assert narrowings == ["tmux.attach_args"]


# =============================================================================
# Tests for _parse_commands
# =============================================================================


def test_parse_commands_parses_valid_commands() -> None:
    """_parse_commands should parse valid command defaults."""
    raw = {"create": {"name": "test-agent", "connect": False}}
    result = _parse_commands(raw)
    assert "create" in result
    assert result["create"].defaults["name"] == "test-agent"
    assert result["create"].defaults["connect"] is False


def test_parse_commands_handles_empty_dict() -> None:
    """_parse_commands should handle empty dict."""
    result = _parse_commands({})
    assert result == {}


# =============================================================================
# Tests for _parse_create_templates
# =============================================================================


def test_parse_create_templates_parses_valid_templates() -> None:
    """_parse_create_templates should parse valid create templates."""
    raw = {"modal-dev": {"new_host": "modal", "target_path": "/root/workspace"}}
    result = _parse_create_templates(raw)
    assert CreateTemplateName("modal-dev") in result
    assert result[CreateTemplateName("modal-dev")].options["new_host"] == "modal"
    assert result[CreateTemplateName("modal-dev")].options["target_path"] == "/root/workspace"


def test_parse_create_templates_handles_empty_dict() -> None:
    """_parse_create_templates should handle empty dict."""
    result = _parse_create_templates({})
    assert result == {}


def test_parse_create_templates_multiple_templates() -> None:
    """_parse_create_templates should parse multiple templates."""
    raw = {
        "modal": {"new_host": "modal"},
        "docker": {"new_host": "docker"},
        "local": {"transfer": "none"},
    }
    result = _parse_create_templates(raw)
    assert len(result) == 3
    assert CreateTemplateName("modal") in result
    assert CreateTemplateName("docker") in result
    assert CreateTemplateName("local") in result


def test_parse_create_templates_accepts_extend_suffix() -> None:
    """``<field>__extend = [...]`` is a valid template option -- the same ``__extend``
    operator that works in TOML / ``--setting`` / env vars opts a single template
    entry into additive behavior at template-application time."""
    raw = {"dev": {"env__extend": ["DEBUG=1"]}}
    result = _parse_create_templates(raw)
    assert CreateTemplateName("dev") in result
    # The extend key is stored verbatim in options; apply_create_template
    # interprets it at template-application time.
    assert result[CreateTemplateName("dev")].options == {"env__extend": ["DEBUG=1"]}


def test_parse_create_templates_rejects_unknown_field_even_with_extend_suffix() -> None:
    """``<unknown>__extend`` is still rejected -- the ``__extend`` suffix opts the
    base key into additive merge, but the base key still has to be a real
    CreateCliOptions field. (Same shape as the bare-key validation that flagged
    typos in template options before.)"""
    raw = {"dev": {"bogus_typo__extend": ["X=1"]}}
    with pytest.raises(ConfigParseError, match="Unknown field 'bogus_typo__extend'"):
        _parse_create_templates(raw)


# =============================================================================
# Tests for parse_config
# =============================================================================


def test_parse_config_parses_full_config() -> None:
    """parse_config should parse a full config dict."""
    raw = {
        "prefix": "test-",
        "default_host_dir": "/tmp/test",
        "agent_types": {"claude": {"cli_args": "--verbose"}},
        "providers": {"local": {"backend": "local"}},
        "plugins": {"my-plugin": {"enabled": True}},
        "commands": {"create": {"name": "test"}},
        "create_templates": {"modal": {"new_host": "modal"}},
        "logging": {"file_level": "DEBUG"},
    }
    result = parse_config(raw, disabled_plugins=frozenset())
    assert result.prefix == "test-"
    assert result.default_host_dir == "/tmp/test"
    assert AgentTypeName("claude") in result.agent_types
    assert ProviderInstanceName("local") in result.providers
    assert PluginName("my-plugin") in result.plugins
    assert "create" in result.commands
    assert CreateTemplateName("modal") in result.create_templates
    assert result.logging is not None


def test_parse_config_handles_minimal_config() -> None:
    """parse_config should handle minimal config with missing optional fields."""
    raw = {"prefix": "test-"}
    result = parse_config(raw, disabled_plugins=frozenset())
    assert result.prefix == "test-"
    assert result.agent_types == {}
    assert result.providers == {}
    assert result.plugins == {}
    assert result.commands == {}
    assert result.logging is None


def test_parse_config_handles_empty_config() -> None:
    """parse_config should handle empty config dict."""
    result = parse_config({}, disabled_plugins=frozenset())
    assert result.prefix is None
    assert result.default_host_dir is None
    assert result.agent_types == {}
    assert result.providers == {}
    assert result.plugins == {}
    assert result.commands == {}
    assert result.logging is None


def test_parse_config_raises_on_unknown_top_level_field() -> None:
    """parse_config should raise ConfigParseError for unknown top-level fields by default."""
    raw = {"prefix": "test-", "nonexistent_top_level": "value"}
    with pytest.raises(ConfigParseError, match="Unknown configuration fields.*nonexistent_top_level"):
        parse_config(raw, disabled_plugins=frozenset())


@pytest.mark.allow_warnings(match=r"^Unknown configuration fields: \['nonexistent_top_level'\]")
def test_parse_config_warns_on_unknown_top_level_field_when_not_strict(log_warnings: list[str]) -> None:
    """parse_config with strict=False should warn about unknown top-level fields."""
    raw = {"prefix": "test-", "nonexistent_top_level": "value"}
    result = parse_config(raw, disabled_plugins=frozenset(), strict=False)
    assert result.prefix == "test-"
    assert any("nonexistent_top_level" in msg for msg in log_warnings)


def test_parse_config_raises_on_unknown_nested_field() -> None:
    """parse_config should raise ConfigParseError for unknown nested fields by default."""
    raw = {
        "logging": {"file_level": "DEBUG", "bad_field": True},
    }
    with pytest.raises(ConfigParseError, match="Unknown fields in logging.*bad_field"):
        parse_config(raw, disabled_plugins=frozenset())


@pytest.mark.allow_warnings(match=r"Unknown fields in logging.*bad_field")
def test_parse_config_warns_on_unknown_nested_field_when_not_strict(log_warnings: list[str]) -> None:
    """parse_config with strict=False should warn about unknown nested fields."""
    raw = {
        "logging": {"file_level": "DEBUG", "bad_field": True},
    }
    result = parse_config(raw, disabled_plugins=frozenset(), strict=False)
    assert result.logging is not None
    assert any("bad_field" in msg for msg in log_warnings)


def test_parse_config_parses_default_destroyed_host_persisted_seconds() -> None:
    """parse_config should parse default_destroyed_host_persisted_seconds from config."""
    raw = {"default_destroyed_host_persisted_seconds": 86400.0}
    result = parse_config(raw, disabled_plugins=frozenset())
    assert result.default_destroyed_host_persisted_seconds == 86400.0


def test_parse_config_handles_missing_default_destroyed_host_persisted_seconds() -> None:
    """parse_config should set None when default_destroyed_host_persisted_seconds is absent."""
    result = parse_config({}, disabled_plugins=frozenset())
    assert result.default_destroyed_host_persisted_seconds is None


def test_parse_config_accepts_every_mngr_config_field() -> None:
    """parse_config must consume every MngrConfig field (except disabled_plugins).

    If a new field is added to MngrConfig but not handled in parse_config,
    this test will fail because parse_config raises ConfigParseError for
    unknown fields in strict mode.
    """
    # disabled_plugins is computed by load_config, not parsed from config files
    fields_not_from_config_files = {"disabled_plugins"}

    # Build a raw dict with a key for every config-file-settable field.
    # Values must be valid enough for the parsing helpers to accept.
    expected_fields = set(MngrConfig.model_fields.keys()) - fields_not_from_config_files
    missing_samples = expected_fields - set(_SAMPLE_CONFIG_VALUES.keys())
    assert not missing_samples, (
        f"New MngrConfig fields need sample values in _SAMPLE_CONFIG_VALUES: {sorted(missing_samples)}"
    )
    raw: dict[str, Any] = {}
    for field_name in expected_fields:
        raw[field_name] = _SAMPLE_CONFIG_VALUES[field_name]

    result = parse_config(dict(raw), disabled_plugins=frozenset())

    # Verify the parsed config has our values for scalar fields
    assert result.prefix == "regression-"
    assert result.pager == "less"
    assert result.connect_command == "my-connect"
    assert result.is_remote_agent_installation_allowed is False
    assert result.headless is True
    assert result.unset_vars == ["TEST_VAR"]
    assert result.enabled_backends == [ProviderBackendName("local")]
    assert ".venv" in result.work_dir_extra_paths
    assert ".test_output" in result.work_dir_extra_paths


def test_load_config_threads_every_field_from_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config must thread every config-file field through to the final MngrConfig.

    If a new field is added to MngrConfig and parse_config but not to load_config's
    config_dict assembly, this test will fail because the field's value from the
    TOML file won't appear in the final config.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.delenv("MNGR_HEADLESS", raising=False)

    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text(_SAMPLE_TOML)

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    config = mngr_ctx.config

    assert config.prefix == "regression-"
    assert config.pager == "less"
    assert config.connect_command == "my-connect"
    assert config.is_remote_agent_installation_allowed is False
    assert config.headless is True
    assert config.is_nested_tmux_allowed is True
    assert config.is_error_reporting_enabled is False
    assert config.default_destroyed_host_persisted_seconds == 12345.0
    assert config.retry.connect_retry_times == 5
    assert config.retry.connect_retry_delay == "10s"
    assert config.tmux.primary_window_name == "main"
    assert config.tmux.attach_args == ("-CC",)
    assert config.tmux.additional_config_path == Path("~/.mngr/tmux.user.conf")
    assert "TEST_VAR" in config.unset_vars
    assert ProviderBackendName("local") in config.enabled_backends
    assert ".venv" in config.work_dir_extra_paths
    assert ".test_output" in config.work_dir_extra_paths
    assert config.allow_settings_key_assignment_narrowing is True


def test_load_config_disabled_plugins_includes_opt_in_plugin(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """config.disabled_plugins must include opt-in plugins blocked by default.

    Opt-in plugins (OPT_IN_PLUGINS) are blocked in create_plugin_manager but are
    not [plugins.*] config entries, so _apply_plugin_overrides alone drops them.
    load_config folds the opt-in-derived set back in so the field matches the
    actual block state.
    """
    opt_in_name = next(iter(OPT_IN_PLUGINS))
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)
    # Mirror create_plugin_manager, which blocks opt-in plugins before load_config
    # (and lets load_config's strict block pass re-affirm the block as a no-op).
    pm.set_blocked(opt_in_name)

    _isolate_load_config_env(monkeypatch)

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert opt_in_name in mngr_ctx.config.disabled_plugins


def test_load_config_disabled_plugins_excludes_explicitly_enabled_opt_in_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """An opt-in plugin explicitly enabled in config is not in disabled_plugins.

    Mirrors the runtime: read_disabled_plugins() omits an opt-in plugin whose
    config sets enabled = true, so create_plugin_manager does not block it and
    the union must not add it back.
    """
    opt_in_name = next(iter(OPT_IN_PLUGINS))
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)

    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    (profile_dir / "settings.toml").write_text(
        f"is_allowed_in_pytest = true\n[plugins.{opt_in_name}]\nenabled = true\n"
    )

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert opt_in_name not in mngr_ctx.config.disabled_plugins


def test_load_config_disabled_plugins_omits_opt_in_plugin_when_loading_all(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """MNGR_LOAD_ALL_PLUGINS keeps opt-in plugins out of disabled_plugins.

    Doc/tooling runs set MNGR_LOAD_ALL_PLUGINS so create_plugin_manager blocks
    nothing; the union must be gated the same way or it would mark opt-in plugins
    disabled even though they are loaded.
    """
    opt_in_name = next(iter(OPT_IN_PLUGINS))
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert opt_in_name not in mngr_ctx.config.disabled_plugins


# Sample values used by the regression tests above. When adding a new field to
# MngrConfig, add an entry here with a non-default value so the tests catch it.
_SAMPLE_CONFIG_VALUES: dict[str, Any] = {
    "prefix": "regression-",
    "default_host_dir": "/tmp/regression",
    "unset_vars": ["TEST_VAR"],
    "pager": "less",
    "enabled_backends": ["local"],
    "agent_types": {"claude": {"cli_args": "--verbose"}},
    "providers": {"local": {"backend": "local"}},
    "plugins": {"my-plugin": {"enabled": True}},
    "commands": {"create": {"name": "test"}},
    "create_templates": {"modal": {"new_host": "modal"}},
    "pre_command_scripts": {"create": ["echo hello"]},
    "work_dir_extra_paths": {".venv": "SHARE", ".test_output": "COPY"},
    "retry": {"connect_retry_times": 5, "connect_retry_delay": "10s"},
    "logging": {"file_level": "DEBUG"},
    "tmux": {
        "primary_window_name": "main",
        "attach_args": ["-CC"],
        "additional_config_path": "~/.mngr/tmux.user.conf",
    },
    "is_remote_agent_installation_allowed": False,
    "connect_command": "my-connect",
    "is_nested_tmux_allowed": True,
    "headless": True,
    "is_error_reporting_enabled": False,
    "is_allowed_in_pytest": True,
    "default_destroyed_host_persisted_seconds": 12345.0,
    "default_min_online_host_age_seconds": 600.0,
    "agent_ready_timeout": 15.0,
    "allow_settings_key_assignment_narrowing": True,
}

_SAMPLE_TOML = """\
prefix = "regression-"
default_host_dir = "/tmp/regression"
unset_vars = ["TEST_VAR"]
pager = "less"
enabled_backends = ["local"]
connect_command = "my-connect"
is_remote_agent_installation_allowed = false
is_nested_tmux_allowed = true
headless = true
is_error_reporting_enabled = false
is_allowed_in_pytest = true
default_destroyed_host_persisted_seconds = 12345.0
default_min_online_host_age_seconds = 600.0
agent_ready_timeout = 15.0
allow_settings_key_assignment_narrowing = true

[commands.create]
name = "test"

[pre_command_scripts]
create = ["echo hello"]

[work_dir_extra_paths]
".venv" = "SHARE"
".test_output" = "COPY"

[retry]
connect_retry_times = 5
connect_retry_delay = "10s"

[logging]
file_level = "DEBUG"

[tmux]
primary_window_name = "main"
attach_args = ["-CC"]
additional_config_path = "~/.mngr/tmux.user.conf"
"""


def test_parse_providers_accepts_destroyed_host_persisted_seconds() -> None:
    """_parse_providers should accept destroyed_host_persisted_seconds on any provider config."""
    raw_providers = {
        "my-local": {
            "backend": "local",
            "destroyed_host_persisted_seconds": 172800.0,
        },
    }
    result = _parse_providers(raw_providers, disabled_plugins=frozenset())
    provider_config = result[ProviderInstanceName("my-local")]
    assert provider_config.destroyed_host_persisted_seconds == 172800.0


# =============================================================================
# Tests for on_load_config hook
# =============================================================================


def test_on_load_config_hook_is_called(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Test that the on_load_config hook is called during load_config."""
    # Track whether hook was called
    hook_called = False
    received_config_dict: dict[str, Any] = {}

    class TestPlugin:
        @hookimpl
        def on_load_config(self, config_dict: dict[str, Any]) -> None:
            nonlocal hook_called, received_config_dict
            hook_called = True
            received_config_dict = dict(config_dict)

    # Set up plugin manager with our test plugin
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(TestPlugin())
    load_all_registries(pm)

    # Ensure no config files interfere
    _isolate_load_config_env(monkeypatch)

    # Call load_config
    load_config(
        pm=pm,
        concurrency_group=cg,
    )

    # Verify hook was called
    assert hook_called, "on_load_config hook was not called"
    assert "prefix" in received_config_dict or "providers" in received_config_dict


def test_on_load_config_hook_can_modify_config(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Test that on_load_config hook can modify the config dict."""

    class TestPlugin:
        @hookimpl
        def on_load_config(self, config_dict: dict[str, Any]) -> None:
            # Modify the config dict to change the prefix
            config_dict["prefix"] = "modified-by-plugin-"

    # Set up plugin manager with our test plugin
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(TestPlugin())
    load_all_registries(pm)

    # Ensure no config files interfere
    _isolate_load_config_env(monkeypatch)

    # Call load_config
    mngr_ctx = load_config(
        pm=pm,
        concurrency_group=cg,
    )

    # Verify the config was modified
    assert mngr_ctx.config.prefix == "modified-by-plugin-"


def test_on_load_config_hook_can_add_new_fields(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Test that on_load_config hook can add new config fields."""

    class TestPlugin:
        @hookimpl
        def on_load_config(self, config_dict: dict[str, Any]) -> None:
            # Add a custom agent type
            if "agent_types" not in config_dict:
                config_dict["agent_types"] = {}
            config_dict["agent_types"][AgentTypeName("custom-agent")] = {"cli_args": "--custom"}

    # Set up plugin manager with our test plugin
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(TestPlugin())
    load_all_registries(pm)

    # Ensure no config files interfere
    _isolate_load_config_env(monkeypatch)

    # Call load_config
    mngr_ctx = load_config(
        pm=pm,
        concurrency_group=cg,
    )

    # Verify the agent type was added
    assert AgentTypeName("custom-agent") in mngr_ctx.config.agent_types
    assert mngr_ctx.config.agent_types[AgentTypeName("custom-agent")].cli_args == ("--custom",)


# =============================================================================
# Tests for get_or_create_profile_dir
# =============================================================================


def test_get_or_create_profile_dir_creates_new_profile_when_no_config(tmp_path: Path) -> None:
    """get_or_create_profile_dir should create a new profile when config.toml doesn't exist."""
    base_dir = tmp_path / "mngr"

    result = get_or_create_profile_dir(base_dir)

    # Should have created the directories
    assert (base_dir / "profiles").exists()
    assert result.parent == base_dir / "profiles"
    assert result.exists()

    # Should have written config.toml with the profile ID
    config_path = base_dir / "config.toml"
    assert config_path.exists()
    content = config_path.read_text()
    profile_id = result.name
    assert f'profile = "{profile_id}"' in content


def test_get_or_create_profile_dir_reads_existing_profile_from_config(tmp_path: Path) -> None:
    """get_or_create_profile_dir should read existing profile from config.toml."""
    base_dir = tmp_path / "mngr"
    base_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = base_dir / "profiles"
    profiles_dir.mkdir(exist_ok=True)

    # Create existing profile
    existing_profile_id = "existing123"
    existing_profile_dir = profiles_dir / existing_profile_id
    existing_profile_dir.mkdir(exist_ok=True)

    # Write config.toml pointing to existing profile
    config_path = base_dir / "config.toml"
    config_path.write_text(f'profile = "{existing_profile_id}"\n')

    result = get_or_create_profile_dir(base_dir)

    assert result == existing_profile_dir
    assert result.name == existing_profile_id


def test_get_or_create_profile_dir_creates_profile_dir_if_specified_but_missing(tmp_path: Path) -> None:
    """get_or_create_profile_dir should create profile dir if config.toml specifies it but dir doesn't exist."""
    base_dir = tmp_path / "mngr"
    base_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = base_dir / "profiles"
    profiles_dir.mkdir(exist_ok=True)

    # Write config.toml pointing to non-existent profile
    specified_profile_id = "specified456"
    config_path = base_dir / "config.toml"
    config_path.write_text(f'profile = "{specified_profile_id}"\n')

    result = get_or_create_profile_dir(base_dir)

    # Should have created the specified profile directory
    assert result == profiles_dir / specified_profile_id
    assert result.exists()


def test_get_or_create_profile_dir_raises_on_invalid_config_toml(tmp_path: Path) -> None:
    """get_or_create_profile_dir should raise ConfigParseError when config.toml is invalid."""
    base_dir = tmp_path / "mngr"
    base_dir.mkdir(parents=True, exist_ok=True)

    # Write invalid TOML
    config_path = base_dir / "config.toml"
    config_path.write_text("[invalid toml syntax")

    with pytest.raises(ConfigParseError, match="Failed to parse config file"):
        get_or_create_profile_dir(base_dir)


def test_get_or_create_profile_dir_handles_config_without_profile_key(tmp_path: Path) -> None:
    """get_or_create_profile_dir should create new profile if config.toml has no 'profile' key."""
    base_dir = tmp_path / "mngr"
    base_dir.mkdir(parents=True, exist_ok=True)

    # Write valid TOML but without profile key
    config_path = base_dir / "config.toml"
    config_path.write_text('other_key = "value"\n')

    result = get_or_create_profile_dir(base_dir)

    # Should have created a new profile
    assert result.exists()
    assert result.parent == base_dir / "profiles"


def test_get_or_create_profile_dir_returns_same_profile_on_subsequent_calls(tmp_path: Path) -> None:
    """get_or_create_profile_dir should return the same profile on subsequent calls."""
    base_dir = tmp_path / "mngr"

    result1 = get_or_create_profile_dir(base_dir)
    result2 = get_or_create_profile_dir(base_dir)

    assert result1 == result2


# =============================================================================
# Tests for _get_or_create_user_id
# =============================================================================


def test_get_or_create_user_id_creates_new_id_when_file_missing(tmp_path: Path) -> None:
    """_get_or_create_user_id should create a new user ID when file doesn't exist."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    result = get_or_create_user_id(profile_dir)

    # Should return a non-empty string (hex UUID, which is 32 chars)
    assert result
    assert len(result) == 32

    # Should have written the ID to file
    user_id_file = profile_dir / "user_id"
    assert user_id_file.exists()
    assert user_id_file.read_text() == result


def test_get_or_create_user_id_reads_existing_id(tmp_path: Path) -> None:
    """_get_or_create_user_id should read existing user ID from file."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Create existing user_id file
    existing_id = "abcdef1234567890abcdef1234567890"
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text(existing_id)

    result = get_or_create_user_id(profile_dir)

    assert result == existing_id


def test_get_or_create_user_id_strips_whitespace(tmp_path: Path) -> None:
    """_get_or_create_user_id should strip whitespace from existing ID."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Create existing user_id file with whitespace
    existing_id = "abcdef1234567890abcdef1234567890"
    user_id_file = profile_dir / "user_id"
    user_id_file.write_text(f"  {existing_id}  \n")

    result = get_or_create_user_id(profile_dir)

    assert result == existing_id


def test_get_or_create_user_id_returns_same_id_on_subsequent_calls(tmp_path: Path) -> None:
    """_get_or_create_user_id should return the same ID on subsequent calls."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    result1 = get_or_create_user_id(profile_dir)
    result2 = get_or_create_user_id(profile_dir)

    assert result1 == result2


# =============================================================================
# Tests for MNGR_ALLOW_UNKNOWN_CONFIG via load_config
# =============================================================================


def test_load_config_rejects_unknown_fields_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should raise on unknown config fields when MNGR_ALLOW_UNKNOWN_CONFIG is not set."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.delenv("MNGR_ALLOW_UNKNOWN_CONFIG", raising=False)

    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    settings_path = profile_dir / "settings.toml"
    # is_allowed_in_pytest lets the config past the pytest guard so the
    # unknown-field rejection (the behavior under test) is what surfaces.
    settings_path.write_text('future_field = "hello"\nis_allowed_in_pytest = true\n')

    with pytest.raises(ConfigParseError, match="Unknown configuration fields.*future_field"):
        load_config(pm=pm, concurrency_group=cg)


@pytest.mark.allow_warnings(match=r"^Unknown configuration fields: \['future_field'\]")
def test_load_config_allows_unknown_fields_with_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    temp_git_repo_cwd: Path,
    cg: ConcurrencyGroup,
    log_warnings: list[str],
) -> None:
    """load_config should warn (not raise) on unknown fields when MNGR_ALLOW_UNKNOWN_CONFIG is set."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("MNGR_ALLOW_UNKNOWN_CONFIG", "1")

    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text('future_field = "hello"\nis_allowed_in_pytest = true\n')

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert mngr_ctx.config.prefix == "mngr-"
    assert any("future_field" in msg for msg in log_warnings)


# =============================================================================
# Tests for default_destroyed_host_persisted_seconds via load_config
# =============================================================================


def test_load_config_preserves_default_destroyed_host_persisted_seconds_from_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should forward default_destroyed_host_persisted_seconds from TOML to the final config."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)

    # Write a user config with custom default_destroyed_host_persisted_seconds
    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text("is_allowed_in_pytest = true\ndefault_destroyed_host_persisted_seconds = 86400.0\n")

    mngr_ctx = load_config(
        pm=pm,
        concurrency_group=cg,
    )

    assert mngr_ctx.config.default_destroyed_host_persisted_seconds == 86400.0


# =============================================================================
# Tests for _parse_commands with default_subcommand
# =============================================================================


def test_parse_commands_extracts_default_subcommand() -> None:
    """_parse_commands should extract default_subcommand from raw defaults."""
    raw = {"mngr": {"default_subcommand": "list", "connect": False}}
    result = _parse_commands(raw)
    assert result["mngr"].default_subcommand == "list"
    # default_subcommand should NOT appear in the defaults dict
    assert "default_subcommand" not in result["mngr"].defaults
    assert result["mngr"].defaults["connect"] is False


def test_parse_commands_handles_missing_default_subcommand() -> None:
    """_parse_commands should set default_subcommand to None when absent."""
    raw = {"create": {"new_host": "docker"}}
    result = _parse_commands(raw)
    assert result["create"].default_subcommand is None
    assert result["create"].defaults["new_host"] == "docker"


def test_parse_commands_empty_string_default_subcommand() -> None:
    """_parse_commands should preserve empty string default_subcommand."""
    raw = {"mngr": {"default_subcommand": ""}}
    result = _parse_commands(raw)
    assert result["mngr"].default_subcommand == ""


# =============================================================================
# Tests for block_disabled_plugins
# =============================================================================


def test_block_disabled_plugins_blocks_names_in_plugin_manager() -> None:
    """block_disabled_plugins should call pm.set_blocked for each disabled name."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    block_disabled_plugins(pm, frozenset({"modal", "docker"}))

    assert pm.is_blocked("modal")
    assert pm.is_blocked("docker")
    assert not pm.is_blocked("local")


def test_block_disabled_plugins_is_idempotent() -> None:
    """block_disabled_plugins should be safe to call multiple times."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    block_disabled_plugins(pm, frozenset({"modal"}))
    block_disabled_plugins(pm, frozenset({"modal"}))

    assert pm.is_blocked("modal")


# =============================================================================
# Tests for _normalize_tuple_fields_for_construct
# =============================================================================


def test_normalize_cli_args_no_cli_args_key() -> None:
    """_normalize_tuple_fields_for_construct should return the input unchanged when no cli_args key."""
    raw = {"some_key": "value"}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result == {"some_key": "value"}


def test_normalize_cli_args_string_value() -> None:
    """_normalize_tuple_fields_for_construct should split a non-empty string into a tuple."""
    raw = {"cli_args": "--verbose --model opus"}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["cli_args"] == ("--verbose", "--model", "opus")


def test_normalize_cli_args_empty_string() -> None:
    """_normalize_tuple_fields_for_construct should convert an empty string to an empty tuple."""
    raw = {"cli_args": ""}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["cli_args"] == ()


def test_normalize_cli_args_list_value() -> None:
    """_normalize_tuple_fields_for_construct should convert a list to a tuple."""
    raw = {"cli_args": ["--verbose", "--model", "opus"]}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["cli_args"] == ("--verbose", "--model", "opus")


def test_normalize_cli_args_tuple_value() -> None:
    """_normalize_tuple_fields_for_construct should pass through a tuple."""
    raw = {"cli_args": ("--verbose",)}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["cli_args"] == ("--verbose",)


def test_normalize_cli_args_other_type_passes_through() -> None:
    """_normalize_tuple_fields_for_construct should pass through unrecognized types."""
    raw = {"cli_args": 42}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["cli_args"] == 42


def test_normalize_tuple_fields_converts_provisioning_lists() -> None:
    """_normalize_tuple_fields_for_construct should convert TOML lists to tuples for provisioning fields."""
    raw = {
        "extra_provision_command": ["echo setup", "echo done"],
        "env": ["FOO=1"],
        "upload_file": ["a.txt:/a.txt"],
    }
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["extra_provision_command"] == ("echo setup", "echo done")
    assert result["env"] == ("FOO=1",)
    assert result["upload_file"] == ("a.txt:/a.txt",)


def test_normalize_tuple_fields_ignores_missing_fields() -> None:
    """_normalize_tuple_fields_for_construct should leave config unchanged when no tuple fields present."""
    raw = {"parent_type": "claude", "command": "my-cmd"}
    result = _normalize_tuple_fields_for_construct(raw)
    assert result == {"parent_type": "claude", "command": "my-cmd"}


def test_normalize_tuple_fields_handles_all_fields_together() -> None:
    """_normalize_tuple_fields_for_construct should normalize cli_args and provisioning fields in one call."""
    raw = {
        "cli_args": "--verbose",
        "extra_provision_command": ["echo hi"],
        "create_directory": ["/tmp/test"],
    }
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["cli_args"] == ("--verbose",)
    assert result["extra_provision_command"] == ("echo hi",)
    assert result["create_directory"] == ("/tmp/test",)


def test_normalize_tuple_fields_wraps_string_in_tuple() -> None:
    """_normalize_tuple_fields_for_construct should wrap a bare string in a one-element tuple for provisioning fields."""
    raw = {
        "extra_provision_command": "echo setup",
        "env": "FOO=1",
    }
    result = _normalize_tuple_fields_for_construct(raw)
    assert result["extra_provision_command"] == ("echo setup",)
    assert result["env"] == ("FOO=1",)


# =============================================================================
# Tests for _parse_mngr_env_overrides edge cases
# =============================================================================


def test_parse_mngr_env_overrides_skips_bare_prefix() -> None:
    """Env key exactly equal to the prefix is skipped (no segments to parse)."""
    environ = {"MNGR__": "value"}
    assert _parse_mngr_env_overrides(environ) == {}


def test_parse_mngr_env_overrides_skips_old_command_form() -> None:
    """The old MNGR_COMMANDS_* form is no longer recognized; it's ignored."""
    environ = {"MNGR_COMMANDS_CREATE_BRANCH": "main"}
    assert _parse_mngr_env_overrides(environ) == {}


# =============================================================================
# Tests for load_config pytest guard
# =============================================================================


def test_load_config_raises_when_in_pytest_and_not_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should raise ConfigParseError when is_allowed_in_pytest is False and PYTEST_CURRENT_TEST is set."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_something")

    # Write config that disables pytest
    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    settings_path = profile_dir / "settings.toml"
    settings_path.write_text("is_allowed_in_pytest = false\n")

    with pytest.raises(ConfigParseError, match="Running mngr within pytest is not allowed"):
        load_config(pm=pm, concurrency_group=cg)


def test_load_config_allows_pytest_when_config_opts_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config runs during pytest when the loaded config sets is_allowed_in_pytest = true.

    Configs written specifically for tests set this flag; real configs (which
    default to False) stay blocked even when picked up by a poorly-scoped test.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_something")

    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    (profile_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n")

    # Should NOT raise.
    load_config(pm=pm, concurrency_group=cg)


def test_load_config_raises_when_in_pytest_and_config_omits_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A loaded config that omits is_allowed_in_pytest raises during pytest.

    This pins the default: is_allowed_in_pytest defaults to False, so a config
    file that does not set it cannot be picked up during a pytest run.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_something")

    # A real config file that sets an unrelated key but does not opt in.
    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    (profile_dir / "settings.toml").write_text('prefix = "custom-"\n')

    with pytest.raises(ConfigParseError, match="Running mngr within pytest is not allowed"):
        load_config(pm=pm, concurrency_group=cg)


def test_load_config_raises_when_one_layer_opts_in_but_another_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A non-opted-in layer trips the guard even when a higher layer opts in.

    This pins the per-layer design: the guard checks every loaded config file
    individually, not just the merged value. Here the lower-precedence user layer
    is a real config that does NOT opt in, while the higher-precedence project
    layer does -- so the *merged* is_allowed_in_pytest resolves to True. A merged-
    value check would therefore let the real user config be loaded; the per-layer
    check must still raise on the user layer. Without this, a test config opting
    in could silently mask a real config riding in beneath it.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_something")

    # Lower-precedence user/profile layer: a real config that does NOT opt in.
    mngr_dir = tmp_path / ".mngr"
    mngr_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = get_or_create_profile_dir(mngr_dir)
    user_settings_path = profile_dir / "settings.toml"
    user_settings_path.write_text('prefix = "custom-"\n')

    # Higher-precedence project layer opts in. root_name collapses to "mngr" under
    # _isolate_load_config_env, so this resolves to <git-root>/.mngr/. The merged
    # is_allowed_in_pytest is therefore True (project overrides user).
    project_config_dir = temp_git_repo_cwd / ".mngr"
    project_config_dir.mkdir(parents=True, exist_ok=True)
    (project_config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n")

    with pytest.raises(ConfigParseError, match="Running mngr within pytest is not allowed") as exc_info:
        load_config(pm=pm, concurrency_group=cg)
    # The error must name the non-opted-in user layer even though the merged value
    # is True, proving the per-layer check fired rather than a merged-value check.
    assert str(user_settings_path) in str(exc_info.value)


def test_load_config_allows_pytest_when_no_config_file_loaded(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config runs during pytest when no config file is picked up at all.

    The guard only fires when a config file was actually loaded; with nothing to
    protect against, a test that loads no config file needs no opt-in.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_something")

    # No settings.toml is written anywhere: the HOME-based profile dir is empty
    # and the isolated git repo cwd has no .mngr/, so no config file is loaded.

    # Should NOT raise.
    load_config(pm=pm, concurrency_group=cg)


# =============================================================================
# Tests for load_config with env command overrides
# =============================================================================


def test_load_config_applies_mngr_env_overrides(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """load_config should merge ``MNGR__*`` env overrides into the final config."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("MNGR__COMMANDS__CREATE__CONNECT", "false")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    assert "create" in mngr_ctx.config.commands
    # JSON-parsed: "false" becomes the boolean False.
    assert mngr_ctx.config.commands["create"].defaults.get("connect") is False


def test_load_config_headless_default_is_false(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """By default, config.headless is False."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.delenv("MNGR_HEADLESS", raising=False)

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    assert mngr_ctx.config.headless is False


def test_load_config_mngr_headless_env_var_true(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """MNGR_HEADLESS=true sets config.headless to True."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("MNGR_HEADLESS", "true")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    assert mngr_ctx.config.headless is True


def test_load_config_mngr_headless_env_var_false(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """MNGR_HEADLESS=false sets config.headless to False."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("MNGR_HEADLESS", "false")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    assert mngr_ctx.config.headless is False


def test_load_config_headless_from_config_file(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """headless = true in settings.toml sets config.headless to True."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    monkeypatch.delenv("MNGR_HEADLESS", raising=False)

    # Write a project settings file with headless = true. is_allowed_in_pytest
    # opts this loaded config into the pytest run (it defaults to False).
    mngr_dir = temp_git_repo_cwd / ".mngr"
    mngr_dir.mkdir(exist_ok=True)
    (mngr_dir / "settings.toml").write_text("is_allowed_in_pytest = true\nheadless = true\n")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    assert mngr_ctx.config.headless is True


def test_load_config_mngr_headless_env_overrides_config_file(
    monkeypatch: pytest.MonkeyPatch, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """MNGR_HEADLESS env var overrides headless setting from config file."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    # Config file says headless = true, but env var says false
    monkeypatch.setenv("MNGR_HEADLESS", "false")

    mngr_dir = temp_git_repo_cwd / ".mngr"
    mngr_dir.mkdir(exist_ok=True)
    (mngr_dir / "settings.toml").write_text("is_allowed_in_pytest = true\nheadless = true\n")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    assert mngr_ctx.config.headless is False


# =============================================================================
# Tests for hyphen normalization in config field names
# =============================================================================


def test_parse_commands_normalizes_hyphens_to_underscores() -> None:
    """_parse_commands should accept hyphenated TOML field names like `pass-env`."""
    raw = {"create": {"pass-env": ["FOO", "BAR"]}}
    result = _parse_commands(raw)
    assert result["create"].defaults == {"pass_env": ["FOO", "BAR"]}


def test_parse_commands_raises_on_hyphen_underscore_collision() -> None:
    """_parse_commands should raise when both `pass-env` and `pass_env` are set."""
    raw = {"create": {"pass-env": ["FOO"], "pass_env": ["BAR"]}}
    with pytest.raises(ConfigParseError, match="both 'pass-env' and 'pass_env'"):
        _parse_commands(raw)


def test_parse_create_templates_normalizes_hyphens() -> None:
    """_parse_create_templates should accept hyphenated TOML field names."""
    raw = {"mytmpl": {"pass-env": ["FOO"], "new-host": True}}
    result = _parse_create_templates(raw)
    assert result[CreateTemplateName("mytmpl")].options == {"pass_env": ["FOO"], "new_host": True}


def test_parse_config_normalizes_top_level_hyphens() -> None:
    """parse_config should accept hyphenated top-level field names."""
    raw = {"connect-command": "tmux attach"}
    cfg = parse_config(raw, disabled_plugins=frozenset())
    assert cfg.connect_command == "tmux attach"


def test_parse_logging_config_normalizes_hyphens() -> None:
    """_parse_logging_config should accept hyphenated TOML field names without raising."""
    # Without normalization, an unknown `file-level` would raise (or warn); the
    # presence of the field after normalization is what we are asserting.
    raw = {"file-level": "DEBUG"}
    result = _parse_logging_config(raw)
    assert result.file_level == "DEBUG"


def test_parse_plugins_normalizes_hyphens() -> None:
    """_parse_plugins should accept hyphenated TOML field names within a plugin block."""

    class _HyphenTestPluginConfig(PluginConfig):
        custom_field: str = "default"

    # The plugin config registry is populated at module-import time by external
    # plugin packages (e.g. mngr_notifications), so a blanket reset here would
    # wipe legitimate registrations and break tests in other packages that look
    # them up. Snapshot and restore just this test's addition instead.
    register_plugin_config("hyphen-test-plugin", _HyphenTestPluginConfig)
    try:
        raw = {"hyphen-test-plugin": {"custom-field": "value"}}
        result = _parse_plugins(raw)
        parsed = result[PluginName("hyphen-test-plugin")]
        assert isinstance(parsed, _HyphenTestPluginConfig)
        assert parsed.custom_field == "value"
    finally:
        _plugin_config_registry.pop(PluginName("hyphen-test-plugin"), None)


# =============================================================================
# Tests for silent=True warning suppression (used by `mngr plugin add`)
# =============================================================================


def test_parse_providers_silent_does_not_warn_on_unknown_backend(log_warnings: list[str]) -> None:
    """_parse_providers with strict=False, silent=True must not warn about an unknown backend.

    Regression guard for the issue where `mngr plugin add` was emitting
    `Provider X references unknown backend Y` warnings during installation,
    since the user is *about to install* the missing backend.
    """
    raw = {"modal": {"backend": "modal"}}
    result = _parse_providers(raw, disabled_plugins=frozenset(), strict=False, silent=True)
    # Provider with unknown backend was skipped (same as non-silent strict=False path).
    assert ProviderInstanceName("modal") not in result
    # No warning was emitted.
    assert not any("references unknown backend" in msg for msg in log_warnings), log_warnings


def test_parse_providers_silent_does_not_warn_on_unknown_field(log_warnings: list[str]) -> None:
    """_parse_providers with silent=True must not warn about unknown fields on a known backend."""
    raw = {"my-local": {"backend": "local", "typo_field": "value"}}
    result = _parse_providers(raw, disabled_plugins=frozenset(), strict=False, silent=True)
    assert ProviderInstanceName("my-local") in result
    # Unknown field still stripped from the parsed model -- silent affects only the warning.
    assert "typo_field" not in result[ProviderInstanceName("my-local")].model_dump()
    assert not any("typo_field" in msg for msg in log_warnings), log_warnings


def test_parse_agent_types_silent_does_not_warn_on_unknown_field(log_warnings: list[str]) -> None:
    """_parse_agent_types with silent=True must not warn about unknown fields."""
    raw = {"claude": {"bogus_option": "value"}}
    result = _parse_agent_types(raw, disabled_plugins=frozenset(), strict=False, silent=True)
    # Unknown field still stripped -- silent affects only the warning.
    assert AgentTypeName("claude") in result
    assert not any("bogus_option" in msg for msg in log_warnings), log_warnings


def test_parse_plugins_silent_does_not_warn_on_unknown_field(log_warnings: list[str]) -> None:
    """_parse_plugins with silent=True must not warn about unknown fields."""
    raw = {"some-plugin": {"unknown_setting": "x"}}
    _parse_plugins(raw, strict=False, silent=True)
    assert not any("unknown_setting" in msg for msg in log_warnings), log_warnings


def test_parse_config_silent_does_not_warn_on_unknown_top_level_field(log_warnings: list[str]) -> None:
    """parse_config with silent=True must not warn about unknown top-level fields."""
    raw = {"future_top_level_field": "x"}
    parse_config(raw, disabled_plugins=frozenset(), strict=False, silent=True)
    assert not any("future_top_level_field" in msg for msg in log_warnings), log_warnings


# =============================================================================
# Tests for _normalize_field_keys invariants (env-var path safety)
# =============================================================================


def test_parse_config_rejects_field_name_with_double_underscore() -> None:
    """A field name containing '__' (other than the trailing __extend suffix)
    is rejected at config-load time. The double-underscore segment separator
    is reserved for env-var encoding, so a key like ``foo__bar`` would be
    indistinguishable from a nested ``foo.bar`` in MNGR__FOO__BAR form.
    """
    raw = {"foo__bar": 1}
    with pytest.raises(ConfigParseError, match=r"containing '__' in its field name"):
        parse_config(raw, disabled_plugins=frozenset())


def test_parse_config_accepts_extend_suffix_on_field_name() -> None:
    """The trailing __extend suffix is the one place '__' is allowed in a key.

    The suffix is stripped before the field-name shape check, so an extend
    write on an aggregate field doesn't trigger the double-underscore
    rejection. The resolver would normally apply the suffix before
    ``parse_config`` is invoked; passing it through here directly exercises
    the normalisation path in isolation.
    """
    # ``cli_args__extend`` on an agent-type block normalises OK and is
    # forwarded as an unknown field to the agent-type parser; the surrounding
    # parse_config call must not raise from _normalize_field_keys.
    raw = {"agent_types": {"my_agent": {"cli_args__extend": ["--debug"]}}}
    # Strict mode would reject the unknown ``cli_args__extend`` field on the
    # agent-type config, but the failure point we care about is the top-level
    # name shape check, which must pass. Use strict=False so the agent-type
    # parser drops the unknown field with a warning instead of raising.
    parse_config(raw, disabled_plugins=frozenset(), strict=False, silent=True)


def test_parse_config_rejects_sibling_lowercase_collision_within_block() -> None:
    """Two sibling keys *within a single block* that lowercase-collapse to the
    same env-var segment are ambiguous in MNGR__* lookups and are rejected.

    Example: an agent-type block with both ``Cli-Args`` and ``cli_args`` keys
    would resolve to the same ``CLI_ARGS`` env-var segment, so the loader
    raises rather than silently choosing one.
    """
    raw = {"agent_types": {"my_agent": {"Cli-Args": ["--foo"], "cli_args": ["--bar"]}}}
    with pytest.raises(
        ConfigParseError,
        match=r"collapse to the same env-var segment 'CLI_ARGS'",
    ):
        parse_config(raw, disabled_plugins=frozenset())


# =============================================================================
# Tests for the allow_settings_key_assignment_narrowing safety net
# =============================================================================


def _write_two_layer_narrowing_config(tmp_path: Path, allow_narrowing: bool | None) -> Path:
    """Set up a tmp_path with project and local settings whose merge would narrow
    ``commands.create.env``. Returns ``tmp_path`` so the caller can pass it as
    the project config dir (the calling tests expose it via ``MNGR_PROJECT_CONFIG_DIR``).

    ``allow_narrowing=None`` leaves the field unset (default False);
    ``True``/``False`` writes it into the project ``settings.toml`` (the same
    file used as the lower-precedence ``env = ["X=4"]`` layer) so the loader
    sees the same opt-in value the final merged config will resolve to.
    """
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text('is_allowed_in_pytest = true\n\n[commands.create]\nenv = ["X=4"]\n')
    local_path = tmp_path / "settings.local.toml"
    local_path.write_text('is_allowed_in_pytest = true\n\n[commands.create]\nenv = ["X=5"]\n')
    if allow_narrowing is not None:
        opt_in_value = "true" if allow_narrowing else "false"
        settings_path.write_text(
            "is_allowed_in_pytest = true\n"
            f"allow_settings_key_assignment_narrowing = {opt_in_value}\n\n"
            '[commands.create]\nenv = ["X=4"]\n'
        )
    return tmp_path


def test_load_config_narrowing_raises_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Two settings layers that assign over a non-empty list raise without the opt-in.

    Mirrors the user's documented example: project ``env = ["X=4"]`` and local
    ``env = ["X=5"]`` would merge to just ``["X=5"]`` under assign-by-default,
    silently dropping ``X=4``. The safety net catches this and tells the user
    how to opt in (or switch to ``__extend``).
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    _write_two_layer_narrowing_config(tmp_path, allow_narrowing=None)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    with pytest.raises(ConfigParseError, match="narrowing"):
        load_config(pm=pm, concurrency_group=cg)


def test_load_config_narrowing_allowed_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Setting ``allow_settings_key_assignment_narrowing = true`` silences the safety net."""
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    _write_two_layer_narrowing_config(tmp_path, allow_narrowing=True)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    # local layer's env wins -- ["X=4"] was dropped, which the user explicitly opted into.
    assert mngr_ctx.config.commands["create"].defaults["env"] == ["X=5"]


def test_load_config_narrowing_skipped_when_guard_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """``enforce_narrowing_guard=False`` loads a narrowing config without raising.

    The ``mngr config`` command relies on this so it can load (and thus edit) a config that
    would otherwise trip the guard -- otherwise ``config set`` / ``config unset``, the way to
    fix a narrowing config, would themselves fail with the narrowing error (a catch-22).
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    _write_two_layer_narrowing_config(tmp_path, allow_narrowing=None)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    mngr_ctx = load_config(pm=pm, concurrency_group=cg, enforce_narrowing_guard=False)
    # Loads without raising; the narrowing is allowed through (higher layer's env wins).
    assert mngr_ctx.config.commands["create"].defaults["env"] == ["X=5"]


def test_load_config_extend_avoids_narrowing_without_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Using ``env__extend`` in the higher-precedence layer preserves base entries
    and never trips the narrowing guard. The merged value contains both layers'
    entries in precedence order.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)

    _isolate_load_config_env(monkeypatch)
    (tmp_path / "settings.toml").write_text('is_allowed_in_pytest = true\n\n[commands.create]\nenv = ["X=4"]\n')
    (tmp_path / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[commands.create]\nenv__extend = ["X=5"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert mngr_ctx.config.commands["create"].defaults["env"] == ["X=4", "X=5"]


# === load_config narrowing guard against agent_types / providers / create_templates ===
#
# These layer-level integration tests verify the guard fires uniformly across all
# of the container-dict mechanisms, not just commands.<cmd>.defaults. Each test
# follows the same shape: project layer sets a non-empty aggregate value on a
# named entry, local layer assigns over it with a different value, load_config
# must raise unless the user opts in (and ``__extend`` is the natural workaround).


def _setup_layered_test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[pluggy.PluginManager, Path]:
    """Shared boilerplate for the narrowing integration tests below.

    Returns a fresh plugin manager and the project-config dir to write TOML into.
    The autouse fixtures clamp HOME/MNGR_* so the loader can't pick up the
    developer's real config; we re-clamp the project-config dir to tmp_path.
    """
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    load_all_registries(pm)
    _isolate_load_config_env(monkeypatch)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))
    return pm, tmp_path


def test_load_config_narrowing_raises_on_agent_type_cli_args_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A local layer that re-assigns a non-empty ``agent_types.<name>.cli_args`` raises by default."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\nparent_type = "claude"\ncli_args = ["--debug"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\ncli_args = ["--verbose"]\n'
    )
    with pytest.raises(ConfigParseError, match="agent_types.my_claude.cli_args"):
        load_config(pm=pm, concurrency_group=cg)


def test_load_config_extend_avoids_narrowing_on_agent_type_cli_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """``cli_args__extend`` in the local layer preserves the project layer's entries
    and merges them in precedence order, without tripping the guard."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\nparent_type = "claude"\ncli_args = ["--debug"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\ncli_args__extend = ["--verbose"]\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    cli_args = mngr_ctx.config.agent_types[AgentTypeName("my_claude")].cli_args
    assert cli_args == ("--debug", "--verbose")


def test_load_config_narrowing_raises_on_create_template_options_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A local layer that re-assigns a non-empty list inside
    ``create_templates.<name>.options`` raises by default."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.dev]\nenv = ["X=1"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.dev]\nenv = ["X=2"]\n'
    )
    with pytest.raises(ConfigParseError, match="create_templates.dev"):
        load_config(pm=pm, concurrency_group=cg)


def test_load_config_extend_avoids_narrowing_on_create_template_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """``env__extend`` inside ``[create_templates.dev]`` walks through the
    ``options`` mapping and merges with the project layer's entries."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.dev]\nenv = ["X=1"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.dev]\nenv__extend = ["X=2"]\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    template = mngr_ctx.config.create_templates[CreateTemplateName("dev")]
    assert template.options["env"] == ["X=1", "X=2"]


def test_load_config_allows_adding_new_agent_type_in_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Adding a brand-new agent_type entry in the local layer never narrows --
    the per-key container merge preserves the project layer's entry alongside the
    new one. Sanity-check that the safety net doesn't fire on pure additions."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\nparent_type = "claude"\ncli_args = ["--debug"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_codex]\nparent_type = "codex"\ncli_args = ["--other"]\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert mngr_ctx.config.agent_types[AgentTypeName("my_claude")].cli_args == ("--debug",)
    assert mngr_ctx.config.agent_types[AgentTypeName("my_codex")].cli_args == ("--other",)


def test_load_config_extend_in_new_template_preserves_extend_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A template introduced in a single layer with ``env__extend = [...]`` keeps
    the ``__extend`` suffix in its options dict so ``apply_create_template`` can
    extend the runtime params at template-application time (rather than collapsing
    into a bare assign that would narrow over the create command's runtime env).
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.coder_local]\ntype = "claude"\nenv__extend = ["X=1"]\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    template = mngr_ctx.config.create_templates[CreateTemplateName("coder_local")]
    # The __extend suffix is preserved verbatim so apply_create_template can
    # interpret it at template-application time.
    assert template.options.get("env__extend") == ["X=1"]
    assert "env" not in template.options


def test_load_config_extend_in_new_template_extends_runtime_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """End-to-end check for the bug in
    test_load_config_extend_in_new_template_preserves_extend_suffix: loading a
    template whose only ``env__extend`` entry was introduced in a single layer
    should compose with the create command's runtime env (which itself comes from
    a separate config block) rather than narrowing over it.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n"
        "\n"
        '[commands.create]\nenv = ["RUNTIME=1"]\n'
        "\n"
        '[create_templates.coder_local]\ntype = "claude"\n'
        'env__extend = ["TEMPLATE=1"]\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    # Simulate the create command's params after apply_config_defaults: the
    # runtime env tuple has been populated from [commands.create].env.
    ctx = click.Context(click.Command("create"))
    params: dict[str, Any] = {
        "template": ("coder_local",),
        "env": ("RUNTIME=1",),
    }
    ctx.params = params
    for param_name in params:
        ctx.set_parameter_source(param_name, ParameterSource.DEFAULT)
    result = apply_create_template(ctx, params.copy(), mngr_ctx.config)
    assert result["env"] == ("RUNTIME=1", "TEMPLATE=1")


def test_load_config_string_cli_args_replacement_does_not_narrow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """When ``cli_args`` is given as a string (rather than a list) in the higher-
    precedence layer, the user's intent is scalar replacement of the whole command
    line, not list-narrowing. The string form represents a coherent single value
    and should not trigger the narrowing guard against the lower layer's tokenized
    tuple, even when the resulting tokens differ.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\nparent_type = "claude"\ncli_args = "--foo --bar"\n'
    )
    (project_dir / "settings.local.toml").write_text(
        'is_allowed_in_pytest = true\n\n[agent_types.my_claude]\ncli_args = "--baz"\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    assert mngr_ctx.config.agent_types[AgentTypeName("my_claude")].cli_args == ("--baz",)


# =============================================================================
# Tests for cross-scope settings_overrides narrowing (the SettingsPatchField case).
#
# A ``SettingsPatchField`` (claude's ``settings_overrides``) accumulates across
# config scopes, so the field as a whole is never a superset-narrowing. But a higher
# scope with a *bare* key nested inside the patch can still drop a non-empty aggregate a
# lower scope set -- that is real narrowing. These tests verify the overlay merge
# surfaces it through the loader's existing flag-gated aggregation.
# =============================================================================


# The claude agent type (``ClaudeAgentConfig``, with a ``SettingsPatchField``
# ``settings_overrides``) is registered by the autouse ``plugin_manager`` fixture --
# claude is in the default ``enabled_plugins`` set -- so these tests parse
# ``settings_overrides`` as a real accumulating patch field without any extra setup.
# Each agent_types block declares ``parent_type = "claude"`` in *every* layer that
# writes it, so the per-layer parse resolves ``my_claude`` to ``ClaudeAgentConfig``
# (a layer omitting ``parent_type`` would fall back to the base ``AgentTypeConfig``,
# which has no ``settings_overrides`` field).


def _write_settings_overrides_narrowing_config(project_dir: Path, *, higher_op: str | None = None) -> None:
    """Project layer sets ``settings_overrides.permissions.allow = ["A"]`` on a
    claude-derived agent type; local layer assigns ``["B"]`` (dropping ``A``).

    ``higher_op`` lets the caller add a ``__mngr_merge`` directive declaring
    ``permissions.allow`` as that op (e.g. ``"extend"``) to opt into additive behavior
    instead of the bare (narrowing) assign.
    """
    (project_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["A"]\n'
    )
    local = (
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["B"]\n'
    )
    if higher_op is not None:
        local += f'[agent_types.my_claude.settings_overrides.__mngr_merge]\n"permissions.allow" = "{higher_op}"\n'
    (project_dir / "settings.local.toml").write_text(local)


def test_load_config_narrowing_raises_on_settings_overrides_bare_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A local layer whose bare ``settings_overrides`` key drops a non-empty
    aggregate set by the project layer now raises the standard narrowing error.

    This is the previously-silent cross-scope ``settings_overrides`` drop: because the
    patch field accumulates, the drop only shows up *inside* it -- where the overlay
    merge surfaces it. The bare ``permissions`` dict in the local layer assign-replaces
    the project layer's ``permissions`` dict, so the narrowing is recorded at the
    ``permissions`` path.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    _write_settings_overrides_narrowing_config(project_dir)
    with pytest.raises(ConfigParseError) as exc_info:
        load_config(pm=pm, concurrency_group=cg)
    message = str(exc_info.value)
    assert "Settings narrowing detected" in message
    assert "agent_types.my_claude.settings_overrides.permissions" in message


def test_load_config_settings_overrides_narrowing_allowed_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Setting ``allow_settings_key_assignment_narrowing = true`` silences the
    cross-scope ``settings_overrides`` narrowing; the local layer's value wins."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        "allow_settings_key_assignment_narrowing = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["A"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["B"]\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    settings_overrides = mngr_ctx.config.agent_types[AgentTypeName("my_claude")].model_dump()["settings_overrides"]
    assert settings_overrides["permissions"]["allow"] == ["B"]


def test_load_config_extend_avoids_settings_overrides_narrowing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Declaring ``permissions.allow`` as ``extend`` in the local layer's ``__mngr_merge``
    map accumulates onto the project layer's entries rather than narrowing, so no opt-in
    is needed."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    _write_settings_overrides_narrowing_config(project_dir, higher_op="extend")
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    settings_overrides = mngr_ctx.config.agent_types[AgentTypeName("my_claude")].model_dump()["settings_overrides"]
    assert settings_overrides["permissions"]["allow"] == ["A", "B"]


def test_load_config_assign_avoids_settings_overrides_narrowing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A local layer declaring ``permissions`` as ``assign`` in its ``__mngr_merge`` map
    suppresses the cross-scope narrowing without any global opt-in -- the per-key
    replace-without-warning operator. The local value wins wholesale (the project's
    ``defaultMode`` is intentionally dropped), and no error is raised.

    Regression: the deferred ``__assign`` was previously collapsed to a bare assign at
    config-load whenever a lower scope already set the key, so the no-warn intent was lost
    and the guard errored on exactly the key the user opted out of.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["A"]\n'
        'defaultMode = "acceptEdits"\n'
    )
    (project_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides]\n"
        'permissions = { allow = ["B"] }\n'
        "[agent_types.my_claude.settings_overrides.__mngr_merge]\n"
        '"permissions" = "assign"\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    settings_overrides = mngr_ctx.config.agent_types[AgentTypeName("my_claude")].model_dump()["settings_overrides"]
    # The deferred ``__assign`` marker survives config-load (it resolves at provision);
    # the replacement value is the local layer's, and ``defaultMode`` is dropped.
    assert settings_overrides["permissions__assign"]["allow"] == ["B"]
    assert "defaultMode" not in settings_overrides["permissions__assign"]


def test_load_config_settings_overrides_accumulation_does_not_narrow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """A cross-scope ``settings_overrides`` that only *adds* a new key (a superset,
    never dropping a lower-scope aggregate) loads fine without any opt-in -- the
    accumulating-patch behavior is unchanged for the non-narrowing case."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["A"]\n'
    )
    (project_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides]\n"
        'model = "sonnet"\n'
    )
    mngr_ctx = load_config(pm=pm, concurrency_group=cg)
    settings_overrides = mngr_ctx.config.agent_types[AgentTypeName("my_claude")].model_dump()["settings_overrides"]
    # The lower scope's permissions patch is preserved (accumulation, not dropped)
    # and the higher scope's new key is added alongside it.
    assert settings_overrides["permissions"]["allow"] == ["A"]
    assert settings_overrides["model"] == "sonnet"


def test_load_config_settings_overrides_narrowing_error_attributes_both_sides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """The cross-scope ``settings_overrides`` narrowing error names both the
    assigning local layer and the dropped-from project layer, with scopes and
    paths, reusing the standard ``_build_narrowing_error`` attribution path."""
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    _write_settings_overrides_narrowing_config(project_dir)
    with pytest.raises(ConfigParseError) as exc_info:
        load_config(pm=pm, concurrency_group=cg)
    message = str(exc_info.value)
    # Assigning side: the local file, with its scope flag.
    assert "settings.local.toml" in message
    assert "mngr config set --scope local" in message
    # Dropped-from side: the project file, with its scope flag.
    assert "assigned by" in message
    assert "would drop a value from" in message
    assert "mngr config set --scope project" in message


def test_load_config_narrowing_attributes_dropped_from_for_suffixed_lower_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """When the dropped value was declared with an operator (here ``assign`` via
    ``__mngr_merge``) in a *deferred* settings-patch field, the narrowing's ``dropped_from``
    still names that layer. The lower layer's parsed dump carries the desugared suffixed key
    (``permissions__assign``) while the narrowing path is bare (``...permissions``), so
    provenance attribution must normalize the operator suffix to match.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides]\n"
        'permissions = { allow = ["A"] }\n'
        "[agent_types.my_claude.settings_overrides.__mngr_merge]\n"
        '"permissions" = "assign"\n'
    )
    (project_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[agent_types.my_claude]\nparent_type = "claude"\n'
        "[agent_types.my_claude.settings_overrides.permissions]\n"
        'allow = ["B"]\n'
    )
    with pytest.raises(ConfigParseError) as exc_info:
        load_config(pm=pm, concurrency_group=cg)
    message = str(exc_info.value)
    assert "settings_overrides.permissions" in message
    # The dropped value belongs to the project layer, which set it via ``permissions__assign``.
    assert "would drop a value from" in message
    assert "mngr config set --scope project" in message


# =============================================================================
# Tests for narrowing diagnostics: both-sides attribution (which layer assigns
# over which layer's dropped value).
# =============================================================================


def test_display_path_contracts_home_dir(tmp_path: Path) -> None:
    """A path under the user's home is shown with ``~``; a path outside home is
    shown absolute. The autouse env fixture already points HOME at ``tmp_path``.
    """
    assert _display_path(tmp_path / ".mngr" / "settings.toml") == "~/.mngr/settings.toml"
    outside = tmp_path.parent / "outside" / "settings.toml"
    assert _display_path(outside) == str(outside)


def test_collect_narrowing_attributes_highest_precedence_lower_layer() -> None:
    """The dropped-from side is the highest-precedence already-merged layer whose
    value the new layer narrows. Because the merge is assign-by-default, that
    layer holds the merged base value being dropped.
    """
    user_source = _FileSettingsSource(scope=ConfigScope.USER, path=Path("/u/settings.toml"))
    project_source = _FileSettingsSource(scope=ConfigScope.PROJECT, path=Path("/p/settings.toml"))
    local_source = _FileSettingsSource(scope=ConfigScope.LOCAL, path=Path("/p/settings.local.toml"))
    user_layer = MngrConfig(prefix="a-", commands={"create": CommandDefaults(defaults={"env": ["A"]})})
    project_layer = MngrConfig(prefix="a-", commands={"create": CommandDefaults(defaults={"env": ["A", "B"]})})
    # ``base`` is the accumulated merge the local layer is detected against; under
    # assign-by-default it equals the project layer's value (the highest prior).
    base, _ = user_layer.merge_with(project_layer)
    local_layer = MngrConfig(prefix="a-", commands={"create": CommandDefaults(defaults={"env": ["C"]})})

    _, narrowing_paths = base.merge_with(local_layer)
    # Build the provenance map the loader threads through its fold: each prior layer's
    # assigned paths, recorded in precedence order so the highest-precedence prior
    # owner wins.
    provenance: dict[str, _SettingsSource] = {}
    _record_provenance(provenance, user_layer, user_source)
    _record_provenance(provenance, project_layer, project_source)
    violations = _collect_narrowing(narrowing_paths, local_source, provenance)
    assert violations == [
        _NarrowingViolation(
            key_path="commands.create.defaults.env",
            assigned_by=local_source,
            dropped_from=project_source,
        )
    ]


def test_load_config_narrowing_error_names_both_sides_with_paths_and_scopes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """The narrowing error names the assigning layer and the layer whose value is
    dropped, each with the resolved file path and matching ``config set --scope``
    flag, so the user knows exactly which files are implicated.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    _write_two_layer_narrowing_config(project_dir, allow_narrowing=None)

    with pytest.raises(ConfigParseError) as exc_info:
        load_config(pm=pm, concurrency_group=cg)

    message = str(exc_info.value)
    # Paths are rendered with the home dir contracted to ``~`` (the test clamps
    # HOME to tmp_path, so these settings files live directly under it).
    # Assigning side: the local file, with its scope flag.
    assert "~/settings.local.toml" in message
    assert "mngr config set --scope local" in message
    # Dropped-from side: the project file, with its scope flag.
    assert "~/settings.toml" in message
    assert "mngr config set --scope project" in message
    assert "assigned by" in message
    assert "would drop a value from" in message


def test_load_config_narrowing_error_names_env_var_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    """When an ``MNGR__*`` env var narrows a file layer, the assigning side is named
    as the scopeless env layer (no path, no ``config set --scope`` flag) while the
    dropped-from side still names the file and its scope.
    """
    pm, project_dir = _setup_layered_test_env(monkeypatch, tmp_path)
    (project_dir / "settings.toml").write_text('is_allowed_in_pytest = true\n\n[commands.create]\nenv = ["X=4"]\n')
    monkeypatch.setenv("MNGR__COMMANDS__CREATE__ENV", '["X=5"]')

    with pytest.raises(ConfigParseError) as exc_info:
        load_config(pm=pm, concurrency_group=cg)

    message = str(exc_info.value)
    # Assigning side: the env layer, named as such with no path / scope flag.
    assert "assigned by MNGR__* environment variables" in message
    # Dropped-from side: the project file (home contracted to ``~``), with its scope flag.
    assert "~/settings.toml" in message
    assert "mngr config set --scope project" in message
