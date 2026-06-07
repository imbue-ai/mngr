"""Shared runtime helpers for driving a local mngr claude agent unattended.

Both the ``mngr robinhood`` CLI orchestrator and the in-process Agent SDK
(``imbue.mngr_robinhood.agent_sdk``) spin up an ephemeral local claude agent, forward the
parent process's credentials/env into it, read its native transcript, and stop/destroy it when
done. Those cross-cutting pieces live here so both callers share one implementation.
"""

import os
from typing import Final

from loguru import logger

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.cli.common_opts import apply_settings_to_config
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME

# Settings overrides applied to mngr_ctx so the spawned claude agent runs unattended. The two
# ``settings_overrides`` flags are normally added by ``mngr_claude`` only when the host is
# remote (``ProvisioningContext.is_unattended`` == ``not host.is_local``); both robinhood and
# the SDK always run on the local host, so we set them explicitly to avoid hangs on the
# "bypass permissions mode" and "skip dangerous mode" prompts.
UNATTENDED_SETTINGS: Final[tuple[str, ...]] = (
    "agent_types.claude.auto_dismiss_dialogs=true",
    "agent_types.claude.auto_allow_permissions=true",
    "agent_types.claude.settings_overrides.skipDangerousModePermissionPrompt=true",
    "agent_types.claude.settings_overrides.bypassPermissionsModeAccepted=true",
)

# Env var prefixes that mngr's own ``_collect_agent_env_vars`` sets per-agent (state dir, work
# dir, ids, ...). Forwarding the parent process's values for any of these would *override* the
# spawned agent's correct values at the "explicit env_vars" step of env-var collection, breaking
# the readiness hook (which writes ``$MNGR_AGENT_STATE_DIR/session_started``), the
# background-tasks script, the common-transcript writer, and anything else keyed on the
# per-agent state dir.
PER_AGENT_ENV_VARS_TO_DROP: Final[frozenset[str]] = frozenset(
    {
        "MNGR_AGENT_ID",
        "MNGR_AGENT_NAME",
        "MNGR_AGENT_STATE_DIR",
        "MNGR_AGENT_WORK_DIR",
        "MNGR_HOST_DIR",
        "LLM_USER_PATH",
    }
)

# Generous readiness timeout: claude needs time to start, dismiss dialogs, and reach the
# prompt-ready state in a fresh worktree before the first message is delivered. mngr's
# 10-second default is too short here.
AGENT_READY_TIMEOUT_SECONDS: Final[float] = 120.0

# Poll cadence for end-of-turn detection plus transcript tailing.
POLL_INTERVAL_SECONDS: Final[float] = 0.1

# Claude API stop_reason values that mean "this assistant message is the LAST one of the turn".
# Anything else (notably ``tool_use``, or a missing stop_reason) means more events are still
# coming -- either later cycles within the same turn, or a follow-up message that hasn't been
# mirrored from claude's per-session JSONL into the raw transcript yet.
TERMINAL_STOP_REASONS: Final[frozenset[str]] = frozenset({"end_turn", "stop_sequence", "max_tokens"})

# Safety net: if the transcript stops growing for this long while the agent is still alive, bail
# out and finalize with whatever we have. The legitimate maximum gap between assistant events
# inside a single turn is bounded by the longest tool the agent might run (long bash builds,
# slow MCP calls), so this needs to be very generous.
TURN_END_NO_PROGRESS_TIMEOUT_SECONDS: Final[float] = 600.0

# Lifecycle states that mean the agent is no longer alive. Reaching one of these mid-turn is a
# failure: the agent will never produce another assistant_message. STOPPED/DONE are the natural
# end-of-life states, REPLACED means the agent's tmux pane was hijacked by another process, and
# RUNNING_UNKNOWN_AGENT_TYPE means mngr no longer recognizes the agent type.
AGENT_DEAD_STATES: Final[frozenset[AgentLifecycleState]] = frozenset(
    {
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.DONE,
        AgentLifecycleState.REPLACED,
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
    }
)


def normalize_credentials_env() -> None:
    """Unset ``ORIGINAL_CLAUDE_CONFIG_DIR`` so mngr_claude reads credentials from ``$CLAUDE_CONFIG_DIR``.

    When this runs from inside another mngr claude agent, the parent process has
    ``ORIGINAL_CLAUDE_CONFIG_DIR=~/.claude`` set by that parent agent's ``modify_env_vars`` and
    ``CLAUDE_CONFIG_DIR`` set to the parent agent's per-agent config dir. mngr_claude's
    credentials sync prefers ``ORIGINAL_CLAUDE_CONFIG_DIR`` -> ``~/.claude`` -- but on machines
    where the user has never run ``claude login`` outside of mngr, ``~/.claude/`` has no
    credentials, so the sync is a no-op and the spawned claude boots without auth. Dropping
    ``ORIGINAL_CLAUDE_CONFIG_DIR`` makes resolution fall through to ``CLAUDE_CONFIG_DIR`` (the
    parent agent's per-agent dir, which DOES have credentials). Safe in the no-parent-agent case
    too: ``ORIGINAL_CLAUDE_CONFIG_DIR`` is not normally set in a plain shell.
    """
    os.environ.pop("ORIGINAL_CLAUDE_CONFIG_DIR", None)


