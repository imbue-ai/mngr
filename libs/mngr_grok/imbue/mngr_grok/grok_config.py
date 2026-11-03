import json
import shlex
import urllib.parse
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import tomlkit
from pydantic import Field
from tomlkit.exceptions import TOMLKitError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_agent import quote_agent_args
from imbue.mngr.config.external_settings import apply_settings_patch
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.primitives import CommandString
from imbue.mngr_grok.resources.grok_hook import PENDING_WAKE_PROMPT_ID_PREFIX

# Agent state layout, relative to $MNGR_AGENT_STATE_DIR; the launch script hardcodes the same paths.
GROK_PLUGIN_STATE_RELATIVE_PATH: Final[Path] = Path("plugin", "grok")
GROK_HOME_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "home"
LAUNCH_ENV_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "launch.env"
SESSION_ID_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "session_id"
FORK_FROM_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "fork_from"
ROLE_PROMPT_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "role_prompt.md"
# The `[models]` values the agent type and its settings_overrides set at the last provision.
RENDERED_MODEL_SETTINGS_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "rendered_model_settings.json"
# The hook handler's files, one per top-level turn in flight.
ROOT_TURNS_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "turns"

LAUNCH_SCRIPT_NAME: Final[str] = "grok_launch.sh"
_LAUNCH_SCRIPT_INVOCATION: Final[str] = f'bash "$MNGR_AGENT_STATE_DIR/commands/{LAUNCH_SCRIPT_NAME}"'

# Grok resolves this against the hooks file's directory and execs it directly; a space would route it through sh.
_HOOK_HANDLER_COMMAND: Final[str] = "./grok_hook.py"

# The events the handler acts on; every registered hook blocks grok for the handler's run time.
GROK_HOOK_EVENT_NAMES: Final[tuple[str, ...]] = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "Stop",
    "StopFailure",
    "StopCancelled",
    "Notification",
    "SubagentStart",
    "SessionEnd",
)

# The per-surface cells of grok's `[compat.claude]` and `[compat.cursor]` tables.
COMPAT_CELL_NAMES: Final[tuple[str, ...]] = ("skills", "rules", "agents", "mcps", "hooks", "sessions")

# Removed from grok's environment on every launch: each can disable mngr's hooks, overlay config or switch trust off.
REMOVED_GROK_ENVIRONMENT_VARIABLE_NAMES: Final[tuple[str, ...]] = (
    "GROK_AGENT",
    "GROK_CONFIG",
    "GROK_CONFIG_PATH",
    "GROK_FOLDER_TRUST",
    "CLAUDECODE",
)

_PERMISSION_MODE_ALWAYS_APPROVE: Final[str] = "always-approve"
_PERMISSION_MODE_ASK: Final[str] = "ask"
# A cancelled turn leaves running subagents alone instead of asking with a card.
_CANCEL_SUBAGENTS_ALWAYS_CONTINUE: Final[str] = "always_continue"

_MODELS_DEFAULT_KEY: Final[str] = "default"
_MODELS_DEFAULT_REASONING_EFFORT_KEY: Final[str] = "default_reasoning_effort"
# The `[models]` keys grok writes after `/model` and `/effort`, kept across a re-provision.
_CARRIED_OVER_MODEL_KEYS: Final[tuple[str, ...]] = (_MODELS_DEFAULT_KEY, _MODELS_DEFAULT_REASONING_EFFORT_KEY)

# Keys of a `[model.<id>]` table that select a credential other than the session login.
_CUSTOM_MODEL_CREDENTIAL_KEYS: Final[tuple[str, ...]] = ("base_url", "api_key", "env_key")

_MAX_SESSION_BUCKET_NAME_BYTES: Final[int] = 255

_RFC_3339_UTC_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"


class MalformedGrokConfigError(PluginMngrError, ValueError):
    """Raised when a file mngr reads to render a grok config.toml cannot be parsed or has an unexpected shape."""


class SessionBucketNameError(PluginMngrError, ValueError):
    """Raised when mngr cannot reproduce grok's session bucket name for a work dir."""


class RenderedGrokConfig(FrozenModel):
    """A rendered per-agent config.toml body and the `[models]` values mngr's own inputs put in it."""

    body: dict[str, Any] = Field(description="The config.toml body, with the [models] choices grok saved carried over")
    own_model_settings: dict[str, str] = Field(
        description="The [models] values the agent type and its settings_overrides set, before any carry-over"
    )


