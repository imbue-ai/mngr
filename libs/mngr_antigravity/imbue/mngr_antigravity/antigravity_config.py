"""Read/write helpers for Antigravity CLI (``agy``) config under a per-agent ``$HOME``.

``agy`` resolves its entire config/permission/auth/session tree from
``$HOME/.gemini`` and exposes **no** config-dir override env var (no
``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` analog, no ``--config-dir`` flag). The only
lever that yields a per-agent ``settings.json`` -- and therefore per-agent
permissions, model, and isolated transcripts/conversations -- is a **per-agent
``$HOME``**. ``mngr_antigravity`` provisions one such ``$HOME`` per agent (under
the agent state dir) and launches ``agy`` under it (see the plugin's
``provision`` / ``assemble_command``).

This module holds the pure, host-agnostic pieces of that scheme:

* Path builders (``get_antigravity_*_path``) that take a ``$HOME`` root, so the
  same functions address both the user's real ``~/.gemini`` (the copy/auth
  source) and each agent's relocated ``$HOME`` (the destination).
* ``build_isolated_settings`` -- layers the per-agent ``settings.json`` from a
  base (a copy of the user's real settings when ``sync_home_settings``, else an
  empty dict), the agent's trusted workspace path, and the per-agent-type
  ``settings_overrides`` (applied last, so they win). This is the single data
  builder the spec's "one code path" principle rests on: whether an agent is
  locked down or open is purely whether ``settings_overrides`` carries a
  ``permissions`` block.
* ``build_onboarding_seed`` -- the fixed object that, written to
  ``$HOME/.gemini/antigravity-cli/cache/onboarding.json``, skips agy's
  first-run NUX (theme + ToS/telemetry) that would otherwise intercept the
  first message.
* The lifecycle ``active``-marker hooks (``build_antigravity_hooks_config``),
  written to the per-agent ``$HOME/.gemini/config/hooks.json`` (agy executes
  hooks from there with no trust prompt and no ``--add-dir``).

Trust: agy suppresses its first-launch "Do you trust this folder?" dialog for
any path present in its ``settings.json`` ``trustedWorkspaces`` array. The
running (isolated) agy reads only its per-agent ``settings.json``, so the
agent's effective workspace path is seeded there. The user's real global
``settings.json`` is additionally used to *persist* the durable source-repo
path (so trust isn't re-prompted across agents/worktrees of the same repo);
``merge_trusted_workspace`` performs that additive, idempotent global write.

Permission auto-approval is NOT done via a hook -- agy's documented
``PreToolUse`` ``{"decision": "allow"}`` output does not gate the
``run_command`` confirmation dialog -- so the plugin uses either a
``permissions`` policy in ``settings_overrides`` or the
``--dangerously-skip-permissions`` CLI flag (see the plugin's
``assemble_command``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface

# agy's config tree lives under ``$HOME/.gemini``. These segments name the
# files within it that mngr reads (the user's real home) or writes (a per-agent
# home). All are addressed relative to a ``$HOME`` root so the same builders
# serve both roles.
_GEMINI_DIR_NAME: str = ".gemini"
_ANTIGRAVITY_CLI_DIR_NAME: str = "antigravity-cli"
_SETTINGS_FILENAME: str = "settings.json"

# File token agy writes at login (``ChainedAuth``: keyring-first, file-fallback;
# on Linux -- mngr's real runtime -- the file is the native store). mngr
# symlinks/copies this from the user's real home into each per-agent home so the
# isolated agy is authenticated without its own login flow.
_OAUTH_TOKEN_FILENAME: str = "antigravity-oauth-token"

# First-run NUX gate. agy shows the theme + ToS/telemetry flow unless this file
# exists; it is keyed by the file's presence/content, NOT by any settings.json
# key (verified empirically). Seeding it skips the NUX that would otherwise
# intercept the first message.
_ONBOARDING_CACHE_RELATIVE: tuple[str, ...] = ("cache", "onboarding.json")

# agy executes hooks from ``$HOME/.gemini/config/hooks.json`` (and per-workspace
# ``.agents/hooks.json``) with no trust prompt. Under a per-agent ``$HOME`` this
# is the single hooks path -- no ``--add-dir`` symlink workaround needed.
_HOOKS_CONFIG_RELATIVE: tuple[str, ...] = ("config", "hooks.json")


def get_antigravity_cli_dir(home: Path) -> Path:
    """Return ``<home>/.gemini/antigravity-cli`` (agy's CLI-mode app-data dir)."""
    return home / _GEMINI_DIR_NAME / _ANTIGRAVITY_CLI_DIR_NAME


def get_antigravity_settings_path(home: Path) -> Path:
    """Return the ``settings.json`` path under ``home``'s ``.gemini`` tree."""
    return get_antigravity_cli_dir(home) / _SETTINGS_FILENAME


def get_antigravity_oauth_token_path(home: Path) -> Path:
    """Return the ``antigravity-oauth-token`` file path under ``home``'s ``.gemini`` tree."""
    return get_antigravity_cli_dir(home) / _OAUTH_TOKEN_FILENAME


def get_antigravity_onboarding_cache_path(home: Path) -> Path:
    """Return the NUX ``cache/onboarding.json`` path under ``home``'s ``.gemini`` tree."""
    return get_antigravity_cli_dir(home).joinpath(*_ONBOARDING_CACHE_RELATIVE)


def get_antigravity_hooks_config_path(home: Path) -> Path:
    """Return ``<home>/.gemini/config/hooks.json`` -- where agy executes hooks from."""
    return home.joinpath(_GEMINI_DIR_NAME, *_HOOKS_CONFIG_RELATIVE)


TRUSTED_WORKSPACES_KEY: str = "trustedWorkspaces"


def read_antigravity_settings(host: OnlineHostInterface, settings_path: Path) -> dict[str, Any]:
    """Read Antigravity's ``settings.json`` via the host filesystem.

    A missing or empty file yields an empty dict so that downstream
    provisioning can fall through into a clean write. Any other unexpected
    shape -- malformed JSON, or a valid JSON document whose top-level value
    is not an object -- raises ``UserInputError``: the file is user-tier
    state agy itself reads at every launch, and silently treating it as
    empty would let mngr overwrite content the user hand-edited (or that
    a future agy schema put there). Aligns with the
    ``check_silent_decode_error_catches`` ratchet's user-authored-config
    rule: re-raise rather than swallow.
    """
    try:
        content = host.read_text_file(settings_path)
    except FileNotFoundError:
        return {}
    if not content.strip():
        return {}
    try:
        parsed: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        raise UserInputError(
            f"Antigravity settings at {settings_path} contain malformed JSON ({exc}); "
            f"refusing to overwrite. Inspect the file by hand and either fix it or remove it, "
            f"then re-run."
        ) from exc
    if not isinstance(parsed, dict):
        raise UserInputError(
            f"Antigravity settings at {settings_path} have a non-object top-level value "
            f"({type(parsed).__name__}); refusing to overwrite. Inspect the file by hand "
            f"and either fix it or remove it, then re-run."
        )
    return parsed


@pure
def serialize_antigravity_settings(settings: Mapping[str, Any]) -> str:
    """Serialize ``settings`` in the shape Antigravity itself emits.

    Two-space-indented JSON without a trailing newline, mirroring the format
    of the file the live ``agy`` 1.0.0 writes when it updates the file. Keeps
    diffs minimal across re-provisioning runs. Also used for the mngr-owned
    per-agent files (onboarding seed, settings) so they share that formatting.
    """
    return json.dumps(dict(settings), indent=2)


@pure
def merge_trusted_workspace(settings: Mapping[str, Any], workspace_path: str) -> dict[str, Any] | None:
    """Append ``workspace_path`` to ``trustedWorkspaces``, returning ``None`` if already trusted.

    Returns ``None`` when no change is required (the workspace is already in
    the trust list); otherwise returns a fresh dict with the workspace
    appended.

    Raises ``UserInputError`` if ``trustedWorkspaces`` is present but is not a
    list. A non-list value means an unknown agy schema (or a hand edit), and
    silently coercing it to a fresh single-entry array -- as this helper used
    to do -- would destroy whatever was stored there. That is the same
    refuse-to-overwrite stance ``read_antigravity_settings`` and
    ``AntigravityAgent._check_existing_trustedworkspaces_shape`` take; keeping
    it here too means the ``@pure`` helper can't silently lose data even if a
    future caller forgets the upstream shape check.

    The array is preserved exactly as Antigravity writes it -- agy stores
    paths as strings with no further normalization, so the caller is
    responsible for passing the same canonical absolute path that ``agy``
    receives at startup. Two distinct string forms of the same logical path
    (e.g. with vs without a trailing slash) are treated as distinct entries,
    matching agy's own behavior.
    """
    existing_raw = settings.get(TRUSTED_WORKSPACES_KEY, [])
    if not isinstance(existing_raw, list):
        raise UserInputError(
            f"Antigravity settings have a non-list {TRUSTED_WORKSPACES_KEY} value "
            f"({type(existing_raw).__name__}); refusing to overwrite it. Inspect the "
            f"file by hand and either fix the value or remove the key, then re-run."
        )
    existing: list[Any] = list(existing_raw)
    if workspace_path in existing:
        return None
    merged: dict[str, Any] = dict(settings)
    merged[TRUSTED_WORKSPACES_KEY] = existing + [workspace_path]
    return merged


@pure
def build_isolated_settings(
    base_settings: Mapping[str, Any],
    settings_overrides: Mapping[str, Any],
    trusted_workspaces: Sequence[str],
) -> dict[str, Any]:
    """Build a per-agent ``settings.json`` body by layering (low -> high precedence).

    1. ``base_settings`` -- a copy of the user's real (global) ``settings.json``
       (when ``sync_home_settings``), or an empty dict otherwise. Copied, never
       mutated. This is only agy's *global* ``settings.json`` scope (in practice
       theme/telemetry/trust); the user's model, permission grants, and
       behavioral policies live in other scopes (``config/config.json``
       ``userSettings``, per-project ``config/projects/<uuid>.json``) that the
       caller does not read, so they are not inherited -- per-agent model and
       permissions come from ``settings_overrides`` (step 3).
    2. ``trusted_workspaces`` -- the agent's effective workspace path(s),
       appended (deduped) to the inherited ``trustedWorkspaces`` list, so the
       isolated agy trusts its own cwd. Pass an empty sequence to leave the
       trust list exactly as the base had it.
    3. ``settings_overrides`` -- the per-agent-type blob (``permissions``,
       ``toolPermission``, ``model``, ...), applied last so it wins.

    A non-list ``trustedWorkspaces`` in ``base_settings`` is coerced to an empty
    list (matching ``merge_trusted_workspace``); callers that read the base from
    the user's real settings validate that shape first (see the plugin's
    ``_check_existing_trustedworkspaces_shape``), so this is a defensive fallback.
    """
    settings: dict[str, Any] = dict(base_settings)
    existing_raw = settings.get(TRUSTED_WORKSPACES_KEY, [])
    existing = list(existing_raw) if isinstance(existing_raw, list) else []
    for workspace_path in trusted_workspaces:
        if workspace_path not in existing:
            existing.append(workspace_path)
    if existing or TRUSTED_WORKSPACES_KEY in settings:
        settings[TRUSTED_WORKSPACES_KEY] = existing
    settings.update(settings_overrides)
    return settings


@pure
def build_onboarding_seed() -> dict[str, Any]:
    """Build the onboarding-complete object that skips agy's first-run NUX.

    Written to ``$HOME/.gemini/antigravity-cli/cache/onboarding.json``. The NUX
    (theme + ToS/telemetry) is gated by this file's presence/content, not by any
    ``settings.json`` key (verified empirically); without it agy intercepts the
    first message with the onboarding flow.

    All three flags are ``True`` so the seed suppresses the NUX regardless of how
    the user authenticated: agy gates a separate enterprise onboarding flow on
    ``enterpriseOnboardingComplete``, so it must be set too. Marking an account
    type the user does not have complete is inert.
    """
    return {
        "consumerOnboardingComplete": True,
        "enterpriseOnboardingComplete": True,
        "onboardingComplete": True,
    }


# =============================================================================
# Hook config builder
# =============================================================================

# Top-level key for mngr's named hook group in the per-agent ``hooks.json``.
# agy keys hooks.json by hook *name* (each name maps to per-event handler
# lists); a single mngr-owned name keeps the file self-contained and easy to
# identify.
_MNGR_HOOK_NAME: str = "mngr"

# Marker file (in ``$MNGR_AGENT_STATE_DIR``) whose presence the base
# ``BaseAgent.get_lifecycle_state`` reads as "agent is actively working"
# (RUNNING); its absence means WAITING. Name kept in sync with the literal
# ``"active"`` that ``BaseAgent`` and the provider listing scripts check.
ACTIVE_MARKER_FILENAME: str = "active"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) recording every agy conversation
# ID this agent has touched -- the root agent's and its subagents' -- one per
# line, appended the first time each is seen (see ``capture_conversation_id.sh``).
# Its unique lines are the full set ``stream_transcript.sh`` tails. This is the
# transcript-scoping set only; the agent's *main* conversation for resume is
# tracked separately in ``ROOT_CONVERSATION_FILENAME`` (the conversation-ids file
# is unsuitable for resume because subagents also land in it). The capture script
# hardcodes this same literal; keep them in sync.
CONVERSATION_IDS_FILENAME: str = "antigravity_conversation_ids"

