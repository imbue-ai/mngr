from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from enum import auto
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Final
from typing import Self
from typing import TypeVar
from uuid import uuid4

import pluggy
from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import field_validator
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import ParseSpecError
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import LifecycleHook
from imbue.mngr.primitives import NewAgentLocation
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.logging import LoggingConfig

USER_ID_FILENAME: Final[str] = "user_id"

# 7 days in seconds -- controls how long destroyed host records (and their
# snapshots) are kept before permanent deletion, giving users a recovery
# window via `mngr create --snapshot`.
_DEFAULT_DESTROYED_HOST_PERSISTED_SECONDS: Final[float] = 60.0 * 60.0 * 24.0 * 7.0

# 10 minutes -- minimum age before GC will destroy an online host with no agents.
# Short because we only need to protect against transient empty states (e.g. between
# agent creation and discovery).
_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS: Final[float] = 60.0 * 10.0

# === Helper Functions ===

PluginConfigT = TypeVar("PluginConfigT", bound="PluginConfig")


class ScalarTuple(tuple):
    """Marker tuple subclass for a tuple-typed field whose value is semantically a
    single scalar: replacing it from a higher-precedence settings layer is scalar
    replacement, not aggregate narrowing.

    The settings-narrowing guard (``_check_narrowing`` / ``would_assignment_narrow``)
    normally flags a higher-precedence layer that drops entries from a non-empty
    list/tuple set by a lower layer. For some fields that additive intent never
    applies -- the value is a coherent whole a higher layer means to *replace*.
    Marking such a field's parsed value ``ScalarTuple`` exempts it from that check.

    Two cases produce the marker: a tuple field written as a single string in TOML
    (``StringDerivedTuple``, a subclass), and a field declared with ``ScalarStrTuple``
    that is always replace-by-default regardless of how it is written (e.g. an AWS
    provider's ``allowed_ssh_cidrs`` -- combining CIDRs across config layers is never
    the intent).

    The marker is applied by ``model_validate`` (via the ``ScalarStrTuple``
    after-validator) or by the loader's string-splitting path. It survives
    ``model_construct`` (which bypasses validation) but is intentionally not
    preserved through ``model_dump`` / merges. That is enough because narrowing
    detection always compares a freshly-parsed layer (which retains the marker)
    against the already-merged base.
    """


class StringDerivedTuple(ScalarTuple):
    """Marker for a tuple value originally provided as a single string in user
    settings.

    Some tuple-typed fields (most notably ``cli_args``) accept either a list/tuple
    or a single string in TOML. When the user writes a string, the natural unit is
    the whole string -- so a higher-precedence layer that replaces one string with
    another is scalar replacement, not aggregate narrowing. A specialization of
    ``ScalarTuple`` (it inherits the narrowing exemption); this subclass exists so
    the loader can mark *only* the string-shaped writes of fields that otherwise
    merge additively.
    """


def _coerce_to_scalar_tuple(value: tuple[str, ...]) -> ScalarTuple:
    """After-validator for ``ScalarStrTuple``: wrap the validated string tuple in
    ``ScalarTuple`` so the settings-narrowing guard treats the field as
    replace-by-default."""
    return ScalarTuple(value)


# A ``tuple[str, ...]`` config field that is semantically a single scalar value: a
# higher-precedence settings layer that sets it replaces the whole value rather than
# narrowing it. Use for fields where combining entries across config layers is never
# the intent (e.g. an AWS provider's ``allowed_ssh_cidrs``). The narrowing exemption
# only takes effect under ``model_validate`` (which runs the after-validator);
# ``_parse_providers`` validates provider blocks, so provider-config fields qualify.
ScalarStrTuple = Annotated[tuple[str, ...], AfterValidator(_coerce_to_scalar_tuple)]


