import os
import re
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from typing import Sequence
from uuid import uuid4

import pluggy
from loguru import logger
from pydantic import BaseModel
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.agent_alias_registry import is_agent_alias
from imbue.mngr.config.agent_alias_registry import normalize_agent_type_name
from imbue.mngr.config.agent_alias_registry import unregister_agent_alias
from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.agent_config_registry import is_agent_config_registered
from imbue.mngr.config.consts import PROFILES_DIRNAME
from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.data_types import CreateCliOptions
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.data_types import RetryConfig
from imbue.mngr.config.data_types import TmuxConfig
from imbue.mngr.config.data_types import split_cli_args_string
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.config.key_resolver import set_at_path
from imbue.mngr.config.overlay_merge import build_settings_narrowing_message
from imbue.mngr.config.overlay_merge import suffix_remediation
from imbue.mngr.config.plugin_registry import get_plugin_config_class
from imbue.mngr.config.pre_readers import read_config_layers
from imbue.mngr.config.pre_readers import read_disabled_plugins
from imbue.mngr.config.pre_readers import resolve_project_config_dir
from imbue.mngr.config.pre_readers import try_load_toml
from imbue.mngr.config.provider_config_registry import get_provider_config_class
from imbue.mngr.config.provider_config_registry import list_registered_provider_backend_names
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.errors import UserInputError
from imbue.mngr.plugin_catalog import get_plugin_install_hint
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import PluginKind
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.env_utils import parse_bool_env
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.git_utils import find_git_worktree_root
from imbue.mngr.utils.logging import LoggingConfig
from imbue.overlay.markers import ScalarTuple
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key
from imbue.overlay.operators import parse_scalar_value

# Prefix and shape for dynamic ``MNGR__*`` env var overrides. Each
# ``__``-separated segment after the prefix is lowercased and treated as a
# normalized config key. A trailing ``__EXTEND`` segment is the operator suffix
# documented in ``key_resolver.py``.
_ENV_OVERRIDE_PREFIX: Final[str] = "MNGR__"
_ENV_OVERRIDE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^MNGR__[A-Z0-9_]+(__[A-Z0-9_]+)*$")

# Old-style ``MNGR_*`` env vars that remain accepted as documented aliases for
# specific ``MngrConfig`` fields. Each entry maps the env var name to the dotted
# config path it sets and a value parser. The synthesis step raises
# ``ConfigParseError`` when both the alias and the canonical ``MNGR__*`` form
# are set with different (parsed) values.
#
# The value parser preserves each alias's historic value semantics:
# ``MNGR_HEADLESS`` accepted "1"/"true"/"yes" as truthy long before the new
# ``MNGR__*`` scheme, so it keeps using ``parse_bool_env``. The string-valued
# aliases (``MNGR_PREFIX``, ``MNGR_HOST_DIR``) are JSON-parsed-with-string-fallback
# like every other ``MNGR__*`` value, since they were always plain strings.
_PRESERVED_ALIASES: Final[dict[str, tuple[str, Callable[[str], Any]]]] = {
    "MNGR_HEADLESS": ("headless", parse_bool_env),
    "MNGR_PREFIX": ("prefix", parse_scalar_value),
    "MNGR_HOST_DIR": ("default_host_dir", parse_scalar_value),
}


class _FileSettingsSource(FrozenModel):
    """A TOML settings-file layer for narrowing diagnostics.

    ``scope`` is the :class:`ConfigScope` the file belongs to (which is exactly
    what ``mngr config set --scope`` accepts) and ``path`` is the resolved file
    path. The human-readable label is derived from ``scope`` in
    ``_describe_source`` rather than stored, so the two can't drift.
    """

    scope: ConfigScope
    path: Path


class _EnvSettingsSource(FrozenModel):
    """The ``MNGR__*`` environment-variable layer: not a file, so it carries no
    path and has no ``config set`` scope.
    """


# A settings layer the narrowing guard can attribute a value to. The loader only
# ever deals with these two; ``--setting`` narrowing is a separate path
# (``apply_settings_to_config``) that does not use this type.
_SettingsSource = _FileSettingsSource | _EnvSettingsSource


class _NarrowingViolation(FrozenModel):
    """A single narrowing assignment, with both sides attributed.

    ``assigned_by`` is the higher-precedence layer doing the (narrowing)
    assignment; ``dropped_from`` is the lower-precedence layer whose value would
    be silently dropped (``None`` only if no contributing layer could be
    identified, which should not happen for a real violation).
    """

    key_path: str
    assigned_by: _SettingsSource
    dropped_from: _SettingsSource | None = None


