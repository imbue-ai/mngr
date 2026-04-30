"""Shared logic for wrapping user-installed Stop / SubagentStop hooks
with the ``MNGR_SUBAGENT_PROXY_CHILD`` env-conditional guard.

Lives in its own module (not ``plugin.py``) so the SessionStart hook
in ``hooks/reap.py`` can apply the same wrap to per-agent plugin caches
that Claude Code populates AFTER provisioning. plugin.py-time wraps
miss those entirely because the cache is fetched fresh from GitHub
when claude starts, not copied from the user marketplace.

Pure dict / file manipulation here -- no host abstraction, no pluggy
imports -- so reap.py can import this without inducing a circular
dependency back to plugin.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

PROXY_CHILD_GUARD_PREFIX: Final[str] = '[ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0; '

# Substrings that mark a hook command as mngr-managed (readiness, credential
# sync, subagent-proxy, wait pipeline). Anything whose command doesn't contain
# one of these is treated as a user-configured hook -- a regular hook whose
# top-level-vs-subagent semantics we don't know how to reason about, but which
# we wrap with the env-conditional guard so it no-ops inside spawned subagents.
MNGR_MANAGED_HOOK_MARKERS: Final[tuple[str, ...]] = (
    "$MNGR_AGENT_STATE_DIR",
    "MAIN_CLAUDE_SESSION_ID",
    "imbue.mngr_subagent_proxy.hooks.",
    "sync_keychain_credentials.py",
    "wait_for_stop_hook.sh",
    # Auto-allow PermissionRequest emitter installed by
    # _install_proxy_child_auto_allow. Without this marker the strip
    # pass would remove it on re-provisioning.
    '"hookEventName":"PermissionRequest"',
)


def wrap_with_proxy_child_guard(command: str) -> str:
    """Prepend the guard so the command no-ops inside spawned subagents.

    The wait-script sets MNGR_SUBAGENT_PROXY_CHILD=1 in the spawned
    subagent's env. Wrapping every non-mngr Stop/SubagentStop command
    with this guard means: in the parent (env unset) the command runs
    normally; in a spawned subagent (env set) it exits 0 immediately.
    Already-wrapped commands are left alone (idempotent).
    """
    if PROXY_CHILD_GUARD_PREFIX in command:
        return command
    return PROXY_CHILD_GUARD_PREFIX + command


def iter_user_stop_hook_commands(
    hooks: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str]]:
    """Yield (event_name, cmd_entry, command) for each well-formed
    Stop/SubagentStop command in a hooks dict.

    Skips entries whose shape doesn't match Claude Code's hooks schema
    (non-list ``entries``, non-dict ``entry``/``cmd_entry``, missing or
    non-str ``command``).
    """
    for event_name in ("Stop", "SubagentStop"):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                continue
            for cmd_entry in inner:
                if not isinstance(cmd_entry, dict):
                    continue
                command = cmd_entry.get("command")
                if not isinstance(command, str) or not command:
                    continue
                yield event_name, cmd_entry, command


def guard_user_stop_hooks_against_proxy_children(settings: dict[str, Any]) -> bool:
    """Wrap each non-mngr Stop/SubagentStop command with the proxy-child guard.

    Returns True if any command was modified. Idempotent: re-running on
    already-wrapped commands is a no-op. Mngr-managed commands (recognized
    via MNGR_MANAGED_HOOK_MARKERS) are left alone -- they already handle
    scope appropriately via _SESSION_GUARD or are mngr-internal.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for _event_name, cmd_entry, command in iter_user_stop_hook_commands(hooks):
        # Don't double-guard mngr-managed hooks.
        if any(marker in command for marker in MNGR_MANAGED_HOOK_MARKERS):
            continue
        wrapped = wrap_with_proxy_child_guard(command)
        if wrapped != command:
            cmd_entry["command"] = wrapped
            changed = True
    return changed


def guard_stop_hooks_in_file(path: Path) -> None:
    """Apply the proxy-child guard to every Stop/SubagentStop command in a JSON hooks file.

    In-process / local-filesystem version (no host abstraction). Reads
    the file, walks its ``hooks`` dict, wraps each non-mngr Stop /
    SubagentStop command, writes the file back. No-op if the file is
    missing, malformed, or already fully guarded.
    """
    try:
        content = path.read_text()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("Could not read {} for Stop-hook guard pass: {}", path, e)
        return
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Could not parse {}; skipping Stop-hook guard pass", path)
        return
    if not isinstance(data, dict):
        return
    if not guard_user_stop_hooks_against_proxy_children(data):
        return
    logger.info("Wrapped Stop hooks in {} with MNGR_SUBAGENT_PROXY_CHILD guard", path)
    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        logger.warning("Could not write {} after Stop-hook guard pass: {}", path, e)


def guard_per_agent_plugin_cache(state_dir: Path) -> None:
    """Walk the per-agent Claude Code plugin cache and guard every hooks.json.

    Claude Code populates this cache at session start by fetching from
    its configured marketplaces (typically GitHub) -- AFTER mngr's
    provisioning hook runs. The provisioning-time walk over the user
    marketplace dir misses these freshly-fetched files. Calling this
    helper from a SessionStart hook applies the guard at the right
    moment.

    Idempotent: ``wrap_with_proxy_child_guard`` no-ops on
    already-wrapped commands.
    """
    cache_root = state_dir / "plugin" / "claude" / "anthropic" / "plugins"
    if not cache_root.is_dir():
        return
    try:
        candidates = list(cache_root.rglob("hooks/hooks.json"))
    except OSError as e:
        logger.warning("Could not enumerate per-agent plugin cache hooks under {}: {}", cache_root, e)
        return
    for path in candidates:
        guard_stop_hooks_in_file(path)