@pure
def split_cli_args_string(cli_args: str) -> tuple[str, ...]:
    """Split a CLI args string into individual argument tokens, preserving quoting.

    Uses shlex in non-POSIX mode so that quote characters (both single and double)
    are kept as part of the resulting tokens. This ensures that when the arguments
    are later joined with spaces (e.g. in assemble_command), the quoting is
    maintained and the resulting shell command is correct.

    Example:
        >>> split_cli_args_string("--settings '{\"key\": \"value\"}'")
        ('--settings', '\\'{"key": "value"}\\'')
    """
    lexer = shlex.shlex(cli_args, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return tuple(lexer)


def _assign_scalar(base_value: Any, override_value: Any) -> Any:
    """Return ``override_value`` when it is not None; otherwise fall back to ``base_value``.

    Used inside ``MngrConfig.merge_with`` to express the "override wins if
    explicitly set" rule for scalar fields without duplicating the conditional
    at every call site.
    """
    return override_value if override_value is not None else base_value


def _merge_container_dict(
    base: dict[Any, Any],
    override: dict[Any, Any],
) -> dict[Any, Any]:
    """Per-key additive merge for the top-level container dicts on ``MngrConfig``.

    Keys present in only one side are preserved. Keys present in both are
    merged via the value's ``merge_with`` method. This is the explicit
    carveout from the otherwise assign-by-default rule.
    """
    merged: dict[Any, Any] = {}
    for key in set(base.keys()) | set(override.keys()):
        if key in base and key in override:
            merged[key] = base[key].merge_with(override[key])
        elif key in override:
            merged[key] = override[key]
        else:
            merged[key] = base[key]
    return merged


# The top-level container dicts on ``MngrConfig`` whose merge is per-key
# (instead of assign-by-default). Listed here in one place so narrowing
# detection can recurse into each entry rather than treating the dict as a
# single aggregate to flag.
_CONTAINER_DICT_FIELDS: Final[frozenset[str]] = frozenset(
    {"agent_types", "providers", "plugins", "commands", "create_templates"}
)


def would_assignment_narrow(base_value: Any, override_value: Any) -> bool:
    """Return True if assigning ``override_value`` over ``base_value`` would
    drop at least one base entry (a missing list/set element, a missing dict
    key, or an explicit empty aggregate over a non-empty base).

    Mirrors the leaf-level rule used by ``detect_settings_narrowing`` so the
    settings-layer merge guard and the template-application guard agree on what
    counts as narrowing. Same exemptions: no-ops (override equals base) and
    supersets (every base entry survives, e.g. the materialised result of an
    ``__extend`` operation) return ``False``. Against a list/tuple base, a
    ``ScalarTuple`` override is also exempt -- it represents scalar replacement
    of the whole value (a string-shaped TOML value, or a field declared
    replace-by-default like ``allowed_ssh_cidrs``), not aggregate narrowing.
    Scalars and empty/non-aggregate bases never narrow.
    """
    if not isinstance(base_value, (list, tuple, dict, set, frozenset)) or not base_value:
        return False
    if isinstance(base_value, (list, tuple)):
        # Mirror the ScalarTuple exemption in ``_check_narrowing`` so the
        # template-application guard agrees with the settings-layer guard.
        if isinstance(override_value, ScalarTuple):
            return False
        if isinstance(override_value, (list, tuple)) and all(entry in override_value for entry in base_value):
            return False
        return True
    if isinstance(base_value, (set, frozenset)):
        if isinstance(override_value, (set, frozenset, list, tuple)) and set(base_value) <= set(override_value):
            return False
        return True
    # base_value is a non-empty dict
    if not isinstance(override_value, dict):
        return True
    if any(key not in override_value for key in base_value):
        return True
    return any(would_assignment_narrow(sub_base, override_value[key]) for key, sub_base in base_value.items())


def detect_settings_narrowing(base: Any, override: Any) -> list[str]:
    """Return dotted paths where ``override`` would silently drop entries from
    a non-empty aggregate value in ``base`` (``list``, ``tuple``, ``dict``,
    ``set``, ``frozenset``).

    Used by the loader to surface accidental data loss when the new
    assign-by-default merge semantics replace a previous additive merge. The
    check is recursive: container dicts (``agent_types``, ``providers``,
    ``plugins``, ``commands``, ``create_templates``) traverse per key so only
    the actually-narrowing sub-fields are flagged. Sub-models recurse by
    their own ``model_fields_set`` so untouched fields are ignored.

    "Narrowing" is defined as the override losing at least one base entry --
    a missing list/set element, a missing dict key, or an explicit empty
    aggregate over a non-empty base. No-ops (override equals base) and
    supersets (every base entry survives, e.g. an ``__extend`` result) pass
    without flagging. Against a list/tuple base, a ``ScalarTuple`` override is
    also exempt: a string-shaped TOML value (e.g. ``cli_args = "..."``) or a
    field declared replace-by-default (e.g. ``allowed_ssh_cidrs``) is a coherent
    single value, so a higher-precedence layer that replaces it expresses scalar
    replacement rather than aggregate narrowing. Value mutations at a shared dict
    key recurse instead of flagging at the parent.

    Layers that didn't write the field (override value is ``None``, since
    ``parse_config`` defaults missing fields to ``None``) are skipped by
    ``_walk_for_narrowing`` so an unrelated layer's omission never counts
    as a narrowing assignment.

    Returns dotted paths like ``commands.create.defaults.env``. The list is
    empty when there are no narrowing assignments.
    """
    violations: list[str] = []
    _walk_for_narrowing(base, override, path=(), violations=violations)
    return violations


def _walk_for_narrowing(
    base: Any,
    override: Any,
    path: tuple[str, ...],
    violations: list[str],
) -> None:
    if isinstance(override, BaseModel):
        explicitly_set = override.model_fields_set
        for field_name in override.__class__.model_fields:
            if field_name not in explicitly_set:
                continue
            override_value = getattr(override, field_name)
            # ``None`` mirrors the ``if override.<field> is not None`` test used
            # throughout MngrConfig.merge_with: parse_config sets every kwarg
            # (often to ``None``) so model_fields_set alone over-reports which
            # fields the layer actually touched. A ``None`` value means the
            # layer did not write the field, so it cannot narrow anything.
            if override_value is None:
                continue
            base_value = getattr(base, field_name, None) if isinstance(base, BaseModel) else None
            sub_path = path + (field_name,)
            if field_name in _CONTAINER_DICT_FIELDS:
                # Per-key recurse for container dicts (agent_types, etc.)
                _walk_for_narrowing(base_value, override_value, sub_path, violations)
                continue
            _check_narrowing(base_value, override_value, sub_path, violations)
        return
    if isinstance(override, Mapping):
        # Container dict (e.g. ``commands``) -- recurse per key against
        # whatever the base has at that key. Keys present only in override
        # are pure additions and never narrow.
        for key, sub_override in override.items():
            sub_base = base.get(key) if isinstance(base, Mapping) else None
            if sub_base is None:
                continue
            _walk_for_narrowing(sub_base, sub_override, path + (str(key),), violations)


def _check_narrowing(
    base_value: Any,
    override_value: Any,
    path: tuple[str, ...],
    violations: list[str],
) -> None:
    """Check a single field for narrowing, recursing into nested aggregates.

    Sub-models recurse into their explicitly-set fields; aggregates check
    whether every base entry survives in the override. For dicts the check
    is two-pass: missing keys are flagged at this level (the dict has been
    truncated), while shared keys recurse so a value mutation inside a
    nested aggregate surfaces at the deeper path rather than at the parent.

    Clearing a non-empty aggregate (``env = []`` over ``env = ["X=5"]``) is
    treated as narrowing too: it drops every prior entry, which is the most
    extreme form of data loss the safety net is meant to catch. To clear,
    the user must set ``allow_settings_key_assignment_narrowing = true``.
    Against a non-empty list/tuple base, three forms of override pass without
    warning: no-ops (override equals base), supersets (every base entry
    survives, e.g. ``__extend`` results or additive assigns that happen to
    include every prior value), and ``ScalarTuple`` overrides (a string-shaped
    TOML value such as ``cli_args = "..."``, or a replace-by-default field such
    as ``allowed_ssh_cidrs``, is a coherent single value, so replacing it is
    scalar replacement rather than aggregate narrowing).
    """
    if isinstance(base_value, BaseModel) and isinstance(override_value, BaseModel):
        _walk_for_narrowing(base_value, override_value, path, violations)
        return
    if not isinstance(base_value, (list, tuple, dict, set, frozenset)) or not base_value:
        return
    if isinstance(base_value, (list, tuple)):
        # Scalar replacement intent: the override is either a string-derived value
        # (e.g. ``cli_args = "..."`` in TOML) or a replace-by-default field (e.g.
        # ``allowed_ssh_cidrs``); either way the whole value is replaced as a
        # coherent unit, not narrowed as a list.
        if isinstance(override_value, ScalarTuple):
            return
        if isinstance(override_value, (list, tuple)) and all(entry in override_value for entry in base_value):
            return
        violations.append(".".join(path))
        return
    if isinstance(base_value, (set, frozenset)):
        if isinstance(override_value, (set, frozenset, list, tuple)) and set(base_value) <= set(override_value):
            return
        violations.append(".".join(path))
        return
    # base_value is a non-empty dict
    if not isinstance(override_value, dict):
        violations.append(".".join(path))
        return
    if any(key not in override_value for key in base_value):
        violations.append(".".join(path))
        return
    for key, sub_base in base_value.items():
        _check_narrowing(sub_base, override_value[key], path + (str(key),), violations)


# === Enums ===


class WorkDirExtraPathMode(UpperCaseStrEnum):
    """Transfer mode for extra paths in new work directories."""

    SHARE = auto()
    COPY = auto()


class ConfigScope(UpperCaseStrEnum):
    """A settings-file layer: the user profile, the project, or the local override.

    The lowercased member name is the value accepted by ``mngr config set
    --scope`` and surfaced in diagnostics; ``get_config_path`` (cli/config.py)
    and ``read_config_layers`` (pre_readers.py) both map these to the same files.
    """

    USER = auto()
    PROJECT = auto()
    LOCAL = auto()


# === Value Types ===


class EnvVar(FrozenModel):
    """Environment variable as KEY=VALUE."""

    key: str = Field(description="The environment variable name")
    value: str = Field(description="The environment variable value")

    @classmethod
    def from_string(cls, s: str) -> "EnvVar":
        """Parse a KEY=VALUE string into an EnvVar."""
        if "=" not in s:
            raise ParseSpecError(f"Environment variable must be in KEY=VALUE format, got: {s}")
        key, value = s.split("=", 1)
        return cls(key=key.strip(), value=value.strip())


class HookDefinition(FrozenModel):
    """Lifecycle hook definition as NAME:COMMAND."""

    hook: LifecycleHook = Field(description="The lifecycle hook name")
    command: str = Field(description="The command to run")

    @classmethod
    def from_string(cls, s: str) -> "HookDefinition":
        """Parse a NAME:COMMAND string into a HookDefinition."""
        if ":" not in s:
            raise ParseSpecError(f"Hook must be in NAME:COMMAND format, got: {s}")
        name, command = s.split(":", 1)
        # Normalize name: convert hyphens to underscores and uppercase
        normalized_name = name.strip().upper().replace("-", "_")
        try:
            hook = LifecycleHook(normalized_name)
        except ValueError:
            valid = ", ".join(h.value.lower().replace("_", "-") for h in LifecycleHook)
            raise ParseSpecError(f"Invalid hook name '{name}'. Valid hooks: {valid}") from None
        return cls(hook=hook, command=command.strip())


# === Config Types ===


class AgentTypeConfig(FrozenModel):
    """Defines a custom agent type that inherits from an existing type."""

    parent_type: AgentTypeName | None = Field(
        default=None,
        description="Base type to inherit from (must be a plugin-provided or command type, not another custom type)",
    )
    plugin: str | None = Field(
        default=None,
        description="Plugin that provides this agent type. Defaults to parent_type (if set) or the type name. "
        "Used to skip parsing when the plugin is disabled.",
    )
    command: CommandString | None = Field(
        default=None,
        description="Command to run for this agent type",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the agent",
    )
    extra_provision_command: tuple[str, ...] = Field(
        default=(),
        description="Shell commands to run during provisioning",
    )
    upload_file: tuple[str, ...] = Field(
        default=(),
        description="LOCAL:REMOTE file upload specs",
    )
    create_directory: tuple[str, ...] = Field(
        default=(),
        description="Directories to create on the remote",
    )
    env: tuple[str, ...] = Field(
        default=(),
        description="KEY=VALUE environment variables",
    )
    env_file: tuple[str, ...] = Field(
        default=(),
        description="Paths to env files",
    )

    @field_validator("cli_args", mode="before")
    @classmethod
    def _normalize_cli_args(cls, value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, str):
            return split_cli_args_string(value) if value else ()
        return tuple(value)

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Uses model_fields_set to determine which fields were explicitly set in
        the override config, so that subclass-specific fields (e.g., ClaudeAgentConfig's
        auto_dismiss_dialogs) are correctly preserved during merges.

        All aggregate fields flip to assign-by-default: a list/tuple/dict/set
        value in the override replaces the base value entirely. Use the
        ``field__extend`` operator in TOML / ``--setting`` / env vars to get
        additive behavior.
        """
        # Allow override to be the same class or a base class of self (e.g., when
        # a secondary config file defines the same custom type without repeating
        # parent_type, it gets parsed as the base AgentTypeConfig). Reject
        # sibling subclasses (e.g., ClaudeAgentConfig vs CodexAgentConfig).
        if not isinstance(self, type(override)):
            raise ConfigParseError(f"Cannot merge {self.__class__.__name__} with {type(override).__name__}")

        explicitly_set = override.model_fields_set
        if not explicitly_set:
            return self

        override_values = override.model_dump()
        updates: list[tuple[str, Any]] = [(field_name, override_values[field_name]) for field_name in explicitly_set]
        return self.model_copy_update(*updates)


class ProviderInstanceConfig(FrozenModel):
    """Defines a custom provider instance."""

    backend: ProviderBackendName = Field(
        description="Provider backend to use (e.g., 'docker', 'modal', 'aws')",
    )
    plugin: str | None = Field(
        default=None,
        description="Plugin that provides this backend. Defaults to the backend name. "
        "Used to skip parsing when the plugin is disabled.",
    )
    is_enabled: bool | None = Field(
        default=None,
        description="Whether this provider instance is enabled. Set to false to disable without removing configuration.",
    )
    destroyed_host_persisted_seconds: float | None = Field(
        default=None,
        description="How long (in seconds) a destroyed host's records are kept before permanent deletion. "
        "Overrides the global default_destroyed_host_persisted_seconds when set.",
    )
    min_online_host_age_seconds: float | None = Field(
        default=None,
        description="Minimum age (in seconds) before GC will destroy an online host with no agents. "
        "Overrides the global default_min_online_host_age_seconds when set.",
    )

    def merge_with(self, override: "ProviderInstanceConfig") -> "ProviderInstanceConfig":
        """Merge this config with an override config.

        Uses ``model_fields_set`` so an override only replaces the fields it
        actually set; fields the override left untouched keep the base value.
        This matches ``AgentTypeConfig`` / ``PluginConfig`` and is what keeps a
        higher-precedence layer that touches a single field (e.g. a create
        template's ``--setting providers.<name>.is_enabled=true``) from
        silently resetting every other provider field -- like
        ``is_run_as_root`` -- back to its model default. Relying on
        "override wins unless its value is None" was wrong here: a field whose
        default is a non-None value (a ``bool`` default of ``False``, an empty
        tuple, ...) would clobber the base even when the override never set it.

        Aggregate fields still flip to assign-by-default: a list / dict / set
        the override explicitly sets replaces the base value rather than
        appending. Use the ``field__extend`` operator for additive behavior.
        """
        if not isinstance(override, self.__class__):
            raise ConfigParseError(f"Cannot merge {self.__class__.__name__} with different provider config type")

        explicitly_set = override.model_fields_set
        if not explicitly_set:
            return self
        base_values = self.model_dump()
        override_values = override.model_dump()
        merged_values: dict[str, Any] = dict(base_values)
        for field_name in explicitly_set:
            merged_values[field_name] = override_values[field_name]
        return self.__class__(**merged_values)


class PluginConfig(FrozenModel):
    """Base configuration for a plugin."""

    enabled: bool = Field(
        default=True,
        description="Whether this plugin is enabled",
    )

    def merge_with(self, override: "PluginConfig") -> "PluginConfig":
        """Merge this config with an override config.

        Uses ``model_fields_set`` so plugin subclasses that add extra fields
        get correct assign-by-default semantics on those fields too.
        """
        explicitly_set = override.model_fields_set
        if not explicitly_set:
            return self
        override_values = override.model_dump()
        updates: list[tuple[str, Any]] = [(field_name, override_values[field_name]) for field_name in explicitly_set]
        return self.model_copy_update(*updates)


class CommandDefaults(FrozenModel):
    """Default values for CLI command parameters.

    This allows config files to override default values for CLI arguments.
    Only parameters that were not explicitly set by the user will use these defaults.
    Field names should match the CLI parameter names (after click's conversion).
    """

    # Store as a flexible dict since we don't know all possible CLI parameters ahead of time
    defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of parameter name to default value",
    )
    default_subcommand: str | None = Field(
        default=None,
        description="Default subcommand when this group is invoked with no recognized command. "
        "Empty string disables defaulting (shows help instead).",
    )

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Uses ``model_fields_set`` so a layer that touches only
        ``default_subcommand`` (without writing any per-param defaults) leaves
        the base's ``defaults`` intact. When the override does touch
        ``defaults``, assign-by-default applies — the whole map replaces. Use
        ``defaults__extend = { ... }`` to opt into key-merge.
        """
        explicitly_set = override.model_fields_set
        if not explicitly_set:
            return self
        merged_defaults = override.defaults if "defaults" in explicitly_set else self.defaults
        merged_default_subcommand = (
            override.default_subcommand if "default_subcommand" in explicitly_set else self.default_subcommand
        )
        return self.__class__(defaults=merged_defaults, default_subcommand=merged_default_subcommand)


class CreateTemplateName(str):
    """Name of a create template."""

    def __new__(cls, value: str) -> Self:
        if not value:
            raise ParseSpecError("Template name cannot be empty")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )


class CreateTemplate(FrozenModel):
    """Template for the create command.

    Templates are named presets of create command arguments that can be applied
    using --template <name>. All fields are optional; only specified fields
    will override the defaults when the template is applied.

    Templates are useful for setting up common configurations for different
    providers or environments (e.g., different paths in remote containers vs locally).
    """

    # Store as a flexible dict since templates can contain any create command parameter
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of parameter name to value for create command options",
    )

    def merge_with(self, override: Self) -> Self:
        """Merge this template with an override template.

        Uses ``model_fields_set`` so a layer that doesn't touch ``options``
        leaves the base intact. When the override does touch ``options``,
        assign-by-default applies — the whole map replaces. Use
        ``options__extend`` to opt into key-merge.
        """
        explicitly_set = override.model_fields_set
        if not explicitly_set:
            return self
        merged_options = override.options if "options" in explicitly_set else self.options
        return self.__class__(options=merged_options)


class RetryConfig(FrozenModel):
    """Configuration for connection retry behavior.

    Controls how many times and how frequently mngr retries SSH connections
    to remote agents when connecting (via both ``mngr create --connect`` and
    ``mngr connect``).
    """

    connect_retry_times: int = Field(
        default=3,
        description="Number of times to retry a failed SSH connection before giving up",
    )
    connect_retry_delay: str = Field(
        default="5s",
        description="Delay between connection retries (e.g., '5s', '1m')",
    )

    def merge_with(self, override: "RetryConfig") -> "RetryConfig":
        """Merge this config with an override config.

        Important note: despite the type signatures, any of these fields may be None in the override--this means that they were NOT set in the toml (and thus should be ignored)

        Scalar fields: override wins if not None
        """
        return RetryConfig(
            connect_retry_times=override.connect_retry_times
            if override.connect_retry_times is not None
            else self.connect_retry_times,
            connect_retry_delay=override.connect_retry_delay
            if override.connect_retry_delay is not None
            else self.connect_retry_delay,
        )


class MngrConfig(FrozenModel):
    """Root configuration model for mngr."""

    prefix: str = Field(
        default="mngr-",
        description="Prefix for naming resources (tmux sessions, containers, etc.)",
    )
    default_host_dir: Path = Field(
        default=Path("~/.mngr"),
        description="Default base directory for mngr data on hosts (can be overridden per provider instance)",
    )
    unset_vars: list[str] = Field(
        # these are necessary to prevent tmux from accidentally sticking test data in history files
        default_factory=lambda: list(("HISTFILE", "PROFILE", "VIRTUAL_ENV")),
        description="Environment variables to unset when creating agent tmux sessions",
    )
    work_dir_extra_paths: dict[str, WorkDirExtraPathMode] = Field(
        default_factory=dict,
        description="Paths to transfer into new work directories, mapped to transfer mode. "
        "'SHARE': symlink on same host, copy on different host. "
        "'COPY': always copy via rsync.",
    )
    pager: str | None = Field(
        default=None,
        description="Pager command for help output (e.g., 'less'). If None, uses PAGER env var or 'less' as fallback.",
    )
    enabled_backends: list[ProviderBackendName] = Field(
        default_factory=list,
        description="List of enabled provider backends. If empty, all backends are enabled. If non-empty, only the listed backends are enabled.",
    )
    agent_types: dict[AgentTypeName, AgentTypeConfig] = Field(
        default_factory=dict,
        description="Custom agent type definitions",
    )
    providers: dict[ProviderInstanceName, ProviderInstanceConfig] = Field(
        default_factory=dict,
        description="Custom provider instance definitions",
    )
    plugins: dict[PluginName, PluginConfig] = Field(
        default_factory=dict,
        description="Plugin configurations",
    )
    disabled_plugins: frozenset[str] = Field(
        default_factory=frozenset,
        description="Set of plugin names that were explicitly disabled (used to filter backends)",
    )
    commands: dict[str, CommandDefaults] = Field(
        default_factory=dict,
        description="Default values for CLI command parameters (e.g., 'commands.create')",
    )
    create_templates: dict[CreateTemplateName, CreateTemplate] = Field(
        default_factory=dict,
        description="Named templates for the create command (e.g., 'create_templates.modal-dev')",
    )
    pre_command_scripts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Commands to run before CLI commands execute, keyed by command name (e.g., 'create': ['echo hello', 'validate.sh'])",
    )
    retry: RetryConfig = Field(
        default_factory=RetryConfig,
        description="Connection retry configuration",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration",
    )
    is_remote_agent_installation_allowed: bool = Field(
        default=True,
        description="Whether to allow automatic installation of agents (e.g. Claude) on remote hosts. "
        "When False, raises an error if the agent is not already installed on the remote host. "
        "Defaults to True (allowed).",
    )
    connect_command: str | None = Field(
        default=None,
        description="Custom command to run instead of the builtin connect when create, start, or connect connects to agents. "
        "The environment variables MNGR_AGENT_NAME and MNGR_SESSION_NAME are set before running the command.",
    )
    is_nested_tmux_allowed: bool = Field(
        default=False,
        description="Allow attaching to tmux sessions from within an existing tmux session by unsetting $TMUX",
    )
    headless: bool = Field(
        default=False,
        description="When true, disables all interactive behavior (prompts, TUI, editor). "
        "Equivalent to passing --headless on the CLI. Can also be set via MNGR_HEADLESS env var.",
    )
    is_error_reporting_enabled: bool = Field(
        default=True,
        description="Whether to suggest launching a diagnostic agent "
        "when an unexpected error occurs while running interactively",
    )
    is_allowed_in_pytest: bool = Field(
        default=False,
        description=(
            "Whether this config may be loaded during a pytest run. Defaults to False so a "
            "poorly-scoped test cannot pick up a real config (e.g. ~/.mngr) and perform real "
            "operations; configs written for tests set this to True to opt in."
        ),
    )
    default_destroyed_host_persisted_seconds: float = Field(
        default=_DEFAULT_DESTROYED_HOST_PERSISTED_SECONDS,
        description="Default number of seconds a destroyed host's records are kept before permanent deletion. "
        "Can be overridden per provider via destroyed_host_persisted_seconds in the provider config.",
    )
    default_min_online_host_age_seconds: float = Field(
        default=_DEFAULT_MIN_ONLINE_HOST_AGE_SECONDS,
        description="Default minimum age (in seconds) before GC will destroy an online host with no agents. "
        "Can be overridden per provider via min_online_host_age_seconds in the provider config.",
    )
    agent_ready_timeout: float = Field(
        default=10.0,
        description="Max seconds to wait for an agent to signal readiness before sending messages. "
        "Hook-based polling returns early; this is an upper bound, not an unconditional delay.",
    )
    allow_settings_key_assignment_narrowing: bool = Field(
        default=False,
        description=(
            "When False (the default), it is an error for a higher-precedence settings layer "
            "(project local, env vars, --setting, etc.) to assign over a non-empty list/tuple/"
            "dict/set value coming from a lower-precedence layer. This guards against silently "
            "losing entries when a settings file is loaded with the new assign-by-default merge "
            "behavior; the user is told to either use the __extend suffix to opt into the prior "
            "additive behavior or to set this field to True. The default for this field is "
            "expected to change to True in a future version, and support for False may be "
            "removed entirely once the migration is complete."
        ),
    )

    def merge_with(self, override: Self) -> Self:
        """Merge this config with an override config.

        Assign-by-default for every aggregate field (list, tuple, dict,
        frozenset). The override's value replaces the base value entirely
        when explicitly set (non-None / non-empty). Use the ``__extend``
        suffix on the override's TOML key (or ``--setting`` / env var) to
        get additive behavior — that resolution happens before merge_with
        is invoked.

        Carveout: the top-level *container* dicts (``agent_types``,
        ``providers``, ``plugins``, ``commands``, ``create_templates``)
        keep their per-key additive merge — adding ``[agent_types.foo]``
        at one scope does not drop another scope's ``[agent_types.bar]``.
        For keys that appear in both, the sub-class's ``merge_with`` is
        invoked, where leaf fields again use assign-by-default.
        """
        merged_agent_types = _merge_container_dict(self.agent_types, override.agent_types)
        merged_providers = _merge_container_dict(self.providers, override.providers)
        merged_plugins = _merge_container_dict(self.plugins, override.plugins)
        merged_commands = _merge_container_dict(self.commands, override.commands)
        merged_create_templates = _merge_container_dict(self.create_templates, override.create_templates)

        merged_retry = self.retry.merge_with(override.retry) if override.retry is not None else self.retry
        merged_logging = self.logging.merge_with(override.logging) if override.logging is not None else self.logging

        return self.__class__(
            prefix=_assign_scalar(self.prefix, override.prefix),
            default_host_dir=_assign_scalar(self.default_host_dir, override.default_host_dir),
            pager=_assign_scalar(self.pager, override.pager),
            unset_vars=override.unset_vars if override.unset_vars is not None else self.unset_vars,
            work_dir_extra_paths=override.work_dir_extra_paths
            if override.work_dir_extra_paths is not None
            else self.work_dir_extra_paths,
            enabled_backends=override.enabled_backends
            if override.enabled_backends is not None
            else self.enabled_backends,
            agent_types=merged_agent_types,
            providers=merged_providers,
            plugins=merged_plugins,
            # disabled_plugins is a deliberate carveout from the assign-by-default
            # rule: it is populated by CLI ``--disable-plugin`` flags via
            # ``_apply_plugin_overrides`` *after* layers have been merged, and
            # individual TOML layers do not normally write to it. An override
            # with an empty frozenset is indistinguishable from "not set", so we
            # preserve the base value instead of wiping it. Users wanting to
            # disable a specific plugin per-scope should use
            # ``[plugins.<name>] enabled = false`` rather than this field.
            disabled_plugins=override.disabled_plugins if override.disabled_plugins else self.disabled_plugins,
            commands=merged_commands,
            create_templates=merged_create_templates,
            pre_command_scripts=override.pre_command_scripts
            if override.pre_command_scripts is not None
            else self.pre_command_scripts,
            is_remote_agent_installation_allowed=_assign_scalar(
                self.is_remote_agent_installation_allowed,
                override.is_remote_agent_installation_allowed,
            ),
            connect_command=_assign_scalar(self.connect_command, override.connect_command),
            retry=merged_retry,
            logging=merged_logging,
            is_nested_tmux_allowed=_assign_scalar(self.is_nested_tmux_allowed, override.is_nested_tmux_allowed),
            headless=_assign_scalar(self.headless, override.headless),
            is_error_reporting_enabled=_assign_scalar(
                self.is_error_reporting_enabled,
                override.is_error_reporting_enabled,
            ),
            is_allowed_in_pytest=_assign_scalar(self.is_allowed_in_pytest, override.is_allowed_in_pytest),
            default_destroyed_host_persisted_seconds=_assign_scalar(
                self.default_destroyed_host_persisted_seconds,
                override.default_destroyed_host_persisted_seconds,
            ),
            default_min_online_host_age_seconds=_assign_scalar(
                self.default_min_online_host_age_seconds,
                override.default_min_online_host_age_seconds,
            ),
            agent_ready_timeout=_assign_scalar(self.agent_ready_timeout, override.agent_ready_timeout),
            allow_settings_key_assignment_narrowing=_assign_scalar(
                self.allow_settings_key_assignment_narrowing,
                override.allow_settings_key_assignment_narrowing,
            ),
        )


