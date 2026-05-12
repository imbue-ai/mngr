"""Resolve a Claude Code ``subagent_type`` to its agent-definition file.

Claude Code's typed subagents are spawned from ``.md`` files containing
YAML frontmatter (name, description, tools, model) and a Markdown body
that becomes the spawned subagent's system prompt. This module surfaces
the body so the plugin can preserve the system-prompt contract when
proxying or denying typed Task calls (otherwise both modes effectively
strip the system prompt and spawn a generic Claude).

Built-in agent types -- ``general-purpose``, ``Explore``, etc. -- are
baked into Claude Code itself and have no on-disk definition. The
resolver returns ``None`` for those (and any other unknown name); the
caller falls back to the prompt-only spawn path.

Discovery branches on whether ``subagent_type`` is plugin-namespaced
(contains ``:``):

- Non-namespaced (e.g. ``code-reviewer``): walk in precedence order,
  closest wins, and stop at the first hit:

  1. ``<work_dir>/.claude/agents/<name>.md`` -- project-local.
  2. ``~/.claude/agents/<name>.md`` -- user-level.

- Plugin-namespaced (``plugin:agent``, e.g.
  ``imbue-code-guardian:verify-and-fix``): only the marketplaces root
  is checked --
  ``~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/<agent>.md``
  (marketplaces enumerated in sorted order; first hit wins). The flat
  ``~/.claude/agents/`` is NOT a fallback for namespaced types -- a
  same-named flat file under a different plugin namespace would be a
  silent collision, so we refuse to cross the boundary.

Tool restrictions declared in the frontmatter (``tools: [Read, Grep]``)
are NOT honored in v1: a plain mngr Claude subagent inherits the user's
full Claude config. The skill + ``SubagentProxyMode.DENY`` docstring
flag this limitation. A future branch can add ``--type`` variants with
restricted permissions.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_claude.claude_config import get_user_claude_config_dir

_FRONTMATTER_RE: Final[re.Pattern[str]] = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


class AgentDefinition(FrozenModel):
    """An on-disk Claude Code agent definition resolved from a ``subagent_type``."""

    path: Path = Field(description="Absolute path to the resolved .md file.")
    body: str = Field(description="The Markdown body (post-frontmatter) -- the system prompt Claude Code would use.")


def _strip_frontmatter(content: str) -> str:
    """Strip a leading YAML frontmatter block from ``content``.

    The frontmatter delimiter is the standard ``---`` line on its own;
    if no frontmatter is present, returns ``content`` unchanged (after
    stripping any leading blank lines, so the body starts on the first
    non-empty line whether or not frontmatter was present).
    """
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return content.lstrip("\n")
    return content[match.end() :].lstrip("\n")


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


def _iter_candidate_paths(subagent_type: str, work_dir: Path) -> Iterator[Path]:
    """Yield candidate ``.md`` paths for ``subagent_type`` in precedence order.

    Plugin-namespaced types (``plugin:agent``) are only resolved via the
    user's installed marketplaces; non-namespaced types are only
    resolved via project-local then user-level ``.claude/agents/``.
    Mixing the two would silently let a flat user file shadow a
    marketplace-installed agent of the same agent-name, which would
    be surprising.
    """
    if ":" in subagent_type:
        plugin_name, _, agent_name = subagent_type.partition(":")
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
    yield work_dir / ".claude" / "agents" / f"{subagent_type}.md"
    yield _user_claude_agents_dir() / f"{subagent_type}.md"


def resolve_agent_definition(subagent_type: str, work_dir: Path) -> AgentDefinition | None:
    """Resolve ``subagent_type`` to the agent definition Claude Code would load.

    Returns ``None`` for:
    - empty ``subagent_type``,
    - built-in types (``general-purpose``, ``Explore``, ...) with no on-disk file,
    - any plugin-namespaced type whose marketplace path doesn't exist,
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
        return AgentDefinition(path=candidate, body=_strip_frontmatter(content))
    return None