def apply_unattended_settings(mngr_ctx: MngrContext, extra_settings: tuple[str, ...] = ()) -> MngrContext:
    """Inject the claude agent-type config overrides for unattended operation.

    ``extra_settings`` (e.g. streaming overrides) are merged into the SAME
    ``apply_settings_to_config`` call so the ``settings_overrides`` dict is assembled in one shot (a
    second merge over the non-empty dict would trip the settings-narrowing guard).
    """
    updated_config = apply_settings_to_config(
        mngr_ctx.config,
        UNATTENDED_SETTINGS + extra_settings,
        mngr_ctx.config.disabled_plugins,
    )
    return mngr_ctx.model_copy_update(to_update(mngr_ctx.field_ref().config, updated_config))


# Characters that break the agent env file when a value is written unquoted and then sourced
# (mngr's env-file writer does not quote backtick / command-substitution sequences). A single
# such value silently swallows every variable written after it, so we drop those values rather
# than forward them. Backtick and ``$(`` start a command substitution; a newline truncates the
# line mid-value.
_SHELL_UNSAFE_VALUE_FRAGMENTS: Final[tuple[str, ...]] = ("`", "$(", "\n", "\r")

# Prefixes of terminal-emulator environment variables that must NOT be forwarded to a headless
# tmux agent. The critical one is ``KITTY_SHELL_INTEGRATION=enabled``: when present, the agent's
# login shell tries to load kitty's shell-integration scripts even though it is running inside a
# detached tmux pane (not a real kitty window), which wedges shell startup so claude never boots
# and never writes its ``session_started`` readiness signal -- the agent then times out / hangs.
# The whole ``KITTY_*`` family is terminal-specific and useless to a headless agent, so we drop it
# all. (On main this breakage is masked by luck: ``KITTY_PUBLIC_KEY``'s backtick value triggers an
# unterminated command substitution that happens to swallow the following ``KITTY_SHELL_INTEGRATION``
# line; dropping the unsafe-valued ``KITTY_PUBLIC_KEY`` removes that accident and exposes the bug.)
_TERMINAL_ENV_VAR_PREFIXES_TO_DROP: Final[tuple[str, ...]] = ("KITTY_",)


def _is_forwardable_env_var(key: str, value: str) -> bool:
    """True if this process env var is safe to write into the agent's sourced env file.

    Drops the per-agent ``MNGR_*`` / ``LLM_USER_PATH`` vars that mngr sets itself; terminal-emulator
    vars (``KITTY_*``) that wedge a headless agent's shell startup; exported bash function definitions
    (``BASH_FUNC_*`` keys, whose multi-line ``() { ... }`` values corrupt the env file); and any value
    containing shell-unsafe fragments (e.g. a backtick) that would break sourcing of the env file and
    drop every variable written after it.
    """
    if key in PER_AGENT_ENV_VARS_TO_DROP:
        return False
    if key.startswith("BASH_FUNC_"):
        return False
    if any(key.startswith(prefix) for prefix in _TERMINAL_ENV_VAR_PREFIXES_TO_DROP):
        return False
    if any(fragment in value for fragment in _SHELL_UNSAFE_VALUE_FRAGMENTS):
        return False
    return True


def build_pass_env_vars() -> AgentEnvironmentOptions:
    """Forward variables from the current process environment to the agent.

    Everything safe is passed through -- including ``ANTHROPIC_API_KEY``, without which the
    spawned claude boots unauthenticated and only emits synthetic error messages. See
    :func:`_is_forwardable_env_var` for what is filtered out and why.
    """
    pairs = tuple(
        EnvVar(key=key, value=value) for key, value in os.environ.items() if _is_forwardable_env_var(key, value)
    )
    return AgentEnvironmentOptions(env_vars=pairs)


def build_events_target(mngr_ctx: MngrContext, agent: AgentInterface) -> EventsTarget | None:
    """Build the events target used to read the agent's raw transcript from its local host."""
    return try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=agent.id,
        agent_name=str(agent.name),
        host_id=agent.host_id,
        provider_name=LOCAL_PROVIDER_NAME,
    )


def stop_agent(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """Best-effort: stop the agent (leaving its state on disk), swallowing cleanup errors."""
    try:
        host.stop_agents([agent.id])
    except (OSError, MngrError) as exc:
        logger.warning("Failed to stop agent {}: {}", agent.name, exc)


def destroy_agent(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """Best-effort: stop and destroy the agent (removing its state), swallowing cleanup errors."""
    stop_agent(agent, host)
    try:
        host.destroy_agent(agent)
    except (OSError, MngrError) as exc:
        logger.warning("Failed to destroy agent {}: {}", agent.name, exc)
