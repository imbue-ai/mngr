"""Seed `[agent_types.main]` into the laptop-side user-scope settings.toml.

mngr's project-config discovery is cwd-based: from any cwd that isn't
inside a git worktree containing `.mngr/settings.toml`, the workspace's
`[agent_types.X]` definitions are invisible. minds.app spawns `mngr forward`
and `mngr list` with cwd=$HOME, so the FCT workspace's `[agent_types.main]`
(which lives at `/code/.mngr/settings.toml` inside the lima VM, with the
laptop only ever seeing it in an ephemeral temp clone during `mngr create`)
is not loaded for those laptop-side invocations.

The cwd-independent layer is user-scope settings.toml at
``<host_dir>/profiles/<profile_id>/settings.toml``. Seeding the minimum
mapping there lets every laptop-side mngr resolve `type=main` -> ClaudeAgent
without affecting the system-wide ``~/.mngr/`` install used outside minds.
"""

from pathlib import Path

from loguru import logger

from imbue.mngr.config.loader import get_or_create_profile_dir

_AGENT_TYPES_MAIN_MARKER = "[agent_types.main]"

_SEED_BLOCK = """
# Seeded by minds.app at startup so laptop-side mngr (cwd=$HOME) can
# resolve the FCT workspace's `main` type without needing to load the
# workspace's own `.mngr/settings.toml` (which lives inside the lima VM
# at /code/.mngr/ and on the laptop only in ephemeral mngr-create
# temp clones). Without this, `mngr forward` and `mngr list` fall
# back to BaseAgent for agents whose on-disk data.json records
# `type = "main"`, which (a) shows them in mngr list as
# RUNNING_UNKNOWN_AGENT_TYPE and (b) makes `mngr message` route via
# BaseAgent.send_message (literal text + Enter) instead of the
# InteractiveTuiAgent paste-and-submit pipeline Claude's TUI needs.
# The workspace's full override list (sync_*, command, etc.) is only
# honored at agent-creation time and inside the VM; the laptop only
# needs the parent-type mapping for resolve_agent_type to succeed.
[agent_types.main]
parent_type = "claude"
"""


def seed_laptop_agent_types_for_minds(host_dir: Path) -> None:
    """Idempotent. Appends `[agent_types.main]` to the user-scope settings.toml
    under ``host_dir`` if the section is not already present.

    Safe to call on every minds startup -- a literal substring check for
    the section header avoids re-appending on subsequent launches and is
    robust against the TOML being hand-edited (we only care that *some*
    `[agent_types.main]` exists, regardless of which fields it sets).
    """
    profile_dir = get_or_create_profile_dir(host_dir)
    settings_path = profile_dir / "settings.toml"
    existing = settings_path.read_text() if settings_path.exists() else ""
    if _AGENT_TYPES_MAIN_MARKER in existing:
        return
    settings_path.write_text(existing + _SEED_BLOCK)
    logger.info("seeded [agent_types.main] into {}", settings_path)