class GrokRootTurn(FrozenModel):
    """A top-level grok turn in flight, as mngr's hook handler records it."""

    session_id: str = Field(description="The root session the turn runs in")
    prompt_id: str = Field(description="The turn's promptId, or `pending-wake:<subagent id>` for a placeholder")
    is_pending_wake: bool = Field(
        description="Whether this is the placeholder for an auto-wake turn grok has not started yet"
    )


@pure
def get_grok_home(agent_state_dir: Path) -> Path:
    return agent_state_dir / GROK_HOME_RELATIVE_PATH


@pure
def parse_root_turn(session_id: str, turn_text: str) -> GrokRootTurn:
    """The turn a `turns/<session id>` file records: `<promptId>\\n<tail>\\n`."""
    prompt_id = turn_text.split("\n", 1)[0].strip()
    return GrokRootTurn(
        session_id=session_id,
        prompt_id=prompt_id,
        is_pending_wake=prompt_id.startswith(PENDING_WAKE_PROMPT_ID_PREFIX),
    )


@pure
def parse_grok_config_text(text: str, source_description: str) -> dict[str, Any]:
    """Parse a grok config.toml into plain Python values; blank text is an empty config."""
    if not text.strip():
        return {}
    try:
        document = tomlkit.parse(text)
    except TOMLKitError as exc:
        raise MalformedGrokConfigError(
            f"{source_description} contains malformed TOML ({exc}); fix it by hand, then re-run."
        ) from exc
    return document.unwrap()


@pure
def _read_consent_answers(config: Mapping[str, Any], source_description: str) -> dict[str, dict[str, Any]]:
    consent_table = config.get("consent", {})
    if not isinstance(consent_table, Mapping):
        raise MalformedGrokConfigError(f"{source_description} has a [consent] value that is not a table")
    answers_table = consent_table.get("answers", {})
    if not isinstance(answers_table, Mapping):
        raise MalformedGrokConfigError(f"{source_description} has a [consent.answers] value that is not a table")
    answer_by_notice_id: dict[str, dict[str, Any]] = {}
    for notice_id, answer in answers_table.items():
        if not isinstance(answer, Mapping):
            raise MalformedGrokConfigError(
                f"{source_description} has a [consent.answers.{notice_id}] that is not a table"
            )
        version = answer.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise MalformedGrokConfigError(
                f"{source_description} has a [consent.answers.{notice_id}] whose version is not an integer"
            )
        if not isinstance(answer.get("account"), str):
            raise MalformedGrokConfigError(
                f"{source_description} has a [consent.answers.{notice_id}] whose account is not a string"
            )
        answer_by_notice_id[notice_id] = dict(answer)
    return answer_by_notice_id


@pure
def _is_agent_answer_kept_over_user_answer(agent_answer: Mapping[str, Any], user_answer: Mapping[str, Any]) -> bool:
    return agent_answer["account"] == user_answer["account"] and agent_answer["version"] > user_answer["version"]


@pure
def merge_consent_answers(existing_agent_config_text: str, user_config_text: str) -> dict[str, dict[str, Any]]:
    """Merge the `[consent.answers.<id>]` tables of the agent's config.toml and the one beside the shared credential.

    Per notice id: an answer present on one side is kept; for the same account the higher version wins (the
    credential side on a tie); for different accounts the credential side wins, because under shared auth its
    account is the agent's login. Pass an empty user text when the agent keeps its own credential.
    """
    agent_answers = _read_consent_answers(
        parse_grok_config_text(existing_agent_config_text, "The agent's grok config.toml"),
        "The agent's grok config.toml",
    )
    user_answers = _read_consent_answers(
        parse_grok_config_text(user_config_text, "The user's grok config.toml"),
        "The user's grok config.toml",
    )
    merged_answers = dict(agent_answers)
    for notice_id, user_answer in user_answers.items():
        agent_answer = agent_answers.get(notice_id)
        if agent_answer is None or not _is_agent_answer_kept_over_user_answer(agent_answer, user_answer):
            merged_answers[notice_id] = user_answer
    return dict(sorted(merged_answers.items()))


@pure
def _read_carried_over_model_setting(existing_config: Mapping[str, Any], key: str) -> str | None:
    models_table = existing_config.get("models", {})
    if not isinstance(models_table, Mapping):
        raise MalformedGrokConfigError("The agent's grok config.toml has a [models] value that is not a table")
    value = models_table.get(key)
    if value is not None and not isinstance(value, str):
        raise MalformedGrokConfigError(f"The agent's grok config.toml has a [models] {key} that is not a string")
    return value


@pure
def _get_own_model_settings(config: Mapping[str, Any]) -> dict[str, str]:
    models_table = config.get("models", {})
    if not isinstance(models_table, Mapping):
        return {}
    return {key: models_table[key] for key in _CARRIED_OVER_MODEL_KEYS if isinstance(models_table.get(key), str)}