def load_config(
    pm: pluggy.PluginManager,
    concurrency_group: ConcurrencyGroup,
    enabled_plugins: Sequence[str] | None = None,
    disabled_plugins: Sequence[str] | None = None,
    is_interactive: bool = False,
    strict: bool | None = None,
    silent_unknown_fields: bool = False,
    enforce_narrowing_guard: bool = True,
) -> MngrContext:
    """Load and merge configuration from all sources.

    Precedence (lowest to highest):
    1. Built-in MngrConfig defaults
    2. User config (~/.{root_name}/profiles/<profile_id>/settings.toml)
    3. Project config (.{root_name}/settings.toml at the git root or MNGR_PROJECT_CONFIG_DIR)
    4. Local config (.{root_name}/settings.local.toml at the git root or MNGR_PROJECT_CONFIG_DIR)
    5. MNGR__* env vars (each ``__``-separated segment after ``MNGR__`` maps to a dotted config
       key; values are JSON-parsed with raw-string fallback) plus the preserved aliases
       ``MNGR_PREFIX``, ``MNGR_HOST_DIR``, and ``MNGR_HEADLESS`` (synthesised into the same form
       via _collect_env_overrides). See docs/concepts/environment_variables.md for the full surface.
    6. ``--setting KEY=VALUE`` CLI overrides (applied later in setup_command_context)
    7. CLI arguments (handled by caller)

    Note: the narrowing guard below runs over layers 2-5 (the config files and
    env vars) only. It does NOT see the layer-6 ``--setting`` overrides, which
    are merged afterwards in ``setup_command_context``. ``--setting`` cannot
    fully resolve until this function has produced the config it extends against
    (``__extend`` keys resolve against the loaded config), so it deliberately
    runs after. Consequently ``allow_settings_key_assignment_narrowing`` can only
    be opted into via a settings file or the ``MNGR__*`` env var, not via
    ``--setting`` (see the error message and changelog).

    MNGR_ROOT_NAME is read before config-file resolution to derive:
    1. Config file paths (where to look for settings files)
    2. Defaults for prefix and default_host_dir (if not set in config files)

    Explicit MNGR_PREFIX/MNGR_HOST_DIR values override MNGR_ROOT_NAME-derived defaults.

    MNGR_PROJECT_CONFIG_DIR overrides where project settings are found. When set, project
    and local config files are loaded from that directory instead of .{root_name}/
    at the git root.

    Returns MngrContext containing both the final MngrConfig and a reference to the plugin manager.
    """

    # Read MNGR_ROOT_NAME early to use for config file discovery
    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")

    # Determine base directory (may be overridden by env var)
    base_dir = read_default_host_dir()

    # Get/create profile directory first (needed for user config
    profile_dir = get_or_create_profile_dir(base_dir)

    # Pre-compute disabled plugins so _parse_providers can skip them.
    # This uses the same lightweight pre-reader that create_plugin_manager() uses.
    config_disabled_plugins = read_disabled_plugins()

    # Start with base config that has defaults based on root_name
    # Use model_construct with None to allow merging to work properly
    config = MngrConfig.model_construct(
        prefix=f"{root_name}-",
        default_host_dir=Path(f"~/.{root_name}"),
        agent_types={},
        providers={},
        plugins={},
        logging=LoggingConfig(),
        tmux=TmuxConfig(),
        commands={},
    )

    if strict is None:
        strict = resolve_strict_from_env()

    # Read the user/project/local config layers (in precedence order) through
    # read_config_layers -- the single chokepoint that applies the pytest config
    # guard -- so a real (non-test) config can never be loaded here during a test
    # run. The project root is resolved from the cwd's git worktree root (or
    # MNGR_PROJECT_CONFIG_DIR). Each layer carries its resolved path and its
    # ``config set --scope`` value so narrowing diagnostics can name the actual
    # file rather than an opaque layer label.
    project_config_dir = resolve_project_config_dir(root_name, concurrency_group)
    loaded_layers = read_config_layers(profile_dir, project_config_dir)

    # Merge config files in precedence order (user, project, local). Narrowing
    # violations -- a higher-precedence layer assigning over a non-empty aggregate
    # value from a lower-precedence layer -- are collected as we go, then turned
    # into a single error after all layers are merged (when the final
    # ``allow_settings_key_assignment_narrowing`` resolves to False).
    # ``provenance`` maps each assigned path to the highest-precedence source that
    # has assigned it so far, so each violation can be attributed to the specific
    # lower-precedence layer whose value is being dropped.
    narrowing_violations: list[_NarrowingViolation] = []
    provenance: dict[str, _SettingsSource] = {}
    for scope, config_path, raw in loaded_layers:
        file_source = _FileSettingsSource(scope=scope, path=config_path)
        parsed_layer = _parse_config_with_extends(
            raw,
            base_config=config,
            disabled_plugins=config_disabled_plugins,
            strict=strict,
            silent=silent_unknown_fields,
        )
        config, narrowing_paths = config.merge_with(parsed_layer)
        # Read provenance BEFORE this layer updates it: a dropped value belongs to
        # a prior layer, so its source is whatever held the path before now.
        narrowing_violations.extend(_collect_narrowing(narrowing_paths, file_source, provenance))
        _record_provenance(provenance, parsed_layer, file_source)

    # Apply ``MNGR__*`` env-var overrides plus the preserved-alias env vars
    # (MNGR_PREFIX, MNGR_HOST_DIR, MNGR_HEADLESS). These all flow through the
    # shared key resolver so assign vs extend semantics match TOML and
    # ``--setting``. Conflicts between an alias and its canonical ``MNGR__*``
    # form raise ConfigParseError.
    env_override_raw = _collect_env_overrides(os.environ)
    if env_override_raw:
        env_source = _EnvSettingsSource()
        parsed_env_layer = _parse_config_with_extends(
            env_override_raw,
            base_config=config,
            disabled_plugins=config_disabled_plugins,
            strict=strict,
            silent=silent_unknown_fields,
        )
        config, env_narrowing_paths = config.merge_with(parsed_env_layer)
        narrowing_violations.extend(_collect_narrowing(env_narrowing_paths, env_source, provenance))
        _record_provenance(provenance, parsed_env_layer, env_source)

    # Raise on collected narrowing assignments unless the user has opted in. Skipped when
    # ``enforce_narrowing_guard`` is False (the ``mngr config`` command, which must be able
    # to load a narrowing config in order to *edit* it -- otherwise the guard is a catch-22
    # that blocks the very ``config set``/``unset`` that would resolve it).
    # Done before further config_dict mutation so the error surfaces with the
    # actual settings-file paths in the message.
    if narrowing_violations and enforce_narrowing_guard and not config.allow_settings_key_assignment_narrowing:
        raise _build_narrowing_error(narrowing_violations)

    # Build a dict with non-None values for final validation.
    config_dict: dict[str, Any] = {}
    if config.prefix is not None:
        config_dict["prefix"] = config.prefix
    if config.default_host_dir is not None:
        config_dict["default_host_dir"] = config.default_host_dir

    # Always include agent_types, providers, plugins, commands, and create_templates (they default to empty dicts)
    config_dict["agent_types"] = config.agent_types
    config_dict["providers"] = config.providers
    config_dict["plugins"] = config.plugins
    config_dict["commands"] = config.commands
    config_dict["create_templates"] = config.create_templates

    # Apply CLI plugin overrides
    config_dict["plugins"], cli_disabled_plugins = _apply_plugin_overrides(
        config_dict["plugins"],
        enabled_plugins,
        disabled_plugins,
    )

    # Block the CLI/[plugins.*] disabled set so their hooks don't fire. This covers
    # CLI-level --disable-plugin flags that weren't known at startup; is_strict
    # catches --disable-plugin typos (a name that is neither registered nor already
    # blocked). Opt-in plugins are intentionally excluded here: create_plugin_manager
    # already blocked them, so re-blocking would add nothing in production while
    # tripping the strict check in the bare-pm path.
    block_disabled_plugins(pm, cli_disabled_plugins, is_strict=True)

    # Record disabled_plugins as the faithful union with the opt-in-derived set
    # (OPT_IN_PLUGINS not explicitly enabled). _apply_plugin_overrides only sees
    # [plugins.*] enabled flags, so on its own the field drops opt-in plugins (e.g.
    # claude_subagent_proxy) that create_plugin_manager blocks -- which made
    # `mngr plugin list` mislabel them enabled. Gated on MNGR_LOAD_ALL_PLUGINS
    # exactly like create_plugin_manager, so tooling that loads every plugin keeps
    # them enabled here too. Blocking is one-way (there is no unblock), so the union
    # also stays truthful under --enable-plugin, which cannot un-block an opt-in plugin.
    if parse_bool_env(os.environ.get("MNGR_LOAD_ALL_PLUGINS", "")):
        config_dict["disabled_plugins"] = cli_disabled_plugins
    else:
        config_dict["disabled_plugins"] = cli_disabled_plugins | config_disabled_plugins

    # Include retry if not None
    if config.retry is not None:
        config_dict["retry"] = config.retry

    # Include logging if not None
    if config.logging is not None:
        config_dict["logging"] = config.logging

    # Include tmux if not None
    if config.tmux is not None:
        config_dict["tmux"] = config.tmux

    config_dict["unset_vars"] = config.unset_vars
    config_dict["pager"] = config.pager
    config_dict["enabled_backends"] = config.enabled_backends
    config_dict["connect_command"] = config.connect_command
    config_dict["is_remote_agent_installation_allowed"] = config.is_remote_agent_installation_allowed
    config_dict["is_nested_tmux_allowed"] = config.is_nested_tmux_allowed
    config_dict["headless"] = config.headless
    config_dict["is_error_reporting_enabled"] = config.is_error_reporting_enabled
    config_dict["is_allowed_in_pytest"] = config.is_allowed_in_pytest
    config_dict["pre_command_scripts"] = config.pre_command_scripts
    config_dict["work_dir_extra_paths"] = config.work_dir_extra_paths
    config_dict["default_destroyed_host_persisted_seconds"] = config.default_destroyed_host_persisted_seconds
    config_dict["default_min_online_host_age_seconds"] = config.default_min_online_host_age_seconds
    config_dict["agent_ready_timeout"] = config.agent_ready_timeout
    config_dict["allow_settings_key_assignment_narrowing"] = config.allow_settings_key_assignment_narrowing

    # Allow plugins to modify config_dict before validation
    pm.hook.on_load_config(config_dict=config_dict)

    # Validate and apply defaults using normal constructor
    final_config = MngrConfig.model_validate(config_dict)

    # Resolve project root for use as cwd in pre-command scripts.
    # Note: MNGR_PROJECT_CONFIG_DIR is NOT used here because it points to the config
    # directory (containing settings.toml), not the project root.
    project_root = find_git_worktree_root(start=None, cg=concurrency_group)

    # Return MngrContext containing both config and plugin manager
    return MngrContext(
        config=final_config,
        pm=pm,
        is_interactive=is_interactive,
        profile_dir=profile_dir,
        concurrency_group=concurrency_group,
        project_root=project_root,
    )


