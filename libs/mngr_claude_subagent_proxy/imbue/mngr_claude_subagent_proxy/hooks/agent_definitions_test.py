"""Unit tests for the typed-``subagent_type`` agent-definition resolver.

The resolver maps a Claude Code ``subagent_type`` (e.g.
``imbue-code-guardian:verify-and-fix``, ``general-purpose``) to an
on-disk ``.md`` agent definition under one of three discovery roots:

- ``<work_dir>/.claude/agents/`` (project-local; closest)
- ``~/.claude/agents/`` (user-level)
- ``~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/``
  (Claude Code marketplace plugins; only used for ``plugin:agent``
  namespaced types)

Tests use ``monkeypatch.setenv("HOME", ...)`` to point the resolver at a
per-test ``tmp_path`` so they don't read the developer's actual
``~/.claude/`` directory. The resolver actually goes through
``get_user_claude_config_dir()``, which prefers ``$ORIGINAL_CLAUDE_CONFIG_DIR``
then ``$CLAUDE_CONFIG_DIR`` then ``Path.home() / ".claude"``; the autouse
``setup_test_mngr_env`` fixture (via ``isolate_home``) already ``delenv``'s
the two config-dir vars, so the per-test ``HOME`` override is sufficient
to redirect the lookup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imbue.mngr_claude_subagent_proxy.hooks.agent_definitions import resolve_agent_definition


def _write_agent(path: Path, *, name: str, body: str, description: str = "test agent") -> None:
    """Write a Claude Code agent definition file with frontmatter + body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at a per-test ``tmp_path`` so the resolver walks the
    test's fake home directory instead of the developer's.

    The resolver resolves the user-scope Claude config dir at request
    time (not at import) via ``get_user_claude_config_dir()``, so a
    runtime monkeypatch of ``HOME`` is honored. This relies on the
    autouse ``setup_test_mngr_env`` fixture having already cleared
    ``$ORIGINAL_CLAUDE_CONFIG_DIR`` and ``$CLAUDE_CONFIG_DIR``, which
    otherwise take precedence over ``HOME``. Returns the fake home
    path for tests that need to drop files under it.
    """
    home = tmp_path / "fake_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_resolves_user_installed_agent_at_user_claude_agents(fake_home: Path, tmp_path: Path) -> None:
    """A flat agent at ``~/.claude/agents/<name>.md`` resolves with body stripped of frontmatter."""
    user_agents = fake_home / ".claude" / "agents"
    _write_agent(
        user_agents / "code-reviewer.md",
        name="code-reviewer",
        body="You are a code reviewer. Be thorough.",
        description="reviews code",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = resolve_agent_definition("code-reviewer", work_dir)

    assert result is not None
    assert result.path == user_agents / "code-reviewer.md"
    # Body is post-frontmatter; the leading "---" YAML block is gone.
    assert "---" not in result.body
    assert "name: code-reviewer" not in result.body
    assert result.body.startswith("You are a code reviewer.")


def test_resolves_marketplace_plugin_agent(fake_home: Path, tmp_path: Path) -> None:
    """A plugin-namespaced ``plugin:agent`` resolves under
    ``~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/<agent>.md``.

    Mirrors the on-disk layout Claude Code uses for installed marketplace
    plugins -- ``imbue-code-guardian:verify-and-fix`` lives at
    ``.../marketplaces/imbue-code-guardian/plugins/imbue-code-guardian/agents/verify-and-fix.md``.
    """
    marketplace_agent = (
        fake_home
        / ".claude"
        / "plugins"
        / "marketplaces"
        / "imbue-code-guardian"
        / "plugins"
        / "imbue-code-guardian"
        / "agents"
        / "verify-and-fix.md"
    )
    _write_agent(
        marketplace_agent,
        name="verify-and-fix",
        body="You are an autonomous verifier-and-fixer. Use your best judgment.",
        description="verify and fix branch issues",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = resolve_agent_definition("imbue-code-guardian:verify-and-fix", work_dir)

    assert result is not None
    assert result.path == marketplace_agent
    assert result.body.startswith("You are an autonomous verifier-and-fixer.")


def test_returns_none_for_unknown_builtin_subagent_type(fake_home: Path, tmp_path: Path) -> None:
    """Built-in types like ``general-purpose`` have no on-disk file -- resolver returns None.

    Callers are expected to fall back to the prompt-only spawn path
    (no system-prompt prepend) for these.
    """
    # ``fake_home`` is declared so ``HOME`` is rooted in ``tmp_path`` (so the resolver doesn't
    # touch the developer's real ``~/.claude``); the body doesn't reference it directly.
    del fake_home
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    assert resolve_agent_definition("general-purpose", work_dir) is None
    assert resolve_agent_definition("Explore", work_dir) is None
    assert resolve_agent_definition("Plan", work_dir) is None
    # Plugin-namespaced unknowns also return None.
    assert resolve_agent_definition("some-plugin:nonexistent", work_dir) is None
    # Empty type is always None.
    assert resolve_agent_definition("", work_dir) is None


def test_project_local_overrides_user_level_same_name(fake_home: Path, tmp_path: Path) -> None:
    """When both ``<work_dir>/.claude/agents/X.md`` and ``~/.claude/agents/X.md``
    exist with the same name, the project-local copy wins.

    Mirrors Claude Code's own discovery precedence: closer scope shadows
    user-global. Without this, a developer trying to override an agent
    for one project would have to delete the user-level file globally.
    """
    user_agents = fake_home / ".claude" / "agents"
    _write_agent(
        user_agents / "explorer.md",
        name="explorer",
        body="USER-LEVEL explorer body.",
    )
    work_dir = tmp_path / "work"
    project_agents = work_dir / ".claude" / "agents"
    _write_agent(
        project_agents / "explorer.md",
        name="explorer",
        body="PROJECT-LOCAL explorer body.",
    )

    result = resolve_agent_definition("explorer", work_dir)

    assert result is not None
    assert result.path == project_agents / "explorer.md"
    assert "PROJECT-LOCAL" in result.body
    assert "USER-LEVEL" not in result.body


def test_resolves_definition_without_frontmatter(fake_home: Path, tmp_path: Path) -> None:
    """A .md file with no YAML frontmatter still resolves -- the body is
    the entire file content. The resolver doesn't reject the file; it
    just doesn't try to strip a non-existent frontmatter block.
    """
    user_agents = fake_home / ".claude" / "agents"
    user_agents.mkdir(parents=True)
    (user_agents / "no-frontmatter.md").write_text("Just a body, no frontmatter.\n")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = resolve_agent_definition("no-frontmatter", work_dir)

    assert result is not None
    assert result.body.startswith("Just a body, no frontmatter.")


def test_marketplace_namespaced_does_not_fall_back_to_user_flat_agent(fake_home: Path, tmp_path: Path) -> None:
    """A ``plugin:agent`` lookup does NOT silently match a flat
    ``~/.claude/agents/agent.md`` -- that would let a user-level agent
    shadow a marketplace plugin's same-named agent under a different
    plugin namespace, which would be a confusing collision.
    """
    user_agents = fake_home / ".claude" / "agents"
    _write_agent(
        user_agents / "verify-and-fix.md",
        name="verify-and-fix",
        body="UNRELATED flat user agent.",
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    assert resolve_agent_definition("imbue-code-guardian:verify-and-fix", work_dir) is None


def test_first_marketplace_wins_when_multiple_have_same_plugin_agent(fake_home: Path, tmp_path: Path) -> None:
    """If two marketplaces both ship ``<plugin>/agents/<agent>.md``, the
    first (sorted by name) wins. Stable precedence is required so the
    typed-subagent path stays deterministic; depending on iteration
    order would make the deny reason non-deterministic.
    """
    marketplaces = fake_home / ".claude" / "plugins" / "marketplaces"
    _write_agent(
        marketplaces / "alpha" / "plugins" / "shared" / "agents" / "thing.md",
        name="thing",
        body="ALPHA marketplace body.",
    )
    _write_agent(
        marketplaces / "beta" / "plugins" / "shared" / "agents" / "thing.md",
        name="thing",
        body="BETA marketplace body.",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = resolve_agent_definition("shared:thing", work_dir)

    assert result is not None
    # Sorted iteration -> "alpha" < "beta".
    assert "ALPHA" in result.body
    assert "BETA" not in result.body