@pure
def _fill_in_carried_over_model_settings(
    config: dict[str, Any],
    existing_config: Mapping[str, Any],
    previously_rendered_model_settings: Mapping[str, str],
) -> dict[str, Any]:
    """Add each `[models]` choice made inside the agent that neither the agent type nor its overrides set.

    An existing value that equals what mngr rendered for that key at the last provision is mngr's own, not a choice.
    """
    models_table = config.get("models", {})
    if not isinstance(models_table, Mapping):
        return config
    carried_over_settings: dict[str, str] = {}
    for key in _CARRIED_OVER_MODEL_KEYS:
        if key in models_table:
            continue
        value = _read_carried_over_model_setting(existing_config, key)
        if value is not None and value != previously_rendered_model_settings.get(key):
            carried_over_settings[key] = value
    if not carried_over_settings:
        return config
    return {**config, "models": {**models_table, **carried_over_settings}}


@pure
def parse_rendered_model_settings(text: str, source_description: str) -> dict[str, str]:
    """Parse the record `render_rendered_model_settings` writes; blank text is an empty record."""
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedGrokConfigError(
            f"{source_description} is not valid JSON ({exc}); remove it, then re-run."
        ) from exc
    if not isinstance(settings, dict) or not all(isinstance(value, str) for value in settings.values()):
        raise MalformedGrokConfigError(
            f"{source_description} is not a JSON object of strings; remove it, then re-run."
        )
    return settings


@pure
def render_rendered_model_settings(own_model_settings: Mapping[str, str]) -> str:
    return json.dumps(dict(own_model_settings), sort_keys=True) + "\n"


@pure
def strip_claude_plugin_marketplace_suffixes(enabled_plugin_keys: Sequence[str]) -> list[str]:
    """The bare plugin names grok's `[plugins] disabled` matches, from Claude `enabledPlugins` keys like `name@marketplace`."""
    names = {key.rpartition("@")[0] or key for key in enabled_plugin_keys}
    return sorted(name for name in names if name)


@pure
def fold_settings_overrides_onto_empty_config(settings_overrides: Mapping[str, Any]) -> dict[str, Any]:
    """What a `settings_overrides` patch sets on its own: the patch folded onto an empty config.toml."""
    return apply_settings_patch(
        {},
        settings_overrides,
        allow_narrowing=True,
        base_description="an empty grok config.toml",
    )


@pure
def _does_override_select_custom_model(settings_overrides: Mapping[str, Any]) -> bool:
    model_table = fold_settings_overrides_onto_empty_config(settings_overrides).get("model", {})
    if not isinstance(model_table, Mapping):
        return False
    return any(
        isinstance(model_definition, Mapping) and any(key in model_definition for key in _CUSTOM_MODEL_CREDENTIAL_KEYS)
        for model_definition in model_table.values()
    )


@pure
def _build_claude_compat_tables(
    *,
    is_isolated_from_claude_config: bool,
    subagent_names_to_disable: Sequence[str],
    enabled_claude_plugin_keys: Sequence[str],
) -> dict[str, Any]:
    if is_isolated_from_claude_config:
        all_cells_off = {cell: False for cell in COMPAT_CELL_NAMES}
        tables: dict[str, Any] = {
            "claude_compat": {"imported": True},
            "compat": {"claude": dict(all_cells_off), "cursor": dict(all_cells_off), "codex": {"sessions": False}},
        }
        sorted_subagent_names = sorted(set(subagent_names_to_disable))
        if sorted_subagent_names:
            tables["subagents"] = {"toggle": {name: False for name in sorted_subagent_names}}
        return tables
    opt_out_tables: dict[str, Any] = {"compat": {"claude": {"hooks": False}, "cursor": {"hooks": False}}}
    disabled_plugin_names = strip_claude_plugin_marketplace_suffixes(enabled_claude_plugin_keys)
    if disabled_plugin_names:
        opt_out_tables["plugins"] = {"disabled": disabled_plugin_names}
    return opt_out_tables