def get_or_create_profile_dir(base_dir: Path) -> Path:
    """Get or create the profile directory for this mngr installation.

    The profile directory is stored at ~/.mngr/profiles/<profile_id>/. The active
    profile is specified in ~/.mngr/config.toml. If no profile exists, a new one
    is created with a generated profile ID and saved to config.toml.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = base_dir / PROFILES_DIRNAME
    profiles_dir.mkdir(parents=True, exist_ok=True)

    config_path = base_dir / ROOT_CONFIG_FILENAME
    root_config = try_load_toml(config_path)
    if root_config is not None:
        profile_id = root_config.get("profile")
        if profile_id:
            profile_dir = profiles_dir / profile_id
            profile_dir.mkdir(parents=True, exist_ok=True)
            return profile_dir

    # No valid config.toml or no profile specified -- create a new profile
    profile_id = uuid4().hex
    profile_dir = profiles_dir / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)

    atomic_write(config_path, f'profile = "{profile_id}"\n')

    return profile_dir


# =============================================================================
# Config Loading
# =============================================================================


def _assigned_paths(parsed_layer: MngrConfig) -> list[str]:
    """Return every dotted node-and-leaf path the layer assigns, in the same format
    as the overlay merge's narrowing paths.

    Derived from the layer's sparse dump (``exclude_unset=True``, ``serialize_as_any=True``)
    -- the exact dump the merge lifts to compute narrowings -- so a narrowing path the
    merge surfaces (which may be a field prefix for a whole-aggregate replacement or a
    deep leaf for a same-keys nested drop; see ``overlay.narrowing.narrowing_paths``) is
    always one of these paths. Recording every node and leaf, rather than only leaves,
    is what lets a prefix-level narrowing path find its owner.
    """
    dumped = parsed_layer.model_dump(exclude_unset=True, serialize_as_any=True)
    return list(_walk_dotted_paths(dumped, ()))


def _strip_operator_suffix(key: str) -> str:
    """Strip an outermost ``__extend`` / ``__assign`` operator suffix from a single key.

    A *deferred* settings-patch field (``settings_overrides`` / ``create_templates``)
    carries its operator markers verbatim into the parsed layer's dump, so a path like
    ``...permissions.allow__assign`` would not match the bare ``...permissions.allow`` the
    overlay narrowing reports. Normalizing the suffix (the same single-strip ``lift`` does)
    lets provenance attribute the dropped value to the layer that set it via an operator.
    """
    if is_extend_key(key):
        return bare_key(key)
    if is_assign_key(key):
        return assign_bare_key(key)
    return key


def _walk_dotted_paths(value: Any, prefix: tuple[str, ...]) -> "Iterator[str]":
    """Yield the dotted path of every node and leaf reachable in ``value``.

    Recurses into dict values (the only mapping shape config dumps produce), yielding a
    path for each nested dict node as well as its leaves; non-dict values are leaves.
    Each key segment has its operator suffix normalized (``_strip_operator_suffix``) so the
    paths match the bare paths the overlay narrowing reports. The empty root path is not
    yielded.
    """
    if prefix:
        yield ".".join(prefix)
    if isinstance(value, dict):
        for key, sub_value in value.items():
            yield from _walk_dotted_paths(sub_value, prefix + (_strip_operator_suffix(str(key)),))


def _record_provenance(
    provenance: dict[str, _SettingsSource],
    parsed_layer: MngrConfig,
    source: _SettingsSource,
) -> None:
    """Mark ``source`` as the owner of every path this layer assigns.

    Called after a layer is folded in (and after its narrowings are attributed against
    the prior provenance), so each path maps to the highest-precedence source that has
    assigned it so far.
    """
    for path in _assigned_paths(parsed_layer):
        provenance[path] = source


def _collect_narrowing(
    narrowing_paths: Sequence[str],
    source: _SettingsSource,
    provenance: Mapping[str, _SettingsSource],
) -> list["_NarrowingViolation"]:
    """Build narrowing violations for the cross-scope bare-drops the overlay merge
    surfaced, attributing each side.

    ``narrowing_paths`` is the full list ``MngrConfig.merge_with`` returned
    for merging this layer onto the accumulated config -- both assign-by-default field
    drops and ``SettingsPatchField`` drops inside an accumulating patch. ``source`` is the
    layer doing the assignment. ``dropped_from`` is read from ``provenance`` (the
    highest-precedence prior layer that assigned the path), which must reflect state
    *before* this layer's own assignments are recorded -- the dropped value belongs to a
    prior layer. ``dropped_from`` is ``None`` only if no prior layer owns the path (should
    not happen for a real violation, but keeps the diagnostic robust).
    """
    return [
        _NarrowingViolation(key_path=key_path, assigned_by=source, dropped_from=provenance.get(key_path))
        for key_path in narrowing_paths
    ]


def _display_path(path: Path) -> str:
    """Render ``path`` with the user's home directory contracted to ``~`` (e.g.
    ``~/.mngr/profiles/<id>/settings.toml``), falling back to the absolute path
    when it is not under home. Keeps the narrowing error readable and avoids
    spelling out the full home path.
    """
    home = Path.home()
    if path.is_relative_to(home):
        return f"~/{path.relative_to(home)}"
    return str(path)


def _describe_source(source: _SettingsSource) -> str:
    """Render a settings layer for the narrowing error.

    A TOML file layer is described as ``<scope> settings (<path>) [edit with:
    mngr config set --scope <scope> ...]``; the ``MNGR__*`` env-var layer is
    named as such.
    """
    match source:
        case _FileSettingsSource(scope=scope, path=path):
            scope_flag = scope.value.lower()
            return (
                f"{scope_flag} settings ({_display_path(path)}) [edit with: mngr config set --scope {scope_flag} ...]"
            )
        case _EnvSettingsSource():
            return "MNGR__* environment variables"


def _build_narrowing_error(violations: Sequence["_NarrowingViolation"]) -> ConfigParseError:
    """Construct the user-facing error raised when a higher-precedence layer
    silently narrows a non-empty aggregate value.

    For each offending key it names both sides -- the file/scope doing the
    assignment and the file/scope whose value would be dropped -- then explains
    how to opt in to the assign-by-default semantics and points at the
    ``__extend`` / ``__assign`` operators for the additive / replace opt-outs.
    """
    detail_lines: list[str] = []
    for violation in violations:
        detail_lines.append(f"  {violation.key_path}")
        detail_lines.append(f"      assigned by {_describe_source(violation.assigned_by)}")
        if violation.dropped_from is not None:
            detail_lines.append(f"      would drop a value from {_describe_source(violation.dropped_from)}")
    example_key_path = violations[0].key_path if violations else None
    return ConfigParseError(
        build_settings_narrowing_message(detail_lines, remediation=suffix_remediation(example_key_path))
    )


def resolve_strict_from_env() -> bool:
    """Return the strict policy implied by the MNGR_ALLOW_UNKNOWN_CONFIG env var.

    Strict (True) is the default. When MNGR_ALLOW_UNKNOWN_CONFIG is set to a
    truthy value, unknown fields produce warnings instead of errors, which is
    useful when older mngr installations encounter newer config files.

    Centralized here so that ``load_config`` and ``setup_command_context`` agree
    on the policy and the env var is read in exactly one place.
    """
    return not parse_bool_env(os.environ.get("MNGR_ALLOW_UNKNOWN_CONFIG", ""))


def _normalize_field_keys(raw: dict[str, Any], context: str) -> dict[str, Any]:
    """Replace hyphens with underscores in dict keys.

    TOML conventionally uses hyphens (``pass-env``), but Python dataclasses use
    underscores (``pass_env``). Normalize so both forms map to the same field.

    Also enforces two invariants that keep ``MNGR__*`` env-var lookups
    unambiguous and round-trippable with TOML:

    - Field names cannot themselves contain ``__`` (except as the trailing
      ``__extend`` operator suffix). Two consecutive underscores in a field
      name would collide with the segment separator in env-var form.
    - Sibling keys that lowercase-collapse to the same env-var segment form
      (e.g. ``MyAgent`` and ``my-agent`` both normalising to ``my_agent``)
      raise so the caller picks one canonical spelling.

    Always returns a fresh dict, so callers can freely mutate the result
    (e.g. via ``pop`` in ``parse_config``) without affecting the caller's input.
    """
    result: dict[str, Any] = {}
    seen_normalized: dict[str, str] = {}
    seen_casefolded: dict[str, str] = {}
    for key, value in raw.items():
        normalized = key.replace("-", "_")
        # Strip an ``__extend`` suffix before checking the field-name shape;
        # the operator suffix is the one place ``__`` is legitimately allowed.
        field_part = normalized[: -len(EXTEND_SUFFIX)] if normalized.endswith(EXTEND_SUFFIX) else normalized
        if "__" in field_part:
            raise ConfigParseError(
                f"Config in {context} has key '{key}' containing '__' in its field name. "
                "Field names cannot contain consecutive underscores; '__' is reserved as "
                "the env-var segment separator and the '__extend' operator suffix."
            )
        if normalized in seen_normalized:
            raise ConfigParseError(
                f"Config in {context} has both '{seen_normalized[normalized]}' and '{key}' "
                f"which both normalize to '{normalized}'. Use one or the other."
            )
        casefolded = normalized.lower()
        if casefolded in seen_casefolded:
            raise ConfigParseError(
                f"Config in {context} has both '{seen_casefolded[casefolded]}' and '{key}' "
                f"which collapse to the same env-var segment '{casefolded.upper()}'. "
                "Pick one canonical spelling."
            )
        result[normalized] = value
        seen_normalized[normalized] = key
        seen_casefolded[casefolded] = key
    return result


def _drop_unknown_fields(
    raw_config: dict[str, Any],
    model_class: type[BaseModel],
    context: str,
    *,
    strict: bool = True,
    silent: bool = False,
    extra_hint: str | None = None,
) -> dict[str, Any]:
    """Return ``raw_config`` keeping only fields declared on ``model_class``.

    When strict=True (used by ``config set`` to catch typos), raises
    ConfigParseError on any unknown field. When strict=False, logs a warning and
    returns a copy with the unknown fields removed, so config files written for
    newer versions of mngr don't break older versions. When silent=True (and
    strict=False), suppresses the warning entirely -- used by ``mngr plugin add``,
    where the config is expected to reference plugins that are not yet installed;
    the warnings are noise that resolve themselves once the install completes.

    The input dict is left untouched; the returned dict is the one to use (it is
    the same object when there are no unknown fields).

    `extra_hint` is appended to the error/warning message after the field listing
    when there are unknown fields. Used to suggest causes (e.g. a missing plugin).
    """
    known_fields = set(model_class.model_fields.keys())
    unknown = set(raw_config.keys()) - known_fields
    if not unknown:
        return raw_config
    base_msg = f"Unknown fields in {context}: {sorted(unknown)}. Valid fields: {sorted(known_fields)}"
    full_msg = f"{base_msg}\n{extra_hint}" if extra_hint else base_msg
    if strict:
        raise ConfigParseError(full_msg)
    if not silent:
        logger.warning(full_msg)
    return {k: v for k, v in raw_config.items() if k not in unknown}


def _parse_providers(
    raw_providers: dict[str, dict[str, Any]],
    disabled_plugins: frozenset[str],
    *,
    strict: bool = True,
    silent: bool = False,
) -> dict[ProviderInstanceName, ProviderInstanceConfig]:
    """Parse provider configs using the registry.

    Validates each block with ``model_validate`` so raw TOML scalars are coerced
    to their declared field types (e.g. ``builder = "DEPOT"`` to
    ``DockerBuilder.DEPOT``, ``allowed_ssh_cidrs = [...]`` to a tuple). Only the
    keys actually present in the block are recorded in ``model_fields_set``, so
    per-field config-layer merging works.
    Provider blocks whose plugin is disabled are silently skipped.
    Provider blocks with is_enabled=false whose backend plugin is not installed
    are also skipped, since there is no config class to resolve for a disabled
    provider.  When the backend IS installed, is_enabled=false is preserved in
    the parsed config so that config layer merging works correctly.
    """
    providers: dict[ProviderInstanceName, ProviderInstanceConfig] = {}
    known_backends = set(list_registered_provider_backend_names())

    for name, raw_config in raw_providers.items():
        raw_config = _normalize_field_keys(raw_config, f"providers.{name}")
        backend = raw_config.get("backend") or name
        plugin = raw_config.get("plugin") or backend
        if plugin in disabled_plugins:
            continue
        # Skip disabled providers whose backend plugin is not installed.
        # We cannot skip unconditionally because is_enabled=false must be
        # preserved in the parsed config when the backend IS installed,
        # otherwise config layer merging would lose the override.
        if raw_config.get("is_enabled") is False and backend not in known_backends:
            continue
        try:
            config_class = get_provider_config_class(backend)
        except UnknownBackendError as e:
            msg = f"Provider '{name}' references unknown backend '{backend}'."
            if backend in disabled_plugins:
                msg += (
                    f" The '{backend}' plugin is currently disabled. Either enable"
                    f' the plugin or add `plugin = "{backend}"` to this provider'
                    f" block so it is skipped when the plugin is disabled."
                )
            elif disabled_plugins:
                msg += (
                    f" If this backend is provided by a disabled plugin, either enable"
                    f' the plugin or add `plugin = "<plugin-name>"` to this provider'
                    f" block. Currently disabled plugins: {', '.join(sorted(disabled_plugins))}"
                )
            else:
                msg += f" {get_plugin_install_hint(backend, kind=PluginKind.PROVIDER)}"
            if strict:
                raise ConfigParseError(msg) from e
            if not silent:
                logger.warning(msg)
            continue
        # Drop unknown fields (raising in strict mode), leaving only known fields
        # for model_validate to coerce. Coercion matters because an uncoerced enum
        # like ``builder = "DEPOT"`` fails its ``is``-identity check against
        # ``DockerBuilder.DEPOT``, and an uncoerced nested table like
        # ``SSHProviderConfig.hosts`` stays a raw dict and crashes with
        # ``AttributeError: 'dict' object has no attribute ...`` the moment the
        # backend touches it.
        cleaned_config = _drop_unknown_fields(
            raw_config, config_class, f"providers.{name}", strict=strict, silent=silent
        )
        try:
            providers[ProviderInstanceName(name)] = config_class.model_validate(cleaned_config)
        except ValidationError as e:
            # A malformed known field (bad scalar, failed validator, malformed
            # nested host table, ...) is always fatal: surface it as a clear
            # parse-time ConfigParseError keyed on the provider block rather than
            # a raw pydantic ValidationError or a late AttributeError from the
            # backend.
            raise ConfigParseError(f"Invalid config for 'providers.{name}': {e}") from e

    return providers


# Tuple-typed fields on AgentTypeConfig that need explicit list->tuple coercion
# before model_construct (which bypasses pydantic's normal validators).
# ``cli_args`` is handled separately because it also supports shell-splitting
# of a single string into multiple arguments.
_PLAIN_TUPLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"extra_provision_command", "upload_file", "create_directory", "env", "env_file"}
)


def _normalize_tuple_fields_for_construct(raw_config: dict[str, Any]) -> dict[str, Any]:
    """Normalize tuple fields from str or list to tuple before model_construct (which bypasses validators).

    cli_args gets special shell-splitting behavior for single strings.
    All other tuple fields just convert list -> tuple.

    When the source value is a string, the result is a ``ScalarTuple`` (a ``Static*``
    marker) so the overlay narrowing detector recognises the scalar-replacement intent
    and exempts it from the per-entry narrowing check against the lower-precedence layer.
    """
    result = raw_config
    if "cli_args" in result:
        cli_args = result["cli_args"]
        if isinstance(cli_args, str):
            tokens = split_cli_args_string(cli_args) if cli_args else ()
            normalized: Any = ScalarTuple(tokens)
        elif isinstance(cli_args, (list, tuple)):
            normalized = tuple(cli_args)
        else:
            normalized = cli_args
        result = {**result, "cli_args": normalized}

    for field_name in _PLAIN_TUPLE_FIELDS:
        if field_name not in result:
            continue
        value = result[field_name]
        if isinstance(value, str):
            # Single string -> wrap in a one-element tuple (no shell splitting for these fields)
            result = {**result, field_name: ScalarTuple((value,))}
        elif isinstance(value, (list, tuple)):
            result = {**result, field_name: tuple(value)}
        else:
            # Unrecognized type: pass through for Pydantic to validate or reject
            pass
    return result


def _has_disabled_ancestor(
    name: str,
    raw_types: dict[str, dict[str, Any]],
    disabled_plugins: frozenset[str],
) -> bool:
    """Check if an agent type or any ancestor in its parent chain is disabled.

    At each level, uses the explicit ``plugin`` field if set, otherwise
    falls back to ``parent_type`` (if set) or the type name -- mirroring
    how ``_parse_providers`` resolves the plugin for a provider block.
    """
    current: str | None = name
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        raw = raw_types.get(current)
        # Determine the plugin identity for this level.
        plugin: str | None = raw.get("plugin") if raw is not None else None
        if plugin is not None:
            # Explicit plugin field -- check it and stop walking (the field
            # already tells us which plugin this whole sub-chain depends on).
            return plugin in disabled_plugins
        if current in disabled_plugins:
            return True
        current = raw.get("parent_type") if raw is not None else None
    return False


def _parse_agent_types(
    raw_types: dict[str, dict[str, Any]],
    disabled_plugins: frozenset[str],
    *,
    strict: bool = True,
    silent: bool = False,
) -> dict[AgentTypeName, AgentTypeConfig]:
    """Parse agent type configs using the registry.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    Agent type blocks whose plugin is disabled are silently skipped.
    """
    agent_types: dict[AgentTypeName, AgentTypeConfig] = {}

    # Normalize hyphens in field names up front so _has_disabled_ancestor can
    # read normalized `plugin` / `parent_type` fields as it walks the chain.
    raw_types = {name: _normalize_field_keys(raw, f"agent_types.{name}") for name, raw in raw_types.items()}

    # A user-defined custom type shadows a plugin-registered alias of the same
    # name: the user's concrete type wins (mirroring how a registered type
    # beats an alias at plugin-load time), so drop the colliding alias before
    # resolving anything. Done as a pre-pass so a shadowed alias is never
    # consulted when normalizing a parent_type in the loop below.
    for name in raw_types:
        if is_agent_alias(name):
            shadowed_canonical = normalize_agent_type_name(name)
            unregister_agent_alias(name)
            if not silent:
                logger.warning(
                    "Custom agent type '{}' shadows the built-in alias for '{}'; '{}' now "
                    "refers to your custom type and no longer resolves to '{}'.",
                    name,
                    shadowed_canonical,
                    name,
                    shadowed_canonical,
                )

    for name, raw_config in raw_types.items():
        # Custom types with a parent_type should use the parent's config class,
        # since the parent type defines the valid fields (e.g., ClaudeAgentConfig
        # has auto_dismiss_dialogs). Without this, unregistered custom type names
        # fall back to the base AgentTypeConfig which rejects parent-specific fields.
        # A parent_type may itself be an alias (e.g. parent_type = "agy"), so
        # resolve it to the canonical type before looking up the config class.
        raw_parent_type = raw_config.get("parent_type")
        parent_type = normalize_agent_type_name(raw_parent_type) if raw_parent_type is not None else None
        # Walk the parent chain through raw_types to check if this type or
        # any ancestor depends on a disabled plugin.
        if _has_disabled_ancestor(name, raw_types, disabled_plugins):
            continue
        effective_type = parent_type if parent_type is not None else name
        config_class = get_agent_config_class(effective_type)
        # If no specific config class is registered for this type, the field
        # set we'll validate against is the bare base AgentTypeConfig -- which
        # will reject any plugin-specific fields (e.g. claude's `sync_home_settings`).
        # Mirror the hint shape used by _parse_providers so users learn whether
        # the cause is a missing plugin (or a typo) rather than thinking they
        # mistyped a field name. The "type name matches a disabled plugin"
        # case is already handled upstream by _has_disabled_ancestor (the
        # entire block is skipped), so it isn't surfaced here.
        if is_agent_config_registered(effective_type):
            extra_hint = None
        elif disabled_plugins:
            extra_hint = (
                f"If '{effective_type}' is provided by a disabled plugin, enable it. "
                f"Currently disabled plugins: {', '.join(sorted(disabled_plugins))}. "
                "Otherwise the plugin package that provides this agent type may not be "
                "installed, or one or more field names are misspelled."
            )
        else:
            extra_hint = (
                f"The plugin package that provides agent type '{effective_type}' may not be "
                "installed. Otherwise the agent type name or one of the field names may be "
                "misspelled."
            )
        cleaned_config = _drop_unknown_fields(
            raw_config,
            config_class,
            f"agent_types.{name}",
            strict=strict,
            silent=silent,
            extra_hint=extra_hint,
        )
        normalized_config = _normalize_tuple_fields_for_construct(cleaned_config)
        # Persist the alias-resolved parent_type so downstream resolution sees
        # the canonical type rather than the alias the user wrote.
        if parent_type is not None:
            normalized_config["parent_type"] = parent_type
        agent_types[AgentTypeName(name)] = config_class.model_construct(**normalized_config)

    return agent_types


def _parse_plugins(
    raw_plugins: dict[str, dict[str, Any]],
    *,
    strict: bool = True,
    silent: bool = False,
) -> dict[PluginName, PluginConfig]:
    """Parse plugin configs using the registry.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    plugins: dict[PluginName, PluginConfig] = {}

    for name, raw_config in raw_plugins.items():
        raw_config = _normalize_field_keys(raw_config, f"plugins.{name}")
        config_class = get_plugin_config_class(name)
        cleaned_config = _drop_unknown_fields(
            raw_config, config_class, f"plugins.{name}", strict=strict, silent=silent
        )
        plugins[PluginName(name)] = config_class.model_construct(**cleaned_config)

    return plugins