# Script (provisioned into ``$MNGR_AGENT_STATE_DIR/commands/``) that the
# ``PreInvocation`` capture hook runs to extract ``conversationId`` from agy's
# hook payload and append it to ``CONVERSATION_IDS_FILENAME``. Name kept in
# sync with the resource file under ``resources/``.
CAPTURE_CONVERSATION_ID_SCRIPT_NAME: str = "capture_conversation_id.sh"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) recording the conversation ID of
# the *root* agent for the current turn -- the conversation that opened the turn
# (fired ``PreInvocation`` while ``active`` was absent). Subagents share the same
# hooks and fire their own Stops, so the clear hook uses this to act only on the
# root's Stop. Written by ``set_active_marker.sh``; both shell scripts hardcode
# this same literal, so keep them in sync.
ROOT_CONVERSATION_FILENAME: str = "root_conversation"

# Script (provisioned into ``$MNGR_AGENT_STATE_DIR/commands/``) that the
# ``PreInvocation`` hook runs to touch the ``active`` marker and record the
# turn's root conversation (see ``ROOT_CONVERSATION_FILENAME``). Name kept in
# sync with the resource file under ``resources/``.
SET_ACTIVE_MARKER_SCRIPT_NAME: str = "set_active_marker.sh"

# Script (provisioned into ``$MNGR_AGENT_STATE_DIR/commands/``) that the
# ``Stop`` hook runs to clear the ``active`` marker -- but only on the root
# agent's fully-idle Stop (``"fullyIdle":true`` for the recorded root
# conversation). Name kept in sync with the resource file under ``resources/``.
CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME: str = "clear_active_marker_when_idle.sh"

