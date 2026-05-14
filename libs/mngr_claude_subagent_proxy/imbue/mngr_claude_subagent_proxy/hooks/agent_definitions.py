"""Resolve a Claude Code ``subagent_type`` to its agent-definition file.

This module's job: given a parent's ``Task(subagent_type=X, prompt=Y)``
call, locate the ``.md`` file Claude Code would have loaded as X's
definition and return its post-frontmatter body. The plugin uses the
body either as user-message text prepended to the proxy prompt file
(PROXY mode) or as a path pointer in the deny reason (DENY mode),
preserving the typed-subagent contract the parent expected.

Built-in agent types -- ``general-purpose``, ``Explore``, etc. -- are
baked into Claude Code itself and have no on-disk definition. The
resolver returns ``None`` for those (and any other unknown name); the
caller falls back to the prompt-only spawn path.

## Claude Code on-disk layout contract

This module depends on Claude Code's documented on-disk layout for
agent definitions (verified against Claude Code as of 2026-05). If the
layout changes, this module needs a matching update.

Discovery branches on whether ``subagent_type`` is plugin-namespaced
(contains ``:``):

- Non-namespaced (e.g. ``code-reviewer``): walk in precedence order,
  closest wins, stop at the first hit:

  1. ``<work_dir>/.claude/agents/<name>.md`` -- project-local.
  2. ``<user-claude-config-dir>/agents/<name>.md`` -- user-level.

- Plugin-namespaced (``plugin:agent``, e.g.
  ``imbue-code-guardian:verify-and-fix``): only the marketplaces root
  is checked --
  ``<user-claude-config-dir>/plugins/marketplaces/*/plugins/<plugin>/agents/<agent>.md``
  (marketplaces enumerated in sorted order; first hit wins). The flat
  ``agents/`` dir is NOT a fallback for namespaced types -- a same-named
  flat file under a different plugin namespace would be a silent
  collision, so we refuse to cross the boundary.

Frontmatter parsing is delegated to ``python-frontmatter`` -- the
delimiter is the standard ``---`` YAML block at the top of the file.
A file with no frontmatter returns its full content as the body.

This contract is **not** a Claude Code public API; it's the directory
layout the Claude Code CLI uses for agent discovery as observed and
documented at https://code.claude.com/docs/en/skills (and the
sub-agents page). If Claude Code grows a CLI command like
``claude --resolve-agent X`` or enriches PreToolUse:Agent hook input
with the resolved definition, this whole module collapses into a
subprocess call -- see "Honor agent-definition tool restrictions and
system-prompt semantics" in the plugin README for the v2 followup.

## Limitations

Tool restrictions declared in the frontmatter (``tools: [Read, Grep]``)
are NOT honored in v1: a plain mngr Claude subagent inherits the user's
full Claude config. The skill + ``SubagentProxyMode.DENY`` docstring
flag this limitation. A future branch can add ``--type`` variants with
restricted permissions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import frontmatter
import yaml
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_claude.claude_config import get_user_claude_config_dir


class AgentDefinition(FrozenModel):
    """An on-disk Claude Code agent definition resolved from a ``subagent_type``."""

    path: Path = Field(description="Absolute path to the resolved .md file.")
    body: str = Field(description="The Markdown body (post-frontmatter) -- the system prompt Claude Code would use.")


def _user_claude_agents_dir() -> Path:
    """Return ``<user-claude-config-dir>/agents/`` for the current user.

    Resolved at call time so tests that monkeypatch ``$HOME`` (or
    ``$ORIGINAL_CLAUDE_CONFIG_DIR`` / ``$CLAUDE_CONFIG_DIR``) see the
    override instead of a value snapshotted at import. Goes through
    ``get_user_claude_config_dir()`` so per-agent isolated config dirs
    don't accidentally shadow the user's real ``~/.claude/agents/``.
    """
    return get_user_claude_config_dir() / "agents"


def _user_claude_marketplaces_dir() -> Path:
    """Return ``<user-claude-config-dir>/plugins/marketplaces/`` for the current user.

    Same call-time resolution as ``_user_claude_agents_dir``. Mirrors
    the per-agent plugin cache shape that
    ``_stop_hook_guard.guard_per_agent_plugin_cache`` walks under
    ``<state_dir>/plugin/claude/anthropic/plugins/`` (Claude Code uses
    the same ``<marketplace>/plugins/<plugin>/`` directory layout in both
    places).
    """
    return get_user_claude_config_dir() / "plugins" / "marketplaces"


def _is_safe_subagent_type_segment(segment: str) -> bool:
    """Return True if ``segment`` is safe to interpolate into a candidate path.

    Rejects path-meaningful characters so a ``subagent_type`` value like
    ``"../../etc/passwd"`` or ``"plugin:../x"`` cannot traverse out of
    the ``.claude/agents/`` directory and read an unrelated ``.md``
    file. The ``subagent_type`` field originates from Claude's Task
    tool call (LLM output), so we treat it as untrusted-by-design even
    though it is not externally attacker-controlled.

    Real Claude Code agent names are restricted to a-z, 0-9, and ``-``
    per the docs, so this conservative rejection cannot false-positive
    on any legitimate input. Code must work on both macOS and Linux
    per CLAUDE.md, so we disallow ``/``, ``\\``, and ``\\x00`` explicitly
    (these cover the path-separator characters on both platforms).
    """
    if not segment:
        return False
    if segment in (".", ".."):
        return False
    return not any(bad in segment for bad in ("/", "\\", "\x00"))


def _iter_candidate_paths(subagent_type: str, work_dir: Path) -> Iterator[Path]:
    """Yield candidate ``.md`` paths for ``subagent_type`` in precedence order.

    Plugin-namespaced types (``plugin:agent``) are only resolved via the
    user's installed marketplaces; non-namespaced types are only
    resolved via project-local then user-level ``.claude/agents/``.
    Mixing the two would silently let a flat user file shadow a
    marketplace-installed agent of the same agent-name, which would
    be surprising.

    Each segment of ``subagent_type`` (the whole string for
    non-namespaced, or ``plugin_name`` / ``agent_name`` for namespaced)
    is validated by ``_is_safe_subagent_type_segment`` before being
    interpolated into a path. Unsafe segments (path separators,
    traversal tokens, empty strings) cause the iterator to yield
    nothing -- the caller falls through to the prompt-only / unresolved
    path, same as for built-in types.
    """
    if ":" in subagent_type:
        plugin_name, _, agent_name = subagent_type.partition(":")
        if not _is_safe_subagent_type_segment(plugin_name) or not _is_safe_subagent_type_segment(agent_name):
            logger.warning(
                "agent_definitions: rejecting unsafe namespaced subagent_type {!r}",
                subagent_type,
            )
            return
        marketplaces_root = _user_claude_marketplaces_dir()
        if not marketplaces_root.is_dir():
            return
        try:
            marketplace_dirs = sorted(marketplaces_root.iterdir())
        except OSError as e:
            logger.warning(
                "agent_definitions: failed to enumerate marketplaces under {}: {}",
                marketplaces_root,
                e,
            )
            return
        for marketplace_dir in marketplace_dirs:
            yield marketplace_dir / "plugins" / plugin_name / "agents" / f"{agent_name}.md"
        return
    if not _is_safe_subagent_type_segment(subagent_type):
        logger.warning(
            "agent_definitions: rejecting unsafe non-namespaced subagent_type {!r}",
            subagent_type,
        )
        return
    yield work_dir / ".claude" / "agents" / f"{subagent_type}.md"
    yield _user_claude_agents_dir() / f"{subagent_type}.md"


def resolve_agent_definition(subagent_type: str, work_dir: Path) -> AgentDefinition | None:
    """Resolve ``subagent_type`` to the agent definition Claude Code would load.

    Returns ``None`` for:
    - empty ``subagent_type``,
    - built-in types (``general-purpose``, ``Explore``, ...) with no on-disk file,
    - any plugin-namespaced type whose marketplace path doesn't exist,
    - any ``subagent_type`` with unsafe characters (path separators,
      traversal tokens, or empty segments after splitting on ``:``);
      logged as a warning,
    - any file the resolver can't read (logged as a warning).

    Returns an ``AgentDefinition`` (with the post-frontmatter body) for
    the first candidate path that exists on disk and is readable.
    """
    if not subagent_type:
        return None
    for candidate in _iter_candidate_paths(subagent_type, work_dir):
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text()
        except OSError as e:
            logger.warning("agent_definitions: failed to read {}: {}", candidate, e)
            continue
        # python-frontmatter's YAML handler raises yaml.YAMLError on
        # malformed frontmatter (e.g. typo in an installed marketplace
        # agent's --- block). Without this guard the exception would
        # propagate out of the PreToolUse hook's run(), crashing the
        # hook process before any JSON is emitted -- which would either
        # default-allow the Task (PROXY mode) or skip the deny (DENY
        # mode), defeating the plugin's purpose.
        try:
            parsed = frontmatter.loads(content)
        except yaml.YAMLError as e:
            logger.warning("agent_definitions: failed to parse frontmatter in {}: {}", candidate, e)
            continue
        body = parsed.content.lstrip("\n")
        return AgentDefinition(path=candidate, body=body)
    return None