def _apply_plugin_overrides(
    plugins: dict[PluginName, PluginConfig],
    enabled_plugins: Sequence[str] | None,
    disabled_plugins: Sequence[str] | None,
) -> tuple[dict[PluginName, PluginConfig], frozenset[str]]:
    """Apply CLI plugin enable/disable overrides and filter out disabled plugins.

    Returns a tuple of (enabled_plugins_dict, disabled_plugin_names).
    """
    # Create a mutable copy
    result: dict[PluginName, PluginConfig] = dict(plugins)

    # Apply enabled plugins (add if not present, or set enabled=True)
    if enabled_plugins:
        for plugin_name_str in enabled_plugins:
            plugin_name = PluginName(plugin_name_str)
            if plugin_name in result:
                # Plugin exists - set enabled=True
                existing = result[plugin_name]
                result[plugin_name] = existing.model_copy_update(
                    to_update(existing.field_ref().enabled, True),
                )
            else:
                # Plugin doesn't exist - create with enabled=True
                config_class = get_plugin_config_class(plugin_name_str)
                result[plugin_name] = config_class(enabled=True)

    # Apply disabled plugins (set enabled=False)
    if disabled_plugins:
        for plugin_name_str in disabled_plugins:
            plugin_name = PluginName(plugin_name_str)
            if plugin_name in result:
                # Plugin exists - set enabled=False
                existing = result[plugin_name]
                result[plugin_name] = existing.model_copy_update(
                    to_update(existing.field_ref().enabled, False),
                )
            else:
                # Plugin doesn't exist - create with enabled=False
                config_class = get_plugin_config_class(plugin_name_str)
                result[plugin_name] = config_class(enabled=False)

    # Collect disabled plugin names and filter out disabled plugins
    disabled_names = frozenset(str(name) for name, config in result.items() if not config.enabled)
    enabled_result = {name: config for name, config in result.items() if config.enabled}
    return enabled_result, disabled_names


