"""Read/write helpers for Antigravity CLI's user-tier ``settings.json``.

Antigravity reads its CLI-mode settings from ``~/.gemini/antigravity-cli/settings.json``.
The ``trustedWorkspaces`` array is the agy analog of Gemini CLI's
``trustedFolders.json``: each absolute workspace path the user has accepted via
the "Do you trust the contents of this project?" dialog gets appended to the
array. On subsequent launches, agy reads the array and suppresses the dialog
for any matching path.

Antigravity does **not** expose an env-var override for this file
(no ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` analog exists in the v1.0.0 binary), so
mngr cannot redirect the file to a per-agent path. We therefore *merge* into
the user's global file -- appending the agent's ``work_dir`` to the
``trustedWorkspaces`` array if it isn't already present, leaving every other
key untouched. This is additive and idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OnlineHostInterface


def get_antigravity_user_settings_path() -> Path:
    """Return the user-tier ``settings.json`` path for the Antigravity CLI."""
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


TRUSTED_WORKSPACES_KEY: str = "trustedWorkspaces"


def read_antigravity_settings(host: OnlineHostInterface, settings_path: Path) -> dict[str, Any]:
    """Read Antigravity's ``settings.json`` via the host filesystem.

    A missing file, a malformed top-level JSON document, or a valid JSON
    document whose top-level value is not an object all yield an empty dict
    so that downstream provisioning can fall through into a clean write.
    The non-object case is logged at warning level (the same level used for
    malformed JSON) so that overwriting the file is not silent. Any other
    read failure (permission denied, IO error) is allowed to propagate.
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
        logger.warning("Malformed JSON in Antigravity settings at {}: {}. Treating as empty.", settings_path, exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "Antigravity settings at {} has a non-object top-level value ({}). Treating as empty.",
            settings_path,
            type(parsed).__name__,
        )
        return {}
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
