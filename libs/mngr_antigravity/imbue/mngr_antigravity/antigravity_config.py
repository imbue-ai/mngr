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
) -> dict[str, Any]:
    """Build a per-agent ``settings.json`` body by layering (low -> high precedence).

    1. ``base_settings`` -- a copy of the user's real ``settings.json`` (when
       ``sync_home_settings``) so the agent inherits the user's preferences, or
       an empty dict otherwise. Copied, never mutated.
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
    """
    return {
        "consumerOnboardingComplete": True,
        "enterpriseOnboardingComplete": False,
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

# ``active`` is touched on every ``PreInvocation`` (the loop is about to call
# the model, i.e. the agent is working) and removed on ``Stop`` (the execution
# loop terminated and the agent is back to waiting for input). ``$MNGR_AGENT_STATE_DIR``
# expands in agy's shell at hook-execution time. Both commands intentionally
# emit no stdout: ``PreInvocation``/``Stop`` treat empty output as "no
# injected steps" / "allow stop" (verified live against agy 1.0.3).
_SET_ACTIVE_COMMAND: str = f'touch "$MNGR_AGENT_STATE_DIR/{ACTIVE_MARKER_FILENAME}"'
_CLEAR_ACTIVE_COMMAND: str = f'rm -f "$MNGR_AGENT_STATE_DIR/{ACTIVE_MARKER_FILENAME}"'


@pure
def build_antigravity_hooks_config() -> dict[str, Any]:
    """Build the per-agent ``hooks.json`` body for the antigravity agent.

    Emits the ``active``-marker hooks: ``PreInvocation`` touches the marker
    and ``Stop`` removes it. ``BaseAgent.get_lifecycle_state`` reads that
    marker to report RUNNING while the agent works and WAITING when it's idle;
    agy maintains no such marker on its own.

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
        "PreInvocation": [{"type": "command", "command": _SET_ACTIVE_COMMAND}],
        "Stop": [{"type": "command", "command": _CLEAR_ACTIVE_COMMAND}],
    }
    return {_MNGR_HOOK_NAME: mngr_hook}


@pure
def serialize_antigravity_hooks(hooks_config: Mapping[str, Any]) -> str:
    """Serialize a ``hooks.json`` body as two-space-indented JSON.

    Matches the indentation agy itself uses for its config files; the file is
    mngr-owned so the exact formatting only matters for readable diffs.
    """
    return json.dumps(dict(hooks_config), indent=2)
