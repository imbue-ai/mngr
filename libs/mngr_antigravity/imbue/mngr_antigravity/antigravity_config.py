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
* The lifecycle ``statusLine`` settings block
  (``build_antigravity_statusline_settings``): agy invokes a configured
  ``statusLine`` command on every agent-state change (JSON on stdin), and
  ``statusline.sh`` uses that as the single source of truth for the agent's
  lifecycle -- it maintains the ``active`` marker (RUNNING vs WAITING), records
  the root conversation, and fires the tmux message-submission signal. This is
  mngr-owned and applied last (winning over ``settings_overrides``).
* The transcript-scoping ``PreInvocation`` hook (``build_antigravity_hooks_config``),
  written to the per-agent ``$HOME/.gemini/config/hooks.json`` (agy executes
  hooks from there with no trust prompt and no ``--add-dir``). It only captures
  conversation ids (including subagents', which ``statusLine`` does not surface).

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
from imbue.mngr.config.external_settings import apply_settings_patch
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface

# agy's config tree lives under ``$HOME/.gemini``. These segments name the
# files within it that mngr reads (the user's real home) or writes (a per-agent
# home). All are addressed relative to a ``$HOME`` root so the same builders
# serve both roles.
_GEMINI_DIR_NAME: str = ".gemini"
_ANTIGRAVITY_CLI_DIR_NAME: str = "antigravity-cli"
_SETTINGS_FILENAME: str = "settings.json"

# agy's native resumable conversation store: per-conversation SQLite files at
# ``antigravity-cli/conversations/<conv_id>.db`` (the dir ``agy --conversation
# <id>`` resumes from). Preserved on destroy so the agent can be resumed/adopted.
_CONVERSATIONS_DIR_NAME: str = "conversations"

# The conversation-store dir as a POSIX path relative to the per-agent ``$HOME`` root
# (i.e. under ``$HOME/.gemini/antigravity-cli/``). Public so the plugin can join it with
# the home's state-dir-relative path to form the preserved-item rel_path without importing
# the private dir-name constants above.
CONVERSATIONS_DIR_RELATIVE_TO_HOME: str = "/".join(
    (_GEMINI_DIR_NAME, _ANTIGRAVITY_CLI_DIR_NAME, _CONVERSATIONS_DIR_NAME)
)

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


def get_antigravity_conversations_dir(home: Path) -> Path:
    """Return ``<home>/.gemini/antigravity-cli/conversations`` (agy's resumable store dir)."""
    return get_antigravity_cli_dir(home) / _CONVERSATIONS_DIR_NAME


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

    The array is preserved exactly as Antigravity writes it -- agy stores
    paths as strings with no further normalization, so the caller is
    responsible for passing the same canonical absolute path that ``agy``
    receives at startup. Two distinct string forms of the same logical path
    (e.g. with vs without a trailing slash) are treated as distinct entries,
    matching agy's own behavior.
    """
    existing_raw = settings.get(TRUSTED_WORKSPACES_KEY, [])
    if isinstance(existing_raw, list):
        existing: list[Any] = list(existing_raw)
    else:
        existing = []
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
    *,
    allow_narrowing: bool = False,
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
       ``toolPermission``, ``model``, ...), folded last (so it wins) via the
       shared ``apply_settings_patch``: a bare key assigns with the narrowing
       guard, and a ``__mngr_merge`` directive (desugared at config-load) merges
       onto the base instead -- the same Claude-compatible operator surface
       ``mngr_claude`` uses. Set ``allow_narrowing`` to opt into assign-by-default.

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
    return apply_settings_patch(
        settings,
        settings_overrides,
        allow_narrowing=allow_narrowing,
        base_description="the per-agent antigravity settings base (synced home settings + workspace trust)",
    )


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
# the *root* agent. agy's ``statusLine`` payload always carries the root
# ``conversation_id`` (never a subagent's, even while a subagent runs), so
# ``statusline.sh`` writes it here on every state change. The only consumer is
# the resume prelude in ``assemble_command``. The shell script hardcodes this
# same literal, so keep them in sync.
ROOT_CONVERSATION_FILENAME: str = "root_conversation"

# Script (provisioned into ``$MNGR_AGENT_STATE_DIR/commands/``) that agy runs as
# its configured ``statusLine`` command on every agent-state change. It owns the
# whole lifecycle: it maintains the ``active`` marker (RUNNING vs WAITING from
# ``agent_state``), records the root ``conversation_id``, and fires the tmux
# submission signal ``mngr message`` waits on. Name kept in sync with the
# resource file under ``resources/``.
STATUSLINE_SCRIPT_NAME: str = "statusline.sh"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) holding the user's own statusLine
# command, when they configured one. agy allows only one statusLine command (which
# must be mngr's, for lifecycle correctness), so to preserve a user's custom
# rendering ``statusline.sh`` runs this command -- with the same payload on stdin
# -- and emits only its output as the status row. Written by the provisioner (see
# ``plugin._provision_agy_home``); the shell script hardcodes this same literal,
# so keep them in sync.
USER_STATUSLINE_COMMAND_FILENAME: str = "user_statusline_command"

# The ``statusLine`` command agy invokes (with the JSON payload on stdin) on
# every agent-state change. ``$MNGR_AGENT_STATE_DIR`` expands in agy's shell at
# invocation time. Unlike the hook scripts, this command's stdout IS rendered (it
# is the status row); mngr's use is lifecycle-only, so the script prints nothing of
# its own (agy shows working/idle itself) and emits only a composed user statusLine.
_STATUSLINE_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{STATUSLINE_SCRIPT_NAME}"'

# The lone ``PreInvocation`` handler: records the conversation ID from agy's hook
# payload (delivered on stdin) into ``CONVERSATION_IDS_FILENAME`` for transcript
# scoping. ``statusLine`` (not a hook) drives lifecycle; this hook only needs to
# capture subagent ids, which the statusLine payload does not surface (it always
# reports the root). ``$MNGR_AGENT_STATE_DIR`` expands in agy's shell at
# hook-execution time.
_CAPTURE_CONVERSATION_ID_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{CAPTURE_CONVERSATION_ID_SCRIPT_NAME}"'


@pure
def build_antigravity_statusline_settings() -> dict[str, Any]:
    """Build the ``statusLine`` settings block for the per-agent ``settings.json``.

    agy invokes this command on every agent-state change, piping a JSON payload
    (``agent_state``, ``conversation_id``, ``model``, ...) on stdin and rendering
    the command's stdout in the prompt's status row. ``statusline.sh`` uses that
    signal as the single source of truth for the agent's lifecycle: it maintains
    the ``active`` marker, records the root conversation, and fires the tmux
    submission signal (see the resource script and ``STATUSLINE_SCRIPT_NAME``).

    This block is mngr-owned and must be applied *after* (winning over) the
    user's ``settings_overrides``: lifecycle correctness depends on it, so the agy
    ``statusLine`` must be mngr's. mngr's use is lifecycle-only -- it prints
    nothing of its own (agy shows working/idle itself), so the status row looks as
    it would without mngr. A user's own ``statusLine`` is not discarded but
    *composed* -- the provisioner records its command (see
    ``extract_statusline_command`` and ``USER_STATUSLINE_COMMAND_FILENAME``) and
    ``statusline.sh`` runs it, emitting only its output as the status row.
    """
    return {"statusLine": {"type": "command", "command": _STATUSLINE_COMMAND}}


@pure
def extract_statusline_command(statusline: Any) -> str | None:
    """Return the runnable shell command from an agy ``statusLine`` block, or ``None``.

    agy ``statusLine`` blocks are ``{"type": "command", "command": "<shell>"}``.
    Only that shape can be composed (run by ``statusline.sh``); anything else -- a
    non-dict, a non-``"command"`` ``type``, or a missing/blank ``command`` -- is
    not runnable and returns ``None`` (the provisioner then warns and drops it).
    """
    if not isinstance(statusline, Mapping):
        return None
    if statusline.get("type") != "command":
        return None
    command = statusline.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None


@pure
def build_antigravity_hooks_config() -> dict[str, Any]:
    """Build the per-agent ``hooks.json`` body for the antigravity agent.

    Emits a single ``PreInvocation`` handler running
    ``capture_conversation_id.sh``, which reads agy's hook payload from stdin and
    records every distinct conversation ID this agent touches -- the root agent's
    and its subagents' -- into ``CONVERSATION_IDS_FILENAME``. That file is the
    transcript-scoping set: ``stream_transcript.sh`` tails each one's transcript.
    Subagent ids are needed here precisely because the ``statusLine`` payload
    only ever reports the root conversation (so the hook is the only place
    subagent ids surface).

    The agent's RUNNING/WAITING lifecycle and message-submission signal are NOT
    hooks: they are driven by the mngr-owned ``statusLine`` command
    (``statusline.sh``; see ``build_antigravity_statusline_settings``), which agy
    invokes on every agent-state change. agy's top-level ``agent_state`` already
    aggregates subagent activity (it stays ``working`` continuously while a
    subagent runs and returns to ``idle`` only when root + subagents are all
    done; verified live against agy 1.0.6/1.0.7), so a single ``agent_state``
    check captures the whole-turn busy/idle invariant without per-conversation
    bookkeeping.

    Auto-approval of tool permissions is NOT a hook either: agy's documented
    ``PreToolUse`` ``{"decision": "allow"}`` output does not actually gate the
    ``run_command`` confirmation dialog (verified live against agy 1.0.3 -- the
    hook runs but the dialog still appears). The plugin routes permissions
    through the per-agent ``settings.json`` ``permissions`` block or the
    ``--dangerously-skip-permissions`` CLI flag instead.

    ``PreInvocation`` takes a flat list of handlers (its matcher is ignored). The
    file is mngr-owned and rewritten from scratch each provision, so no
    merge-with-existing-content logic is needed.
    """
    mngr_hook: dict[str, Any] = {
        "PreInvocation": [
            {"type": "command", "command": _CAPTURE_CONVERSATION_ID_COMMAND},
        ],
    }
    return {_MNGR_HOOK_NAME: mngr_hook}


@pure
def serialize_antigravity_hooks(hooks_config: Mapping[str, Any]) -> str:
    """Serialize a ``hooks.json`` body as two-space-indented JSON.

    Matches the indentation agy itself uses for its config files; the file is
    mngr-owned so the exact formatting only matters for readable diffs.
    """
    return json.dumps(dict(hooks_config), indent=2)
