"""Read/write helpers for Antigravity CLI's user-tier ``settings.json``.

Antigravity reads its CLI-mode settings from ``~/.gemini/antigravity-cli/settings.json``.
The ``trustedWorkspaces`` array is the agy analog of Gemini CLI's
``trustedFolders.json``: each absolute workspace path the user has accepted via
the "Do you trust the contents of this project?" dialog gets appended to the
array. On subsequent launches, agy reads the array and suppresses the dialog
for any matching path.

Antigravity does **not** expose an env-var override for this file
(no ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` analog exists in the binary), so
mngr cannot redirect the file to a per-agent path. We therefore *merge* into
the user's global file -- appending the agent's ``work_dir`` to the
``trustedWorkspaces`` array if it isn't already present, leaving every other
key untouched. This is additive and idempotent.

Hooks, by contrast, ARE provisioned per-agent. agy discovers a ``hooks.json``
from any workspace directory's ``.agents/`` subdir (and from the global
``~/.gemini/config/hooks.json``) and executes the hooks it finds. mngr writes a
per-agent ``hooks.json`` into the agent state dir and points agy at it with
``--add-dir`` (see ``build_antigravity_hooks_config`` and the plugin's
``assemble_command``), so the user's global config stays untouched and each
agent's marker files land in its own state dir.

The in-TUI ``/hooks`` command instead writes to
``~/.gemini/antigravity-cli/hooks.json``, which the execution engine does not
run -- that path is loaded only for the TUI's display, while hooks execute only
from ``~/.gemini/config/hooks.json`` and per-workspace ``.agents/hooks.json``
(google-antigravity/antigravity-cli#49). mngr therefore does not use the TUI.

The hooks here only maintain the lifecycle ``active`` marker. Permission
auto-approval is NOT done via a hook -- agy's documented ``PreToolUse``
``{"decision": "allow"}`` output does not gate the ``run_command`` confirmation
dialog -- so the plugin uses the ``--dangerously-skip-permissions`` CLI flag
instead (see the plugin's ``assemble_command``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface


def get_antigravity_user_settings_path() -> Path:
    """Return the user-tier ``settings.json`` path for the Antigravity CLI."""
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


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
    diffs minimal across re-provisioning runs.
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
    receives at startup (typically the agent's ``work_dir``). Two distinct
    string forms of the same logical path (e.g. with vs without a trailing
    slash) are treated as distinct entries, matching agy's own behavior.
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
    hook runs but the dialog still appears). The plugin routes
    ``auto_allow_permissions`` through the ``--dangerously-skip-permissions``
    CLI flag instead (see ``assemble_command``).

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
