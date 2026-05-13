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


def test_malformed_yaml_frontmatter_returns_none_without_raising(fake_home: Path, tmp_path: Path) -> None:
    """A .md file with malformed YAML frontmatter must not crash the resolver --
    ``frontmatter.loads`` raises ``yaml.YAMLError`` on bad YAML, which would
    otherwise propagate out of ``run()`` in the PreToolUse hook and prevent
    any JSON response from being emitted.

    The resolver logs a warning and returns ``None`` for the candidate (same
    handling as an unreadable file), so the caller falls back to the
    prompt-only / unresolved-type path instead of breaking the hook.

    Regression for: a typo in an installed marketplace agent's frontmatter
    would otherwise wedge the entire mngr-proxy plugin for that agent.
    """
    user_agents = fake_home / ".claude" / "agents"
    user_agents.mkdir(parents=True)
    # Malformed YAML: unclosed bracket / missing value. Triggers yaml.YAMLError
    # inside frontmatter.loads.
    (user_agents / "broken.md").write_text(
        "---\nname: [unclosed\ndescription: still bad\n---\n\nbody after broken fm\n"
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Must not raise.
    result = resolve_agent_definition("broken", work_dir)
    assert result is None


@pytest.mark.parametrize(
    "subagent_type",
    [
        pytest.param("../../etc/passwd", id="non_namespaced_traversal"),
        pytest.param("foo/bar", id="non_namespaced_slash"),
        pytest.param("foo\\bar", id="non_namespaced_backslash"),
        pytest.param("..", id="non_namespaced_dotdot"),
        pytest.param(".", id="non_namespaced_dot"),
        pytest.param("plugin:../etc/hosts", id="namespaced_agent_traversal"),
        pytest.param("..:agent", id="namespaced_plugin_dotdot"),
        pytest.param("plugin:foo/bar", id="namespaced_agent_slash"),
        pytest.param("plugin\\x:agent", id="namespaced_plugin_backslash"),
        pytest.param("foo\x00bar", id="non_namespaced_nul"),
    ],
)
def test_unsafe_subagent_type_rejected(subagent_type: str, fake_home: Path, tmp_path: Path) -> None:
    """Path-meaningful characters in ``subagent_type`` are rejected so the
    resolver cannot traverse out of the ``.claude/agents/`` dir to read
    an unrelated ``.md`` file. Even if a malicious ``.md`` were planted
    at the traversal target, the resolver must refuse to open it.

    Defense-in-depth: ``subagent_type`` comes from Claude's Task tool
    call (LLM output) and is not strictly attacker-controlled, but
    the same conservative rejection also surfaces typos that would
    otherwise silently miss.
    """
    # Plant a .md file at a plausible traversal target so the test
    # would fail (resolve to that file) if validation were missing.
    traversal_target = fake_home.parent / "traversal_target.md"
    traversal_target.write_text("---\nname: traversal\n---\n\nLEAKED CONTENT.\n")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = resolve_agent_definition(subagent_type, work_dir)
    assert result is None


@pytest.mark.parametrize(
    "subagent_type",
    [
        pytest.param(":", id="bare_colon"),
        pytest.param("foo:", id="trailing_colon_empty_agent"),
        pytest.param(":bar", id="leading_colon_empty_plugin"),
    ],
)
def test_malformed_namespaced_subagent_type_returns_none(subagent_type: str, fake_home: Path, tmp_path: Path) -> None:
    """Plugin-namespaced types with an empty ``plugin_name`` or
    ``agent_name`` segment (subagent_type starts or ends with ``:``,
    or is just ``:``) must resolve to None rather than silently
    constructing a path with an empty segment that can never match.

    Pins the empty-segment contract independently of the broader
    path-traversal validation; documents that a typo'd Task call
    like ``Task(subagent_type=":")`` surfaces as a logged warning
    plus unresolved (not a crash, not a no-op match).
    """
    # ``fake_home`` is declared so ``HOME`` is rooted in ``tmp_path``; the body
    # doesn't reference it directly.
    del fake_home
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    assert resolve_agent_definition(subagent_type, work_dir) is None


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