def block_disabled_plugins(pm: pluggy.PluginManager, disabled_names: frozenset[str], is_strict: bool = False) -> None:
    """Block disabled plugins in the plugin manager so their hooks don't fire.

    Uses pm.set_blocked() which both prevents future registration and
    unregisters already-registered plugins. Safe to call for names that
    are already blocked (no-op in that case).
    """
    for name in disabled_names:
        if is_strict:
            if not pm.has_plugin(name) and not pm.is_blocked(name):
                registered = [n for n, _ in pm.list_name_plugin()]
                raise UserInputError(
                    f"Cannot disable plugin '{name}' because it is not registered. Registered plugins: {registered}"
                )
        if not pm.is_blocked(name):
            pm.set_blocked(name)


def _parse_retry_config(raw_retry: dict[str, Any], *, strict: bool = True, silent: bool = False) -> RetryConfig:
    """Parse retry config.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    raw_retry = _normalize_field_keys(raw_retry, "retry")
    cleaned_retry = _drop_unknown_fields(raw_retry, RetryConfig, "retry", strict=strict, silent=silent)
    return RetryConfig.model_construct(**cleaned_retry)


def _parse_logging_config(raw_logging: dict[str, Any], *, strict: bool = True, silent: bool = False) -> LoggingConfig:
    """Parse logging config.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    raw_logging = _normalize_field_keys(raw_logging, "logging")
    cleaned_logging = _drop_unknown_fields(raw_logging, LoggingConfig, "logging", strict=strict, silent=silent)
    return LoggingConfig.model_construct(**cleaned_logging)


