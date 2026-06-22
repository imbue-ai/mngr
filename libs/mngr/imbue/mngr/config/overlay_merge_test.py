"""Direct-assertion coverage for the overlay-backed config merge wiring.

These tests pin user-visible behaviors of the overlay node algebra as wired into
``MngrConfig.merge_with``: container-entry subclass preservation through a top-level
merge, and partial sub-model overrides carrying the base's unset sub-fields through
(rather than reverting them to defaults) without spuriously narrowing. Each asserts on
explicit values, not on a frozen reference.

Test instances are constructed the way the loader builds them: via ``model_construct``
with only the keys the layer "wrote", so ``model_fields_set`` is faithful and sparse
(exactly what both the merge and the pipeline's ``exclude_unset`` dump depend on).

The pydantic adapter these exercise (``config/overlay_merge.py``: ``model_dump`` ->
operator dict -> overlay -> ``model_validate``) is generic, not mngr-specific. We should
pull a general version of that pydantic-model logic into the ``overlay`` library itself
as a reusable adapter, so any pydantic consumer can merge models through the algebra
without reimplementing the serialize/re-mark/reparse glue.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from typing import Any

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.agent_config_registry import reset_agent_config_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.data_types import RetryConfig
from imbue.mngr.config.field_markers import SettingsPatchField
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.overlay_merge import build_settings_narrowing_message
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.config.overlay_merge import suffix_remediation
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.config.provider_config_registry import reset_provider_config_registry
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.logging import LoggingConfig


def _parse_layer(raw: dict[str, Any]) -> MngrConfig:
    """Parse a raw TOML-shaped dict into a ``MngrConfig`` the way the loader does
    (the padded ``parse_config`` construction), with no plugins disabled -- so the
    layer is built through the *real* padded path whose ``None`` scalars are the
    point of the top-level probe.
    """
    return parse_config(raw, frozenset())


def _initial_config() -> MngrConfig:
    """The loader's initial accumulator config: ``model_construct`` with defaults
    applied (so ``retry`` / ``logging`` are non-``None`` and scalars are defaulted),
    exactly as ``load_config`` builds its starting point before merging layers."""
    return MngrConfig.model_construct(
        prefix="mngr-",
        default_host_dir=Path("~/.mngr"),
        agent_types={},
        providers={},
        plugins={},
        logging=LoggingConfig(),
        commands={},
    )


def _base_from_layers(*raw_layers: dict[str, Any]) -> MngrConfig:
    """Build a base accumulator by merging ``raw_layers`` (each a raw TOML-shaped dict,
    parsed via the padded ``parse_config``) into the initial config using the
    production ``merge_with`` -- the genuine left-operand shape of a real merge."""
    config = _initial_config()
    for raw in raw_layers:
        config, _ = config.merge_with(_parse_layer(raw))
    return config


class _MngrClaudeLikeConfig(AgentTypeConfig):
    """A settings-bearing ``AgentTypeConfig`` subclass standing in for the real
    ``ClaudeAgentConfig``: carries a ``SettingsPatchField`` plus a subclass-only
    scalar, so the corpus exercises the settings-patch combine and subclass
    round-tripping inside ``agent_types`` entries without depending on the claude
    plugin package's parsing.
    """

    settings_overrides: Annotated[dict[str, Any], SettingsPatchField()] = Field(default_factory=dict)
    auto_dismiss_dialogs: bool | None = Field(default=None)


@pytest.fixture
def _registered_mngr_config_classes() -> Iterator[None]:
    """Register the stand-in container-entry config classes for the duration of each
    test, then reset the registries (test isolation). ``claude`` -> the settings-
    bearing subclass; ``docker`` -> the base provider config (so provider blocks
    parse). Unregistered plugin / command / create-template entries fall back to
    their base classes, which need no registration.
    """
    reset_agent_config_registry()
    reset_provider_config_registry()
    register_agent_config("claude", _MngrClaudeLikeConfig)
    register_provider_config("docker", ProviderInstanceConfig)
    try:
        yield
    finally:
        reset_agent_config_registry()
        reset_provider_config_registry()


@pytest.mark.usefixtures("_registered_mngr_config_classes")
def test_mngr_container_entry_subclass_is_preserved() -> None:
    """A ``claude`` (subclass) ``agent_types`` entry round-trips as the subclass
    through ``MngrConfig.merge_with``, so subclass-only fields survive."""
    base = _base_from_layers({"agent_types": {"c": {"parent_type": "claude", "auto_dismiss_dialogs": True}}})
    override = _parse_layer({"agent_types": {"c": {"parent_type": "claude", "cli_args": "--y"}}})
    actual, _ = base.merge_with(override)
    entry = actual.agent_types[AgentTypeName("c")]
    assert type(entry) is _MngrClaudeLikeConfig
    assert entry.auto_dismiss_dialogs is True


class _PluginWithDirectoryConfig(PluginConfig):
    """A ``PluginConfig`` subclass carrying a type-specific field, standing in for a real
    plugin config (e.g. ``LatchkeyPluginConfig``): exercises plugins-registry subclass
    round-tripping through ``MngrConfig.merge_with`` without depending on a plugin package.
    """

    directory: str | None = Field(default=None)


def test_mngr_plugins_registry_subclass_is_preserved() -> None:
    """A higher scope touching only the base ``enabled`` field of a ``plugins`` entry keeps
    the lower scope's subclass type and its type-specific fields: merging a subclass entry
    with a plain ``PluginConfig`` override round-trips as the subclass, carrying ``directory``
    through while taking ``enabled`` from the override. This is the plugins-registry analogue
    of ``test_mngr_container_entry_subclass_is_preserved`` (agent_types) and
    ``test_container_entry_submodel_carries_base_unset_fields`` (providers), consolidating the
    per-plugin ``merge_with`` subclass-preservation tests the overlay refactor retired
    (e.g. ``mngr_latchkey``'s ``test_merge_with_base_plugin_config_only_carries_enabled``).
    """
    base = MngrConfig.model_construct(
        prefix="m-", plugins={PluginName("my_plugin"): _PluginWithDirectoryConfig(directory="/base/dir")}
    )
    override = MngrConfig.model_construct(plugins={PluginName("my_plugin"): PluginConfig(enabled=False)})
    merged, narrowings = base.merge_with(override)
    entry = merged.plugins[PluginName("my_plugin")]
    assert type(entry) is _PluginWithDirectoryConfig
    assert entry.directory == "/base/dir"
    assert entry.enabled is False
    assert narrowings == []


def test_merge_models_via_overlay_rejects_mismatched_types() -> None:
    """The same-type guard raises when base and override are different concrete
    config types: the merge result reparses into ``type(base)``, so a sibling or
    more-derived override would silently lose fields.
    """
    with pytest.raises(ConfigParseError, match="Cannot merge"):
        merge_models_via_overlay(AgentTypeConfig(), _MngrClaudeLikeConfig())


def test_narrowing_message_tailors_extend_example_to_the_narrowed_key() -> None:
    """The config-load (suffix) remediation example is adapted to the user's actual key so it
    shows how to fix their config; a nested path nests the suffix, and it falls back to a
    generic example when no key is given."""
    top = build_settings_narrowing_message(
        ["  work_dir_extra_paths"], remediation=suffix_remediation("work_dir_extra_paths")
    )
    assert "work_dir_extra_paths__extend = ..." in top
    nested = build_settings_narrowing_message(
        ["  permissions.allow"], remediation=suffix_remediation("permissions.allow")
    )
    assert "permissions__extend = {allow__extend = ...}" in nested
    generic = build_settings_narrowing_message(["  something"], remediation=suffix_remediation())
    assert "permissions__extend = {allow__extend" in generic


# =============================================================================
# Sub-model field-by-field carry-through (the core regression)
# =============================================================================
#
# A partial sub-model override (setting only some of the sub-model's fields) must
# carry the base's *unset* sub-fields through rather than reverting them to defaults,
# and must NOT spuriously narrow. This is the regression these tests pin: the overlay
# integration had treated a sub-model field as a wholesale assign-leaf. The base sets a
# NON-DEFAULT value for a field the override leaves unset (built via ``model_construct``
# so ``model_fields_set`` is genuinely sparse, exactly as the loader's sub-model parsers
# produce).


def test_partial_logging_override_carries_base_unset_fields() -> None:
    """A ``logging`` override that sets only ``console_level`` keeps the base's
    non-default ``file_level`` (carried through) and surfaces no narrowing."""
    base = MngrConfig.model_construct(
        prefix="m-", logging=LoggingConfig(console_level=LogLevel.INFO, file_level=LogLevel.ERROR)
    )
    override = MngrConfig.model_construct(logging=LoggingConfig.model_construct(console_level=LogLevel.TRACE))
    merged, narrowings = base.merge_with(override)
    assert merged.logging.console_level == LogLevel.TRACE
    assert merged.logging.file_level == LogLevel.ERROR
    assert narrowings == []


def test_partial_retry_override_carries_base_unset_fields() -> None:
    """A ``retry`` override that sets only ``connect_retry_delay`` keeps the base's
    non-default ``connect_retry_times`` (carried through) and surfaces no narrowing."""
    base = MngrConfig.model_construct(prefix="m-", retry=RetryConfig(connect_retry_times=9, connect_retry_delay="5s"))
    override = MngrConfig.model_construct(retry=RetryConfig.model_construct(connect_retry_delay="30s"))
    merged, narrowings = base.merge_with(override)
    assert merged.retry.connect_retry_delay == "30s"
    assert merged.retry.connect_retry_times == 9
    assert narrowings == []


def test_partial_logging_override_via_loader_shaped_layers_carries_base() -> None:
    """The loader's real path: a lower scope sets ``logging.file_level`` and a higher
    scope sets only ``logging.console_level``; ``file_level`` must survive (carried
    through) with no narrowing, reproducing a project+local settings merge."""
    base = _base_from_layers({"logging": {"file_level": "ERROR"}})
    override = _parse_layer({"logging": {"console_level": "TRACE"}})
    merged, narrowings = base.merge_with(override)
    assert merged.logging.file_level == LogLevel.ERROR
    assert merged.logging.console_level == LogLevel.TRACE
    assert narrowings == []


class _NestedSubmodel(FrozenModel):
    """A leaf sub-model for ``_ProviderWithSubmodel`` (mirrors a provider's
    ``security_group`` shape: a nested ``BaseModel`` field on a container entry)."""

    name: str = Field(default="default")
    size_gb: int = Field(default=10)


class _ProviderWithSubmodel(ProviderInstanceConfig):
    """Stand-in provider subclass carrying a sub-model field, so the corpus exercises
    field-by-field sub-model merging *inside* a container entry without depending on the
    aws package (whose ``AwsProviderConfig.security_group`` is the real instance)."""

    volume: _NestedSubmodel = Field(default_factory=_NestedSubmodel)


def test_container_entry_submodel_carries_base_unset_fields() -> None:
    """A container entry's own sub-model field merges field-by-field: a base provider
    entry's ``volume.name`` survives when a higher layer sets only ``volume.size_gb``."""
    reset_provider_config_registry()
    register_provider_config("docker", _ProviderWithSubmodel)
    try:
        base = _base_from_layers(
            {"providers": {"p": {"backend": "docker", "volume": {"name": "data", "size_gb": 50}}}}
        )
        override = _parse_layer({"providers": {"p": {"backend": "docker", "volume": {"size_gb": 99}}}})
        merged, narrowings = base.merge_with(override)
        entry = merged.providers[ProviderInstanceName("p")]
        assert isinstance(entry, _ProviderWithSubmodel)
        assert entry.volume.size_gb == 99
        assert entry.volume.name == "data"
        assert narrowings == []
    finally:
        reset_provider_config_registry()