# ``PreInvocation`` runs ``set_active_marker.sh`` (touch ``active`` + record the
# turn's root); ``Stop`` runs ``clear_active_marker_when_idle.sh`` (clear
# ``active`` only on the root's fully-idle Stop). agy runs the Stop hooks each
# time *any* conversation -- the root agent or a subagent it launched -- goes
# idle, reporting ``fullyIdle`` (an interim Stop sends ``false``, the final one
# ``true``; verified live against agy 1.0.5). Subagents fire their own
# ``fullyIdle:true`` Stop while the root still works, so the clear hook gates on
# both ``fullyIdle:true`` *and* the root conversation id, keeping the agent
# RUNNING until the root itself is done. ``$MNGR_AGENT_STATE_DIR`` expands at
# hook time. Both scripts emit no stdout (agy would treat it as injected steps /
# a stop-blocking result).
_SET_ACTIVE_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{SET_ACTIVE_MARKER_SCRIPT_NAME}"'
_CLEAR_ACTIVE_WHEN_IDLE_COMMAND: str = (
    f'bash "$MNGR_AGENT_STATE_DIR/commands/{CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME}"'
)

# Second ``PreInvocation`` handler: records the conversation ID from agy's hook
# payload (delivered on stdin). agy hands each handler its own copy of the
# payload stdin (verified live against agy 1.0.4), so this runs independently
# of the active-marker handler above. ``$MNGR_AGENT_STATE_DIR`` expands in
# agy's shell at hook-execution time.
_CAPTURE_CONVERSATION_ID_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{CAPTURE_CONVERSATION_ID_SCRIPT_NAME}"'