def _parse_tmux_config(raw_tmux: dict[str, Any], *, strict: bool = True, silent: bool = False) -> TmuxConfig:
    """Parse tmux config.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    ``attach_args`` is coerced to a tuple here (accepting either a single string or a
    list) because model_construct skips the field validator that would otherwise do so.
    A string value is wrapped in ``ScalarTuple`` (mirroring how ``cli_args`` is
    handled in ``_normalize_tuple_fields_for_construct``) so narrowing detection treats
    a higher-precedence string replacement as scalar replacement rather than aggregate
    narrowing.
    """
    raw_tmux = _normalize_field_keys(raw_tmux, "tmux")
    cleaned_tmux = _drop_unknown_fields(raw_tmux, TmuxConfig, "tmux", strict=strict, silent=silent)
    if "attach_args" in cleaned_tmux:
        attach_args = cleaned_tmux["attach_args"]
        if isinstance(attach_args, str):
            tokens = split_cli_args_string(attach_args) if attach_args else ()
            cleaned_tmux["attach_args"] = ScalarTuple(tokens)
        else:
            cleaned_tmux["attach_args"] = tuple(attach_args)
    # Coerce the path to Path here: model_construct bypasses field validation, so
    # without this the field would hold a bare str despite its Path | None type.
    if cleaned_tmux.get("additional_config_path") is not None:
        cleaned_tmux["additional_config_path"] = Path(cleaned_tmux["additional_config_path"])
    return TmuxConfig.model_construct(**cleaned_tmux)