@pure
def build_config_toml(
    *,
    model: str | None,
    reasoning_effort: str | None,
    is_auto_allow_permissions: bool,
    is_self_update_disabled: bool,
    is_privacy_pinned: bool,
    is_memory_enabled: bool,
    is_isolated_from_claude_config: bool,
    # Agent definition names found in the user's Claude and legacy grok agent dirs; used only under isolation.
    subagent_names_to_disable: Sequence[str],
    # Keys of the user's Claude `enabledPlugins`; used only when not isolated.
    enabled_claude_plugin_keys: Sequence[str],
    privacy_banner_acked_at: datetime,
    existing_agent_config_text: str,
    # The config.toml beside the credential the agent shares, or empty text when it keeps its own credential.
    user_config_text: str,
    settings_overrides: Mapping[str, Any],
    is_settings_narrowing_allowed: bool,
    # The `own_model_settings` of the agent's last provision, from RENDERED_MODEL_SETTINGS_RELATIVE_PATH.
    previously_rendered_model_settings: Mapping[str, str],
) -> RenderedGrokConfig:
    """Build the per-agent grok config.toml body; `settings_overrides` is folded on with `apply_settings_patch`.

    The `[models]` choices grok saved inside the agent fill only the keys the patched body leaves unset.
    """
    existing_config = parse_grok_config_text(existing_agent_config_text, "The agent's grok config.toml")
    is_session_login_bypassed = _does_override_select_custom_model(settings_overrides)

    cli_table: dict[str, Any] = {"auto_update": False} if is_self_update_disabled else {}
    features_table: dict[str, Any] = (
        {"telemetry": False, "feedback": False, "campaigns": False} if is_privacy_pinned else {}
    )
    config: dict[str, Any] = {
        "cli": {**cli_table, "use_leader": False, "show_tips": False},
        "features": {**features_table, "remote_fetch": not is_session_login_bypassed},
    }
    if is_privacy_pinned:
        config["telemetry"] = {"trace_upload": False, "mixpanel_enabled": False}
        config["harness"] = {"disable_workspace_teleport": True, "disable_codebase_upload": True}
    config["ui"] = {
        "permission_mode": _PERMISSION_MODE_ALWAYS_APPROVE if is_auto_allow_permissions else _PERMISSION_MODE_ASK,
        "prompt_suggestions": False,
        "cancel_subagents_on_turn_cancel": _CANCEL_SUBAGENTS_ALWAYS_CONTINUE,
    }

    models_table = {
        key: value
        for key, value in ((_MODELS_DEFAULT_KEY, model), (_MODELS_DEFAULT_REASONING_EFFORT_KEY, reasoning_effort))
        if value is not None
    }
    if models_table:
        config["models"] = models_table

    config["privacy"] = {
        "privacy_banner_acked": privacy_banner_acked_at.astimezone(timezone.utc).strftime(_RFC_3339_UTC_FORMAT)
    }
    config["memory"] = {"enabled": is_memory_enabled}
    config.update(
        _build_claude_compat_tables(
            is_isolated_from_claude_config=is_isolated_from_claude_config,
            subagent_names_to_disable=subagent_names_to_disable,
            enabled_claude_plugin_keys=enabled_claude_plugin_keys,
        )
    )
    consent_answers = merge_consent_answers(existing_agent_config_text, user_config_text)
    if consent_answers:
        config["consent"] = {"answers": consent_answers}

    patched_config = apply_settings_patch(
        config,
        settings_overrides,
        allow_narrowing=is_settings_narrowing_allowed,
        base_description="mngr's rendered grok config.toml",
    )
    return RenderedGrokConfig(
        body=_fill_in_carried_over_model_settings(patched_config, existing_config, previously_rendered_model_settings),
        own_model_settings=_get_own_model_settings(patched_config),
    )


@pure
def render_toml(document_body: Mapping[str, Any]) -> str:
    document = tomlkit.document()
    for key, value in document_body.items():
        document[key] = value
    return tomlkit.dumps(document)


@pure
def build_requirements_toml(is_privacy_pinned: bool) -> str:
    """The grok home's requirements.toml: privacy pins that beat server campaigns, remote settings and env vars."""
    if not is_privacy_pinned:
        return ""
    return render_toml(
        {
            "features": {"telemetry": False},
            "telemetry": {"trace_upload": False},
            "harness": {"disable_workspace_teleport": True, "disable_codebase_upload": True},
            "cli": {"use_leader": False},
        }
    )


@pure
def build_hooks_json() -> dict[str, Any]:
    """Build the grok home's hooks/mngr.json: every grok hook event runs the handler beside it."""
    return {
        "hooks": {
            event_name: [{"hooks": [{"type": "command", "command": _HOOK_HANDLER_COMMAND}]}]
            for event_name in GROK_HOOK_EVENT_NAMES
        }
    }