@pure
def build_antigravity_hooks_config() -> dict[str, Any]:
    """Build the per-agent ``hooks.json`` body for the antigravity agent.

    Emits two ``PreInvocation`` handlers plus a ``Stop`` handler:

    * The ``active``-marker pair: ``PreInvocation`` runs
      ``set_active_marker.sh`` (touch the marker, record the turn's root
      conversation) and ``Stop`` runs ``clear_active_marker_when_idle.sh``,
      which removes the marker only on the root agent's fully-idle Stop.
      ``BaseAgent.get_lifecycle_state`` reads the marker to report RUNNING vs
      WAITING; agy maintains no such marker on its own. Gating on
      ``fullyIdle`` + the root conversation keeps the agent RUNNING while work
      it launched is still in flight (including subagents, which fire their own
      ``fullyIdle:true`` Stop), instead of flipping to WAITING when the root
      turn -- or any subagent -- ends.
    * The conversation-ID capture handler: a second ``PreInvocation`` handler
      runs ``capture_conversation_id.sh``, which reads agy's hook payload from
      stdin and records every distinct conversation ID this agent touches --
      the root agent's and its subagents' -- into ``CONVERSATION_IDS_FILENAME``.
      That file is the transcript-scoping set: ``stream_transcript.sh`` tails
      each one's transcript. Resume does NOT read it (subagents land there too);
      ``assemble_command`` resumes the agent's main conversation from
      ``root_conversation`` written by ``set_active_marker.sh`` above. agy
      delivers the payload stdin to each handler independently (verified live
      against agy 1.0.4), so the two ``PreInvocation`` handlers do not contend
      for stdin.

    Auto-approval of tool permissions is NOT a hook: agy's documented
    ``PreToolUse`` ``{"decision": "allow"}`` output does not actually gate the
    ``run_command`` confirmation dialog (verified live against agy 1.0.3 -- the
    hook runs but the dialog still appears). The plugin routes permissions
    through the per-agent ``settings.json`` ``permissions`` block or the
    ``--dangerously-skip-permissions`` CLI flag instead.

    ``PreInvocation``/``Stop`` take a flat list of handlers (their matcher is
    ignored). The file is mngr-owned and rewritten from scratch each provision,
    so no merge-with-existing-content logic is needed.
    """
    mngr_hook: dict[str, Any] = {
        "PreInvocation": [
            {"type": "command", "command": _SET_ACTIVE_COMMAND},
            {"type": "command", "command": _CAPTURE_CONVERSATION_ID_COMMAND},
        ],
        "Stop": [{"type": "command", "command": _CLEAR_ACTIVE_WHEN_IDLE_COMMAND}],
    }
    return {_MNGR_HOOK_NAME: mngr_hook}


@pure
def serialize_antigravity_hooks(hooks_config: Mapping[str, Any]) -> str:
    """Serialize a ``hooks.json`` body as two-space-indented JSON.

    Matches the indentation agy itself uses for its config files; the file is
    mngr-owned so the exact formatting only matters for readable diffs.
    """
    return json.dumps(dict(hooks_config), indent=2)