def _parse_commands(raw_commands: dict[str, dict[str, Any]]) -> dict[str, CommandDefaults]:
    """Parse command defaults from config.

    Format: commands.{command_name}.{param_name} = value
    Example: [commands.create]
             new_host = "docker"
             connect = false

    The special key `default_subcommand` is extracted separately from the
    parameter defaults dict so it can be stored on CommandDefaults as a
    first-class field.

    Only fields actually present in ``raw_defaults`` end up in
    ``model_fields_set`` so the overlay config merge can distinguish
    "layer touched defaults" from "layer touched only default_subcommand".
    """
    commands: dict[str, CommandDefaults] = {}

    for command_name, raw_defaults in raw_commands.items():
        # Normalize hyphens to underscores so TOML-style `pass-env` matches `pass_env`.
        # _normalize_field_keys always returns a fresh dict, so the pop() below
        # cannot mutate the caller's input.
        defaults_copy = _normalize_field_keys(raw_defaults, f"commands.{command_name}")
        has_default_subcommand = "default_subcommand" in defaults_copy
        default_subcommand = defaults_copy.pop("default_subcommand", None)
        construct_kwargs: dict[str, Any] = {}
        if defaults_copy:
            construct_kwargs["defaults"] = defaults_copy
        if has_default_subcommand:
            construct_kwargs["default_subcommand"] = default_subcommand
        commands[command_name] = CommandDefaults.model_construct(**construct_kwargs)

    return commands