@pure
def compute_session_bucket_name(canonical_path: str) -> str:
    """The directory in the grok home's sessions/ that holds a work dir's sessions, e.g. `%2Fhome%2Fu%2Frepo`."""
    if not canonical_path.startswith("/"):
        raise SessionBucketNameError(f"The session bucket needs an absolute canonical path, got {canonical_path!r}")
    bucket_name = urllib.parse.quote(canonical_path, safe="")
    if len(bucket_name.encode("utf-8")) > _MAX_SESSION_BUCKET_NAME_BYTES:
        raise SessionBucketNameError(
            f"The work dir {canonical_path} is too long for mngr to place a grok session in it: its encoded form "
            f"is {len(bucket_name)} bytes, over {_MAX_SESSION_BUCKET_NAME_BYTES}, where grok switches to a hashed "
            "bucket name that mngr does not reproduce. Use a work dir with a shorter path."
        )
    return bucket_name


@pure
def build_pinned_grok_environment(
    *,
    grok_home: Path,
    is_privacy_pinned: bool,
    is_isolated_from_claude_config: bool,
) -> dict[str, str]:
    """The environment variables set on every grok process, keeping it on its own home and mngr's pins."""
    environment: dict[str, str] = {
        "GROK_HOME": str(grok_home),
        # Otherwise grok deletes the grok home's requirements.toml at session start.
        "GROK_MANAGED_CONFIG": "0",
        "GROK_CHANGELOG_OFFLINE": "1",
    }
    isolated_cells = COMPAT_CELL_NAMES if is_isolated_from_claude_config else ("hooks",)
    for compat_source in ("CLAUDE", "CURSOR"):
        for cell in isolated_cells:
            environment[f"GROK_{compat_source}_{cell.upper()}_ENABLED"] = "0"
    if is_isolated_from_claude_config:
        environment["GROK_CODEX_SESSIONS_ENABLED"] = "0"
    if is_privacy_pinned:
        environment["DISABLE_TELEMETRY"] = "1"
        environment["GROK_TELEMETRY_TRACE_UPLOAD"] = "0"
        environment["GROK_CAMPAIGNS"] = "0"
    return environment


@pure
def build_removed_grok_environment_names(is_auth_shared: bool) -> tuple[str, ...]:
    if is_auth_shared:
        return REMOVED_GROK_ENVIRONMENT_VARIABLE_NAMES
    return REMOVED_GROK_ENVIRONMENT_VARIABLE_NAMES + ("GROK_AUTH_PATH",)


@pure
def build_launch_command(
    command: CommandString,
    cli_args: Sequence[str],
    agent_args: Sequence[str],
    command_override: CommandString | None,
) -> CommandString:
    """The stored launch command: the launch script followed by the grok command words.

    The base command and `cli_args` are already shell-safe and are spliced as written; `agent_args` are raw argv
    and are quoted.
    """
    base_command = command_override if command_override is not None else command
    words = (_LAUNCH_SCRIPT_INVOCATION, str(base_command), *cli_args, *quote_agent_args(tuple(agent_args)))
    return CommandString(" ".join(words))


@pure
def _render_shell_array_assignment(name: str, values: Sequence[str]) -> str:
    return f"{name}=(" + " ".join(shlex.quote(value) for value in values) + ")"


@pure
def build_launch_env(
    *,
    grok_home: Path,
    work_dir: Path,
    # The user's grok credential file, resolved on the host; None keeps the credential inside the agent's home.
    shared_user_auth_path: Path | None,
    model: str | None,
    reasoning_effort: str | None,
    is_privacy_pinned: bool,
    is_isolated_from_claude_config: bool,
) -> str:
    """Render plugin/grok/launch.env, the bash assignments the launch script sources."""
    pinned_environment = build_pinned_grok_environment(
        grok_home=grok_home,
        is_privacy_pinned=is_privacy_pinned,
        is_isolated_from_claude_config=is_isolated_from_claude_config,
    )
    removed_environment_names = build_removed_grok_environment_names(is_auth_shared=shared_user_auth_path is not None)
    lines = [
        "# Rendered by mngr at provision; sourced by commands/grok_launch.sh.",
        f"launch_grok_home={shlex.quote(str(grok_home))}",
        f"launch_work_dir={shlex.quote(str(work_dir))}",
        f"launch_is_auth_shared={'true' if shared_user_auth_path is not None else 'false'}",
        f"launch_user_auth_path={shlex.quote(str(shared_user_auth_path) if shared_user_auth_path is not None else '')}",
        f"launch_model={shlex.quote(model or '')}",
        f"launch_reasoning_effort={shlex.quote(reasoning_effort or '')}",
        _render_shell_array_assignment(
            "launch_pinned_environment", [f"{name}={value}" for name, value in pinned_environment.items()]
        ),
        _render_shell_array_assignment("launch_removed_environment", removed_environment_names),
    ]
    return "\n".join(lines) + "\n"