class MngrContext(FrozenModel):
    """Context object containing configuration and plugin manager.

    This combines MngrConfig and PluginManager into a single object
    that can be passed through the application, providing access to
    both configuration and plugin hooks.
    """

    model_config = {"arbitrary_types_allowed": True}

    config: MngrConfig = Field(
        description="Configuration for mngr",
    )
    pm: pluggy.PluginManager = Field(
        description="Plugin manager for hooks and backends",
    )
    is_interactive: bool = Field(
        default=False,
        description="Whether the CLI is running in interactive mode (can prompt user for input)",
    )
    is_auto_approve: bool = Field(
        default=False,
        description="Whether to auto-approve prompts (e.g., skill installation) without asking",
    )
    profile_dir: Path = Field(
        description="Profile-specific directory for user data (user_id, providers, settings)",
    )
    concurrency_group: ConcurrencyGroup = Field(
        default_factory=lambda: ConcurrencyGroup(name="default"),
        description="Top-level concurrency group for managing spawned processes",
    )
    is_full_discovery: bool = Field(
        default=False,
        description="When True, always query all providers during discovery (skip event-stream optimization)",
    )
    project_root: Path | None = Field(
        default=None,
        description="Project root directory (git worktree root)",
    )

    def get_plugin_config(self, name: str, config_type: type[PluginConfigT]) -> PluginConfigT:
        """Get a plugin's typed config, falling back to defaults if absent."""
        config = self.config.plugins.get(PluginName(name))
        if config is None:
            return config_type()
        if not isinstance(config, config_type):
            raise ConfigParseError(
                f"Plugin '{name}' config has type {type(config).__name__}, expected {config_type.__name__}"
            )
        return config

    def get_profile_user_id(self) -> UserId:
        return get_or_create_user_id(self.profile_dir)