def _parse_create_templates(raw_templates: dict[str, dict[str, Any]]) -> dict[CreateTemplateName, CreateTemplate]:
    """Parse create templates from config.

    Format: create_templates.{template_name}.{param_name} = value
    Example: [create_templates.modal-dev]
             new_host = "modal"
             target_path = "/root/workspace"

    ``param_name__extend = [...]`` is also accepted: the same ``__extend``
    operator that works in TOML / ``--setting`` / env vars opts a single
    template option into additive behavior at template-application time.
    See ``apply_create_template`` for the application semantics.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    templates: dict[CreateTemplateName, CreateTemplate] = {}

    for template_name, raw_options in raw_templates.items():
        raw_options = _normalize_field_keys(raw_options, f"create_templates.{template_name}")
        # make sure the options don't define anything that cannot be handled
        # (an ``__extend`` suffix is a valid operator on any CLI option key, so
        # strip it before checking against the CreateCliOptions schema).
        for field in raw_options.keys():
            base_field = bare_key(field) if is_extend_key(field) else field
            if base_field not in CreateCliOptions.model_fields:
                raise ConfigParseError(
                    f"Unknown field '{field}' in create_templates.{template_name}. Valid fields: {sorted(CreateCliOptions.model_fields.keys())}"
                )
        # fine, add the template
        templates[CreateTemplateName(template_name)] = CreateTemplate.model_construct(options=raw_options)

    return templates


def parse_config(
    raw: dict[str, Any],
    disabled_plugins: frozenset[str],
    *,
    strict: bool = True,
    silent: bool = False,
) -> MngrConfig:
    """Parse a raw config dict into MngrConfig.

    Uses model_construct to bypass defaults and explicitly set None for unset fields.

    When strict=True (default), raises ConfigParseError for unknown fields.
    When strict=False, logs a warning and ignores unknown fields (used when
    MNGR_ALLOW_UNKNOWN_CONFIG is set to allow forward-compatible config files).
    When silent=True (and strict=False), suppresses the warning entirely. Used by
    ``mngr plugin add``, where the config is expected to reference plugins that
    are not yet installed.
    """
    raw = _normalize_field_keys(raw, "top-level config")
    # Build kwargs with None for unset scalar fields
    kwargs: dict[str, Any] = {}
    kwargs["prefix"] = raw.pop("prefix", None)
    kwargs["default_host_dir"] = raw.pop("default_host_dir", None)
    kwargs["unset_vars"] = raw.pop("unset_vars", None)
    kwargs["pager"] = raw.pop("pager", None)
    kwargs["enabled_backends"] = raw.pop("enabled_backends", None)
    kwargs["connect_command"] = raw.pop("connect_command", None)
    kwargs["is_remote_agent_installation_allowed"] = raw.pop("is_remote_agent_installation_allowed", None)
    kwargs["agent_types"] = (
        _parse_agent_types(raw.pop("agent_types", {}), disabled_plugins=disabled_plugins, strict=strict, silent=silent)
        if "agent_types" in raw
        else {}
    )
    kwargs["providers"] = (
        _parse_providers(raw.pop("providers", {}), disabled_plugins=disabled_plugins, strict=strict, silent=silent)
        if "providers" in raw
        else {}
    )
    kwargs["plugins"] = (
        _parse_plugins(raw.pop("plugins", {}), strict=strict, silent=silent) if "plugins" in raw else {}
    )
    kwargs["commands"] = _parse_commands(raw.pop("commands", {})) if "commands" in raw else {}
    kwargs["create_templates"] = (
        _parse_create_templates(raw.pop("create_templates", {})) if "create_templates" in raw else {}
    )
    kwargs["retry"] = (
        _parse_retry_config(raw.pop("retry", {}), strict=strict, silent=silent) if "retry" in raw else None
    )
    kwargs["logging"] = (
        _parse_logging_config(raw.pop("logging", {}), strict=strict, silent=silent) if "logging" in raw else None
    )
    kwargs["tmux"] = _parse_tmux_config(raw.pop("tmux", {}), strict=strict, silent=silent) if "tmux" in raw else None
    kwargs["is_nested_tmux_allowed"] = raw.pop("is_nested_tmux_allowed", None)
    kwargs["headless"] = raw.pop("headless", None)
    kwargs["is_error_reporting_enabled"] = raw.pop("is_error_reporting_enabled", None)
    kwargs["is_allowed_in_pytest"] = raw.pop("is_allowed_in_pytest", None)
    kwargs["pre_command_scripts"] = raw.pop("pre_command_scripts", None)
    kwargs["work_dir_extra_paths"] = raw.pop("work_dir_extra_paths", None)
    kwargs["default_destroyed_host_persisted_seconds"] = raw.pop("default_destroyed_host_persisted_seconds", None)
    kwargs["default_min_online_host_age_seconds"] = raw.pop("default_min_online_host_age_seconds", None)
    kwargs["agent_ready_timeout"] = raw.pop("agent_ready_timeout", None)
    kwargs["allow_settings_key_assignment_narrowing"] = raw.pop("allow_settings_key_assignment_narrowing", None)

    if len(raw) > 0:
        if strict:
            raise ConfigParseError(f"Unknown configuration fields: {list(raw.keys())}")
        if not silent:
            logger.warning("Unknown configuration fields: {}", list(raw.keys()))

    # Use model_construct to bypass field defaults
    return MngrConfig.model_construct(**kwargs)


# =============================================================================
# Environment Variable Overrides
# =============================================================================


def _env_segments_to_key_path(segments: list[str]) -> list[str]:
    """Convert lowercased env-var segments into raw-dict key path segments.

    A trailing ``extend`` segment is collapsed into a ``key__extend`` suffix on
    the preceding segment so that ``resolve_extends`` recognises the operator.
    """
    if len(segments) >= 2 and segments[-1] == "extend":
        return segments[:-2] + [segments[-2] + EXTEND_SUFFIX]
    return list(segments)


def _parse_mngr_env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Parse ``MNGR__X__Y[__EXTEND]=value`` env vars into a raw config dict.

    Segments are uppercase-only ([A-Z0-9_]+). Each segment is lowercased to
    produce the canonical config key. Values are JSON-parsed with raw-string
    fallback. The returned dict may contain ``__extend``-suffixed keys; the
    shared resolver applies them against the base config.
    """
    raw: dict[str, Any] = {}
    for env_key, env_value in environ.items():
        if not env_key.startswith(_ENV_OVERRIDE_PREFIX):
            continue
        # Skip mixed-case variants of the canonical form. The pattern enforces
        # the documented uppercase-only convention; anything else is treated as
        # unrelated.
        if not _ENV_OVERRIDE_PATTERN.fullmatch(env_key):
            continue
        suffix = env_key[len(_ENV_OVERRIDE_PREFIX) :]
        segments = [seg.lower() for seg in suffix.split("__")]
        # The pattern's ``[A-Z0-9_]+`` permits embedded underscores, which means
        # malformed shapes like ``MNGR__X__`` or ``MNGR____X`` slip through the
        # regex and produce empty segments after ``split("__")``. Skip those so
        # a stray trailing ``__`` doesn't silently materialize an unnamed key.
        if any(not seg for seg in segments):
            continue
        key_path = _env_segments_to_key_path(segments)
        set_at_path(raw, key_path, parse_scalar_value(env_value))
    return raw


def _collect_env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Combine ``MNGR__*`` overrides with preserved-alias env vars into a single
    raw config dict.

    Each preserved alias uses its historic value parser (see
    ``_PRESERVED_ALIASES``) so backwards-compatible spellings like
    ``MNGR_HEADLESS=yes`` keep their old meaning. Raises ``ConfigParseError``
    when a preserved alias and its canonical ``MNGR__*`` form are both set
    with different parsed values.
    """
    raw = _parse_mngr_env_overrides(environ)
    for alias_name, (canonical_path, value_parser) in _PRESERVED_ALIASES.items():
        alias_value = environ.get(alias_name)
        if alias_value is None:
            continue
        parsed = value_parser(alias_value)
        existing = _walk_raw(raw, canonical_path.split("."))
        # Compare under the alias's value semantics. The alias and the
        # canonical form may use different parsers (e.g. MNGR_HEADLESS uses
        # parse_bool_env while MNGR__HEADLESS uses JSON-with-string-fallback),
        # so a string canonical value gets re-parsed by the alias parser
        # before the equality check. This stops false-positive conflicts
        # when both forms carry the same intent (e.g. both set to "yes").
        if existing is not None:
            normalized_existing = value_parser(existing) if isinstance(existing, str) else existing
            if normalized_existing != parsed:
                raise ConfigParseError(
                    f"Conflict: {alias_name}={alias_value!r} and "
                    f"MNGR__{canonical_path.upper()}={existing!r} are both set with different values. "
                    "Use exactly one form."
                )
        set_at_path(raw, canonical_path.split("."), parsed)
    return raw


def _walk_raw(data: dict[str, Any], key_path: list[str]) -> Any:
    """Look up a dotted path inside a raw dict; return None if any step is
    missing or the intermediate value is not a dict.
    """
    current: Any = data
    for segment in key_path:
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _parse_config_with_extends(
    raw: dict[str, Any],
    *,
    base_config: MngrConfig,
    disabled_plugins: frozenset[str],
    strict: bool = True,
    silent: bool = False,
) -> MngrConfig:
    """Resolve ``__extend`` keys in ``raw`` against ``base_config`` and parse
    the resolved dict via ``parse_config``.
    """
    resolved = resolve_extends(base_config, raw)
    return parse_config(resolved, disabled_plugins=disabled_plugins, strict=strict, silent=silent)
