"""Seed the workspace's agent types into the laptop-side user-scope settings.toml.

mngr's project-config discovery is cwd-based: from any cwd that isn't
inside a git worktree containing `.mngr/settings.toml`, the workspace's
`[agent_types.X]` definitions are invisible. minds.app spawns `mngr forward`,
`mngr list` and `mngr message` with cwd=$HOME, so the DEFAULT_WORKSPACE_TEMPLATE
workspace's `[agent_types.X]` blocks (which live at
`/home/user/workspace/.mngr/settings.toml` inside the workspace container, with the
laptop only ever seeing them in an ephemeral temp clone during `mngr create`)
are not loaded for those laptop-side invocations.

The cwd-independent layer is user-scope settings.toml at
``<host_dir>/profiles/<profile_id>/settings.toml``. Seeding the minimum
mapping there lets every laptop-side mngr resolve the workspace's types
(`chat`, `main`, `worker`) to ClaudeAgent without affecting the system-wide
``~/.mngr/`` install used outside minds.
"""

import os
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr.config.loader import get_or_create_profile_dir

# Every agent type the workspace template declares with `parent_type = "claude"`,
# paired with what it is used for (rendered as a comment into the seeded block).
# Each one needs a laptop-side mapping because agents of that type exist on disk
# (their `data.json` records the bare type name) and every laptop-side mngr
# command loads them.
_CLAUDE_PARENTED_WORKSPACE_TYPES: Final[tuple[tuple[str, str], ...]] = (
    (
        "chat",
        "every user-facing chat agent -- the initial chat the bootstrap creates, each "
        "'New Agent' chat, and each /assist chat (all created with `--template chat`)",
    ),
    ("main", "the hidden services agent, whose window 0 runs supervisord and the background services"),
    ("worker", "the agents a chat agent spawns for delegated tasks"),
)

# When this file is seeded inside a pytest run, mngr's config loader
# refuses to read it unless this key is set, by design (configs in
# pytest are explicit opt-in to keep prod state out of test runs).
_PYTEST_OPT_IN_LINE = "is_allowed_in_pytest = true\n"

_SEED_HEADER = """
# Seeded by minds.app at startup so laptop-side mngr (cwd=$HOME) can resolve the
# DEFAULT_WORKSPACE_TEMPLATE workspace's own agent types without needing to load
# the workspace's `.mngr/settings.toml` (which lives inside the workspace container
# at /home/user/workspace/.mngr/ and on the laptop only in ephemeral mngr-create
# temp clones). Without this, `mngr forward`, `mngr list` and `mngr message` fall
# back to BaseAgent for agents whose on-disk data.json records one of these types,
# which (a) shows them in mngr list as RUNNING_UNKNOWN_AGENT_TYPE and (b) breaks
# `mngr message`: BaseAgent has no send_message at all, so the latchkey
# permission-approval nudge fails instead of routing through the InteractiveTuiAgent
# paste-and-submit pipeline Claude's TUI needs.
# The workspace's full override list (sync_*, command, settings_overrides, etc.) is
# only honored at agent-creation time and inside the workspace; the laptop only
# needs the parent-type mapping for resolve_agent_type to succeed.
"""


def _get_section_header(type_name: str) -> str:
    """Return the TOML section header for an agent type, e.g. ``[agent_types.chat]``."""
    return f"[agent_types.{type_name}]"


def _render_seed_block(type_name: str, purpose: str) -> str:
    """Render the seeded block for one agent type (a comment plus the parent-type mapping)."""
    return f'\n# `{type_name}` is {purpose}.\n{_get_section_header(type_name)}\nparent_type = "claude"\n'


def _with_pytest_opt_in(existing: str) -> str:
    """Return ``existing`` with the pytest opt-in key prepended when it is needed.

    Prepended rather than appended because a bare TOML key placed after a
    section header would be parsed as a member of that section: the file being
    seeded may already contain `[agent_types.*]` / `[providers.*]` blocks.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ or _PYTEST_OPT_IN_LINE.strip() in existing:
        return existing
    return _PYTEST_OPT_IN_LINE + existing


def seed_laptop_agent_types_for_minds(host_dir: Path) -> None:
    """Idempotent. Appends a `[agent_types.X]` block for every workspace type
    missing from the user-scope settings.toml under ``host_dir``.

    Safe to call on every minds startup -- a literal substring check for each
    section header avoids re-appending on subsequent launches and is robust
    against the TOML being hand-edited (we only care that *some*
    `[agent_types.X]` exists, regardless of which fields it sets). Types are
    checked individually, so a settings.toml seeded by an older minds build
    (which only knew about `main`) gains the newer types on the next launch.
    """
    profile_dir = get_or_create_profile_dir(host_dir)
    settings_path = profile_dir / "settings.toml"
    existing = settings_path.read_text() if settings_path.exists() else ""
    missing_types = tuple(
        (type_name, purpose)
        for type_name, purpose in _CLAUDE_PARENTED_WORKSPACE_TYPES
        if _get_section_header(type_name) not in existing
    )
    if not missing_types:
        return
    seed_blocks = _SEED_HEADER + "".join(
        _render_seed_block(type_name, purpose) for type_name, purpose in missing_types
    )
    settings_path.write_text(_with_pytest_opt_in(existing) + seed_blocks)
    logger.info(
        "seeded {} into {}",
        ", ".join(_get_section_header(type_name) for type_name, _purpose in missing_types),
        settings_path,
    )