class OutputOptions(FrozenModel):
    """Options for command output formatting."""

    output_format: OutputFormat = Field(
        default=OutputFormat.HUMAN,
        description="Output format for command results",
    )
    format_template: str | None = Field(
        default=None,
        description="Format template string for custom output formatting (set when --format is a template string rather than a built-in format name)",
    )
    is_quiet: bool = Field(
        default=False,
        description="Whether to suppress all stdout output (set by --quiet)",
    )


def get_or_create_user_id(profile_dir: Path) -> UserId:
    """Get or create a unique user ID for this mngr profile.

    The user ID is stored in a file in the profile directory. This ID is used
    to namespace Modal apps, ensuring that sandboxes created by different mngr
    installations on a shared Modal account don't interfere with each other.
    """
    user_id_file = profile_dir / USER_ID_FILENAME

    if user_id_file.exists():
        user_id = user_id_file.read_text().strip()
        if os.environ.get("MNGR_USER_ID", ""):
            assert user_id == os.environ.get("MNGR_USER_ID", ""), (
                "MNGR_USER_ID environment variable does not match existing user ID file"
            )
    else:
        if os.environ.get("MNGR_USER_ID", ""):
            user_id = os.environ.get("MNGR_USER_ID", "")
        else:
            # Generate a new user ID
            user_id = uuid4().hex
        atomic_write(user_id_file, user_id)
    return UserId(user_id)


