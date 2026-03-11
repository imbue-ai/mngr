import json
from collections.abc import Callable
from pathlib import Path

import pytest

from imbue.mng.cli.complete import _filter_aliases
from imbue.mng.cli.complete import _get_completions
from imbue.mng.cli.complete import _read_cache
from imbue.mng.cli.complete import _read_discovery_names
from imbue.mng.cli.complete import _read_git_branches
from imbue.mng.cli.complete import _read_host_names
from imbue.mng.config.completion_types import COMPLETION_CACHE_FILENAME
from imbue.mng.config.completion_types import CompletionCacheData
from imbue.mng.utils.testing import run_git_command
from imbue.mng.utils.testing import write_discovery_snapshot_to_path


def _write_command_cache(cache_dir: Path, data: CompletionCacheData) -> None:
    """Write a command completions cache file for testing."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / COMPLETION_CACHE_FILENAME).write_text(json.dumps(data._asdict()))


def _write_discovery_events(
    host_dir: Path,
    agent_names: list[str],
    host_names: list[str] | None = None,
) -> None:
    """Write a DISCOVERY_FULL event to the discovery events file for testing."""
    events_path = host_dir / "events" / "mng" / "discovery" / "events.jsonl"
    write_discovery_snapshot_to_path(events_path, agent_names, host_names=host_names)


@pytest.fixture
def completion_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temporary completion cache directory via MNG_COMPLETION_CACHE_DIR."""
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(tmp_path))
    # Also set MNG_HOST_DIR so discovery events are read from the same tmp dir
    monkeypatch.setenv("MNG_HOST_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def set_comp_env(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], None]:
    """Return a helper that sets COMP_WORDS and COMP_CWORD for tab completion tests."""

    def _set(words: str, cword: str) -> None:
        monkeypatch.setenv("COMP_WORDS", words)
        monkeypatch.setenv("COMP_CWORD", cword)

    return _set


# =============================================================================
# _read_cache tests
# =============================================================================


def test_read_cache_returns_data(completion_cache_dir: Path) -> None:
    data = CompletionCacheData(commands=["create", "list"])
    _write_command_cache(completion_cache_dir, data)

    result = _read_cache()

    assert result.commands == ["create", "list"]


def test_read_cache_returns_defaults_when_missing(completion_cache_dir: Path) -> None:
    result = _read_cache()

    assert result == CompletionCacheData()


def test_read_cache_returns_defaults_for_malformed_json(completion_cache_dir: Path) -> None:
    (completion_cache_dir / COMPLETION_CACHE_FILENAME).write_text("not json {{{")

    result = _read_cache()

    assert result == CompletionCacheData()


# =============================================================================
# _read_discovery_names tests
# =============================================================================


def test_read_discovery_names_returns_agent_and_host_names(completion_cache_dir: Path) -> None:
    _write_discovery_events(completion_cache_dir, ["beta", "alpha"], host_names=["saturn", "mars"])

    agent_names, host_names = _read_discovery_names()

    assert agent_names == ["alpha", "beta"]
    assert host_names == ["mars", "saturn"]


def test_read_discovery_names_returns_empty_when_missing(completion_cache_dir: Path) -> None:
    agent_names, host_names = _read_discovery_names()

    assert agent_names == []
    assert host_names == []


# =============================================================================
# _filter_aliases tests
# =============================================================================


def test_filter_aliases_drops_alias_when_canonical_matches() -> None:
    commands = ["c", "config", "connect", "create"]
    aliases = {"c": "create", "cfg": "config"}

    result = _filter_aliases(commands, aliases, "c")

    assert "c" not in result
    assert "config" in result
    assert "connect" in result
    assert "create" in result


def test_filter_aliases_keeps_alias_when_canonical_does_not_match() -> None:
    commands = ["c", "config", "connect", "create"]
    aliases = {"c": "create"}

    result = _filter_aliases(commands, aliases, "cfg")

    # "cfg" does not match anything, so nothing is returned
    assert result == []


def test_filter_aliases_no_aliases() -> None:
    commands = ["create", "list", "destroy"]
    aliases: dict[str, str] = {}

    result = _filter_aliases(commands, aliases, "")

    assert result == ["create", "list", "destroy"]


# =============================================================================
# _get_completions tests
# =============================================================================


def test_get_completions_command_name(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing the command name at position 1."""
    data = CompletionCacheData(
        commands=["ask", "config", "connect", "create", "destroy", "list"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng cr", "1")

    result = _get_completions()

    assert result == ["create"]


def test_get_completions_command_name_all(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing with empty incomplete returns all commands."""
    data = CompletionCacheData(commands=["ask", "create", "list"])
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng ", "1")

    result = _get_completions()

    assert result == ["ask", "create", "list"]


def test_get_completions_alias_filtering(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Aliases should be filtered when their canonical name also matches."""
    data = CompletionCacheData(
        commands=["c", "cfg", "config", "connect", "create"],
        aliases={"c": "create", "cfg": "config"},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng c", "1")

    result = _get_completions()

    assert "create" in result
    assert "config" in result
    assert "connect" in result
    assert "c" not in result
    assert "cfg" not in result


def test_get_completions_subcommand(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing subcommands of a group command."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["edit", "get", "list", "set"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng config ", "2")

    result = _get_completions()

    assert result == ["edit", "get", "list", "set"]


def test_get_completions_subcommand_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing subcommands with a prefix."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["edit", "get", "list", "set"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng config s", "2")

    result = _get_completions()

    assert result == ["set"]


def test_get_completions_options(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing options for a command."""
    data = CompletionCacheData(
        commands=["list"],
        options_by_command={"list": ["--format", "--help", "--running", "--stopped"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng list --f", "2")

    result = _get_completions()

    assert result == ["--format"]


def test_get_completions_option_choices(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing values for an option with choices."""
    data = CompletionCacheData(
        commands=["list"],
        options_by_command={"list": ["--help", "--on-error"]},
        option_choices={"list.--on-error": ["abort", "continue"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng list --on-error ", "3")

    result = _get_completions()

    assert result == ["abort", "continue"]


def test_get_completions_option_choices_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing values for an option with choices and a prefix."""
    data = CompletionCacheData(
        commands=["list"],
        options_by_command={"list": ["--help", "--on-error"]},
        option_choices={"list.--on-error": ["abort", "continue"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng list --on-error a", "3")

    result = _get_completions()

    assert result == ["abort"]


def test_get_completions_subcommand_options(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing options for a subcommand (dot-separated key)."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "set"]},
        options_by_command={"config.get": ["--help", "--scope"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng config get --", "3")

    result = _get_completions()

    assert "--help" in result
    assert "--scope" in result


def test_get_completions_subcommand_option_choices(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing values for a subcommand option with choices."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "set"]},
        options_by_command={"config.get": ["--help", "--scope"]},
        option_choices={"config.get.--scope": ["user", "project", "local"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng config get --scope ", "4")

    result = _get_completions()

    assert result == ["user", "project", "local"]


def test_get_completions_agent_names(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for commands that accept agent arguments."""
    data = CompletionCacheData(
        commands=["connect", "list"],
        agent_name_arguments=["connect"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng connect ", "2")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_agent_names_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names with a prefix filter."""
    data = CompletionCacheData(
        commands=["connect"],
        agent_name_arguments=["connect"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng connect my", "2")

    result = _get_completions()

    assert result == ["my-agent"]


def test_get_completions_no_agent_names_for_non_agent_command(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands not in agent_name_arguments should not complete agent names."""
    data = CompletionCacheData(
        commands=["list"],
        agent_name_arguments=["connect"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mng list ", "2")

    result = _get_completions()

    assert result == []


def test_get_completions_subcommand_agent_names(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for group subcommands (e.g. mng snapshot create <TAB>)."""
    data = CompletionCacheData(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create", "destroy", "list"]},
        agent_name_arguments=["snapshot.create", "snapshot.destroy", "snapshot.list"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng snapshot create ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_subcommand_agent_names_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for group subcommands with a prefix filter."""
    data = CompletionCacheData(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create", "destroy", "list"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng snapshot create my", "3")

    result = _get_completions()

    assert result == ["my-agent"]


def test_get_completions_subcommand_no_agent_names_for_non_agent_subcommand(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Subcommands not in agent_name_arguments should not complete agent names."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "list", "set"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mng config get ", "3")

    result = _get_completions()

    assert result == []


def test_get_completions_alias_resolves_to_canonical(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """An alias typed as the command should resolve to the canonical name for option lookup."""
    data = CompletionCacheData(
        commands=["conn", "connect"],
        aliases={"conn": "connect"},
        options_by_command={"connect": ["--help", "--start"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng conn --", "2")

    result = _get_completions()

    assert "--help" in result
    assert "--start" in result


def test_get_completions_empty_cache(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """When the cache is missing, no completions are returned."""
    set_comp_env("mng ", "1")

    result = _get_completions()

    assert result == []


def test_get_completions_invalid_comp_cword(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """When COMP_CWORD is not a valid integer, no completions are returned."""
    set_comp_env("mng ", "not-a-number")

    result = _get_completions()

    assert result == []


# =============================================================================
# Option handling: flags vs value-taking options
# =============================================================================


def test_get_completions_value_taking_option_suppresses_completions(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a value-taking option (--name), no completions should be offered."""
    data = CompletionCacheData(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create"]},
        options_by_command={"snapshot.create": ["--all", "--dry-run", "--name"]},
        flag_options_by_command={"snapshot.create": ["--all", "--dry-run", "-a"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mng snapshot create --name ", "4")

    result = _get_completions()

    assert result == []


def test_get_completions_long_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a --long flag (--force), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["destroy"],
        options_by_command={"destroy": ["--all", "--force"]},
        flag_options_by_command={"destroy": ["--all", "--force", "-a", "-f"]},
        agent_name_arguments=["destroy"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng destroy --force ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_short_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a -short flag (-f), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["destroy"],
        options_by_command={"destroy": ["--all", "--force"]},
        flag_options_by_command={"destroy": ["--all", "--force", "-a", "-f"]},
        agent_name_arguments=["destroy"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng destroy -f ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_combined_short_flags_allow_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After combined short flags (-fb), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["destroy"],
        aliases={"rm": "destroy"},
        options_by_command={"destroy": ["--all", "--force", "--remove-created-branch"]},
        flag_options_by_command={"destroy": ["--all", "--force", "--remove-created-branch", "-a", "-f", "-b"]},
        agent_name_arguments=["destroy"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng rm -fb ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_combined_short_flags_with_unknown_flag(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Combined flags where one character is not a known flag should suppress completions."""
    data = CompletionCacheData(
        commands=["destroy"],
        options_by_command={"destroy": ["--all", "--force"]},
        flag_options_by_command={"destroy": ["--all", "--force", "-a", "-f"]},
        agent_name_arguments=["destroy"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mng destroy -fx ", "3")

    result = _get_completions()

    assert result == []


def test_get_completions_subcommand_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a flag on a subcommand (--dry-run), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create"]},
        options_by_command={"snapshot.create": ["--all", "--dry-run", "--name"]},
        flag_options_by_command={"snapshot.create": ["--all", "--dry-run", "-a"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng snapshot create --dry-run ", "4")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


# =============================================================================
# Git branch completion tests
# =============================================================================


def test_read_git_branches_returns_branches(temp_git_repo_cwd: Path) -> None:
    """_read_git_branches should return branch names from a real git repo."""
    run_git_command(temp_git_repo_cwd, "branch", "develop")
    run_git_command(temp_git_repo_cwd, "branch", "feature/foo")

    result = _read_git_branches()

    assert "develop" in result
    assert "feature/foo" in result


def test_read_git_branches_returns_empty_outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_git_branches should return an empty list when not in a git repo."""
    monkeypatch.chdir(tmp_path)

    result = _read_git_branches()

    assert result == []


def test_get_completions_git_branch_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
    temp_git_repo_cwd: Path,
) -> None:
    """Completing values for a git branch option should offer branch names."""
    run_git_command(temp_git_repo_cwd, "branch", "develop")
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--base-branch", "--name"]},
        git_branch_options=["create.--base-branch"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --base-branch ", "3")

    result = _get_completions()

    assert "develop" in result


def test_get_completions_git_branch_option_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
    temp_git_repo_cwd: Path,
) -> None:
    """Completing values for a git branch option should filter by prefix."""
    run_git_command(temp_git_repo_cwd, "branch", "develop")
    run_git_command(temp_git_repo_cwd, "branch", "feature/foo")
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--base-branch", "--name"]},
        git_branch_options=["create.--base-branch"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --base-branch dev", "3")

    result = _get_completions()

    assert result == ["develop"]


def test_get_completions_git_branch_option_not_triggered_for_other_options(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
    temp_git_repo_cwd: Path,
) -> None:
    """Options not in git_branch_options should not trigger git branch completion."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--base-branch", "--name"]},
        git_branch_options=["create.--base-branch"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --name ", "3")

    result = _get_completions()

    assert result == []


# =============================================================================
# Host name completion tests
# =============================================================================


def test_read_host_names_returns_names(completion_cache_dir: Path) -> None:
    _write_discovery_events(completion_cache_dir, [], host_names=["my-host", "other-host"])

    result = _read_host_names()

    assert result == ["my-host", "other-host"]


def test_read_host_names_returns_empty_when_missing(completion_cache_dir: Path) -> None:
    result = _read_host_names()

    assert result == []


def test_get_completions_host_name_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing values for a host name option should offer host names."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--host", "--name"]},
        host_name_options=["create.--host"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, [], host_names=["saturn", "jupiter"])
    set_comp_env("mng create --host ", "3")

    result = _get_completions()

    assert "saturn" in result
    assert "jupiter" in result


def test_get_completions_host_name_option_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Host name completion should filter by prefix."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--host", "--name"]},
        host_name_options=["create.--host"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, [], host_names=["saturn", "jupiter"])
    set_comp_env("mng create --host sat", "3")

    result = _get_completions()

    assert result == ["saturn"]


def test_get_completions_host_name_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands in host_name_arguments should offer host names as positional args."""
    data = CompletionCacheData(
        commands=["events"],
        agent_name_arguments=["events"],
        host_name_arguments=["events"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"], host_names=["saturn"])
    set_comp_env("mng events ", "2")

    result = _get_completions()

    assert "my-agent" in result
    assert "saturn" in result


# =============================================================================
# Plugin name completion tests
# =============================================================================


def test_get_completions_plugin_name_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing values for --plugin should offer plugin names."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--name", "--plugin"]},
        plugin_name_options=["create.--plugin"],
        plugin_names=["claude", "docker", "modal"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --plugin ", "3")

    result = _get_completions()

    assert result == ["claude", "docker", "modal"]


def test_get_completions_plugin_name_option_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Plugin name completion should filter by prefix."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--name", "--plugin"]},
        plugin_name_options=["create.--plugin"],
        plugin_names=["claude", "docker", "modal"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --plugin do", "3")

    result = _get_completions()

    assert result == ["docker"]


def test_get_completions_plugin_name_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Plugin enable/disable subcommands should complete plugin names positionally."""
    data = CompletionCacheData(
        commands=["plugin"],
        subcommand_by_command={"plugin": ["enable", "disable", "list"]},
        plugin_name_arguments=["plugin.enable", "plugin.disable"],
        plugin_names=["claude", "docker", "modal"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng plugin enable ", "3")

    result = _get_completions()

    assert result == ["claude", "docker", "modal"]


def test_get_completions_plugin_name_positional_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Plugin name positional completion should filter by prefix."""
    data = CompletionCacheData(
        commands=["plugin"],
        subcommand_by_command={"plugin": ["enable", "disable"]},
        plugin_name_arguments=["plugin.enable"],
        plugin_names=["claude", "docker", "modal"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng plugin enable cl", "3")

    result = _get_completions()

    assert result == ["claude"]


# =============================================================================
# Config key completion tests
# =============================================================================


def test_get_completions_config_key_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Config get/set/unset should complete config keys positionally."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "set", "unset", "list"]},
        config_key_arguments=["config.get", "config.set", "config.unset"],
        config_keys=["prefix", "logging.console_level", "logging.file_level"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng config get ", "3")

    result = _get_completions()

    assert "prefix" in result
    assert "logging.console_level" in result
    assert "logging.file_level" in result


def test_get_completions_config_key_positional_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Config key completion should filter by prefix."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "set", "unset"]},
        config_key_arguments=["config.get"],
        config_keys=["prefix", "logging.console_level", "logging.file_level"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng config get log", "3")

    result = _get_completions()

    assert result == ["logging.console_level", "logging.file_level"]


# =============================================================================
# Dynamic option choices tests (agent types, templates, providers)
# =============================================================================


def test_get_completions_agent_type_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing --agent-type should offer agent type names from option_choices."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--agent-type", "--name"]},
        option_choices={"create.--agent-type": ["claude", "codex", "my-custom"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --agent-type ", "3")

    result = _get_completions()

    assert result == ["claude", "codex", "my-custom"]


def test_get_completions_template_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing --template should offer template names from option_choices."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--name", "--template"]},
        option_choices={"create.--template": ["dev", "prod"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --template ", "3")

    result = _get_completions()

    assert result == ["dev", "prod"]


def test_get_completions_provider_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing --in should offer provider names from option_choices."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--in", "--name"]},
        option_choices={"create.--in": ["docker", "local", "modal"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng create --in ", "3")

    result = _get_completions()

    assert result == ["docker", "local", "modal"]