class CommonCliOptions(FrozenModel):
    """Base class for common CLI options shared across all commands.

    This captures the options added by the @add_common_options decorator.
    All command-specific option classes should inherit from this class.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the @add_common_options decorator and its click.option() decorators.
    """

    headless: bool = False
    safe: bool = False
    output_format: str
    quiet: bool
    verbose: int
    log_file: str | None
    log_commands: bool | None
    plugin: tuple[str, ...]
    disable_plugin: tuple[str, ...]
    setting: tuple[str, ...] = ()


class CreateCliOptions(CommonCliOptions):
    """Options passed from the CLI to the create command.

    This captures all the click parameters so we can pass them as a single object
    to helper functions instead of passing dozens of individual parameters.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the create() function itself.
    """

    positional_name: NewAgentLocation | None
    positional_agent_type: str | None
    agent_args: tuple[str, ...]
    template: tuple[str, ...]
    type: str | None
    reuse: bool
    connect: bool
    foreground: bool
    connect_command: str | None
    ensure_clean: bool
    name: NewAgentLocation | None
    id: str | None
    name_style: str
    extra_window: tuple[str, ...]
    source: str | None
    target_path: str | None
    transfer: str | None
    rsync: bool | None
    rsync_args: str | None
    include_unclean: bool | None
    include_gitignored: bool
    branch: str
    env: tuple[str, ...]
    env_file: tuple[str, ...]
    pass_env: tuple[str, ...]
    provider: str | None
    new_host: bool
    host_name_style: str
    host_label: tuple[str, ...]
    label: tuple[str, ...]
    project: str | None
    host_env: tuple[str, ...]
    host_env_file: tuple[str, ...]
    pass_host_env: tuple[str, ...]
    snapshot: str | None
    build_arg: tuple[str, ...]
    start_arg: tuple[str, ...]
    post_host_create_command: tuple[str, ...]
    reconnect: bool
    message: str | None
    message_file: str | None
    edit_message: bool
    session_command: str | None
    idle_timeout: str | None
    idle_mode: str | None
    activity_sources: str | None
    worktree_base_folder: str | None
    start_on_boot: bool
    start_host: bool
    extra_provision_command: tuple[str, ...]
    upload_file: tuple[str, ...]
    update: bool
    yes: bool
    tmux_width: int | None
    tmux_height: int | None
    tmux_window_size: str | None
