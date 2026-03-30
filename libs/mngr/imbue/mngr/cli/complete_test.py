import json
from collections.abc import Callable
from pathlib import Path

import pytest

from imbue.mngr.cli.complete import _filter_aliases
from imbue.mngr.cli.complete import _get_completions
from imbue.mngr.cli.complete import _read_cache
from imbue.mngr.cli.complete import _read_discovery_names
from imbue.mngr.cli.complete import _read_git_branches
from imbue.mngr.cli.complete import _read_host_names
from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import write_discovery_snapshot_to_path


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
    events_path = host_dir / "events" / "mngr" / "discovery" / "events.jsonl"
    write_discovery_snapshot_to_path(events_path, agent_names, host_names=host_names)


@pytest.fixture
def completion_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temporary completion cache directory via MNGR_COMPLETION_CACHE_DIR."""
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(tmp_path))
    # Also set MNGR_HOST_DIR so discovery events are read from the same tmp dir
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
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
    set_comp_env("mngr cr", "1")

    result = _get_completions()

    assert result == ["create"]


def test_get_completions_command_name_all(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing with empty incomplete returns all commands."""
    data = CompletionCacheData(commands=["ask", "create", "list"])
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr ", "1")

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
    set_comp_env("mngr c", "1")

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
    set_comp_env("mngr config ", "2")

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
    set_comp_env("mngr config s", "2")

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
    set_comp_env("mngr list --f", "2")

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
    set_comp_env("mngr list --on-error ", "3")

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
    set_comp_env("mngr list --on-error a", "3")

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
    set_comp_env("mngr config get --", "3")

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
    set_comp_env("mngr config get --scope ", "4")

    result = _get_completions()

    assert result == ["user", "project", "local"]


def test_get_completions_agent_names(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for commands that accept agent arguments."""
    data = CompletionCacheData(
        commands=["connect", "list"],
        positional_completions={"connect": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr connect ", "2")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_agent_names_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names with a prefix filter."""
    data = CompletionCacheData(
        commands=["connect"],
        positional_completions={"connect": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr connect my", "2")

    result = _get_completions()

    assert result == ["my-agent"]


def test_get_completions_no_agent_names_for_non_agent_command(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands not in positional_completions should not complete agent names."""
    data = CompletionCacheData(
        commands=["list"],
        positional_completions={"connect": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mngr list ", "2")

    result = _get_completions()

    assert result == []


def test_get_completions_subcommand_agent_names(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for group subcommands (e.g. mngr snapshot create <TAB>)."""
    data = CompletionCacheData(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create", "destroy", "list"]},
        positional_completions={
            "snapshot.create": [["agent_names"]],
            "snapshot.destroy": [["agent_names"]],
            "snapshot.list": [["agent_names"]],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr snapshot create ", "3")

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
        positional_completions={"snapshot.create": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr snapshot create my", "3")

    result = _get_completions()

    assert result == ["my-agent"]


def test_get_completions_subcommand_no_agent_names_for_non_agent_subcommand(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Subcommands not in positional_completions should not complete agent names."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "list", "set"]},
        positional_completions={"snapshot.create": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mngr config get ", "3")

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
    set_comp_env("mngr conn --", "2")

    result = _get_completions()

    assert "--help" in result
    assert "--start" in result


def test_get_completions_empty_cache(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """When the cache is missing, no completions are returned."""
    set_comp_env("mngr ", "1")

    result = _get_completions()

    assert result == []


def test_get_completions_invalid_comp_cword(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """When COMP_CWORD is not a valid integer, no completions are returned."""
    set_comp_env("mngr ", "not-a-number")

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
        options_by_command={"snapshot.create": ["--name", "--on-error"]},
        flag_options_by_command={"snapshot.create": []},
        positional_completions={"snapshot.create": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mngr snapshot create --name ", "4")

    result = _get_completions()

    assert result == []


def test_get_completions_long_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a --long flag (--force), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["destroy"],
        options_by_command={"destroy": ["--force"]},
        flag_options_by_command={"destroy": ["--force", "-f"]},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr destroy --force ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_short_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a -short flag (-f), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["destroy"],
        options_by_command={"destroy": ["--force"]},
        flag_options_by_command={"destroy": ["--force", "-f"]},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr destroy -f ", "3")

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
        options_by_command={"destroy": ["--force", "--remove-created-branch"]},
        flag_options_by_command={"destroy": ["--force", "--remove-created-branch", "-f", "-b"]},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr rm -fb ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_combined_short_flags_with_unknown_flag(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Combined flags where one character is not a known flag should suppress completions."""
    data = CompletionCacheData(
        commands=["destroy"],
        options_by_command={"destroy": ["--force"]},
        flag_options_by_command={"destroy": ["--force", "-f"]},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"])
    set_comp_env("mngr destroy -fx ", "3")

    result = _get_completions()

    assert result == []


def test_get_completions_subcommand_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a flag on a subcommand (--force), positional candidates should be offered."""
    data = CompletionCacheData(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["destroy"]},
        options_by_command={"snapshot.destroy": ["--force", "--snapshot"]},
        flag_options_by_command={"snapshot.destroy": ["--force", "-f"]},
        positional_completions={"snapshot.destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr snapshot destroy --force ", "4")

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
        options_by_command={"create": ["--branch", "--name"]},
        git_branch_options=["create.--branch"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr create --branch ", "3")

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
        options_by_command={"create": ["--branch", "--name"]},
        git_branch_options=["create.--branch"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr create --branch dev", "3")

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
        options_by_command={"create": ["--branch", "--name"]},
        git_branch_options=["create.--branch"],
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr create --name ", "3")

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
    set_comp_env("mngr create --host ", "3")

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
    set_comp_env("mngr create --host sat", "3")

    result = _get_completions()

    assert result == ["saturn"]


def test_get_completions_host_name_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands with host_names in positional_completions should offer host names."""
    data = CompletionCacheData(
        commands=["events"],
        positional_completions={"events": [["agent_names", "host_names"], []]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"], host_names=["saturn"])
    set_comp_env("mngr events ", "2")

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
    set_comp_env("mngr create --plugin ", "3")

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
    set_comp_env("mngr create --plugin do", "3")

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
        plugin_names=["claude", "docker", "modal"],
        positional_completions={
            "plugin.enable": [["plugin_names"]],
            "plugin.disable": [["plugin_names"]],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr plugin enable ", "3")

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
        plugin_names=["claude", "docker", "modal"],
        positional_completions={"plugin.enable": [["plugin_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr plugin enable cl", "3")

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
        config_keys=["prefix", "logging.console_level", "logging.file_level"],
        positional_completions={
            "config.get": [["config_keys"]],
            "config.set": [["config_keys"], ["config_value_for_key"]],
            "config.unset": [["config_keys"]],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config get ", "3")

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
        config_keys=["prefix", "logging.console_level", "logging.file_level"],
        positional_completions={"config.get": [["config_keys"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config get log", "3")

    result = _get_completions()

    assert result == ["logging.console_level", "logging.file_level"]


# =============================================================================
# Dynamic option choices tests (agent types, templates, providers)
# =============================================================================


def test_get_completions_agent_type_option(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing --type should offer agent type names from option_choices."""
    data = CompletionCacheData(
        commands=["create"],
        options_by_command={"create": ["--type", "--name"]},
        option_choices={"create.--type": ["claude", "codex", "my-custom"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr create --type ", "3")

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
    set_comp_env("mngr create --template ", "3")

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
    set_comp_env("mngr create --in ", "3")

    result = _get_completions()

    assert result == ["docker", "local", "modal"]


# =============================================================================
# Positional nargs limiting tests
# =============================================================================


def test_get_completions_nargs_limit_reached(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After all positional args are filled, no more positional candidates are offered."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["get", "set", "unset", "list"]},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    # "mngr config set KEY VALUE <TAB>" -- 2 positional args already typed
    set_comp_env("mngr config set prefix myval ", "5")

    result = _get_completions()

    assert result == []


def test_get_completions_nargs_limit_not_reached(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Before the nargs limit is reached, positional candidates are offered."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    # "mngr config set <TAB>" -- 0 positional args typed
    set_comp_env("mngr config set ", "3")

    result = _get_completions()

    assert "prefix" in result
    assert "logging.console_level" in result


def test_get_completions_nargs_interleaved_options(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Interleaved options should not count toward the positional nargs limit."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        options_by_command={"config.set": ["--scope"]},
        flag_options_by_command={},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    # "mngr config set --scope user KEY <TAB>" -- only 1 positional (KEY), room for VALUE
    # Position 1 uses config_value_for_key, but no config_value_choices are provided,
    # so no candidates should be offered.
    set_comp_env("mngr config set --scope user prefix ", "6")

    result = _get_completions()

    assert result == []


def test_get_completions_nargs_unlimited(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands with unlimited nargs (None) should always offer positional candidates."""
    data = CompletionCacheData(
        commands=["destroy"],
        positional_nargs_by_command={"destroy": None},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["agent1", "agent2", "agent3"])
    # "mngr destroy agent1 agent2 <TAB>" -- 2 positional args, but unlimited
    set_comp_env("mngr destroy agent1 agent2 ", "4")

    result = _get_completions()

    assert "agent1" in result
    assert "agent2" in result
    assert "agent3" in result


def test_get_completions_nargs_missing_entry(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands not in positional_nargs_by_command should be treated as unlimited."""
    data = CompletionCacheData(
        commands=["destroy"],
        positional_nargs_by_command={},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["agent1", "agent2"])
    # "mngr destroy agent1 <TAB>" -- command not in nargs dict -> unlimited
    set_comp_env("mngr destroy agent1 ", "3")

    result = _get_completions()

    assert "agent1" in result
    assert "agent2" in result


def test_get_completions_nargs_limit_after_flag(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """The nargs limit should be enforced even when prev_word is a flag."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        options_by_command={"config.set": ["--verbose"]},
        flag_options_by_command={"config.set": ["--verbose"]},
        config_keys=["prefix"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    # "mngr config set KEY VALUE --verbose <TAB>" -- 2 positional args + a flag
    set_comp_env("mngr config set prefix myval --verbose ", "6")

    result = _get_completions()

    # All 2 positional args consumed, so no more positional candidates
    assert result == []


# =============================================================================
# Per-position positional completion tests
# =============================================================================


def test_get_completions_config_set_pos0_offers_keys(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set <TAB> at position 0 should offer config keys."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set ", "3")

    result = _get_completions()

    assert "prefix" in result
    assert "logging.console_level" in result


def test_get_completions_config_set_pos1_string_field_no_completions(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set prefix <TAB> at position 1 should offer nothing (string field, no constrained values)."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={"logging.console_level": ["TRACE", "DEBUG", "INFO"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set prefix ", "4")

    result = _get_completions()

    assert result == []


def test_get_completions_events_pos0_agents_and_hosts(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """events <TAB> at position 0 should offer both agents and hosts."""
    data = CompletionCacheData(
        commands=["events"],
        positional_completions={"events": [["agent_names", "host_names"], []]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent"], host_names=["saturn"])
    set_comp_env("mngr events ", "2")

    result = _get_completions()

    assert "my-agent" in result
    assert "saturn" in result


def test_get_completions_destroy_variadic_repeats(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """destroy a1 a2 <TAB> with variadic nargs should still offer agents (last entry repeats)."""
    data = CompletionCacheData(
        commands=["destroy"],
        positional_nargs_by_command={"destroy": None},
        positional_completions={"destroy": [["agent_names"]]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["a1", "a2", "a3"])
    set_comp_env("mngr destroy a1 a2 ", "4")

    result = _get_completions()

    assert "a1" in result
    assert "a2" in result
    assert "a3" in result


def test_get_completions_rename_pos1_freeform(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """rename my-agent <TAB> at position 1 should offer nothing (freeform new name)."""
    data = CompletionCacheData(
        commands=["rename"],
        positional_nargs_by_command={"rename": 2},
        positional_completions={"rename": [["agent_names"], []]},
    )
    _write_command_cache(completion_cache_dir, data)
    _write_discovery_events(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mngr rename my-agent ", "3")

    result = _get_completions()

    assert result == []


# =============================================================================
# Config value completion tests
# =============================================================================


def test_get_completions_config_set_pos1_enum_values(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set logging.console_level <TAB> should offer enum values."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={
            "logging.console_level": ["TRACE", "DEBUG", "BUILD", "INFO", "WARN", "ERROR", "NONE"],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set logging.console_level ", "4")

    result = _get_completions()

    assert "TRACE" in result
    assert "DEBUG" in result
    assert "INFO" in result
    assert "NONE" in result


def test_get_completions_config_set_pos1_enum_values_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set logging.console_level D<TAB> should filter to matching enum values."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["prefix", "logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={
            "logging.console_level": ["TRACE", "DEBUG", "BUILD", "INFO", "WARN", "ERROR", "NONE"],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set logging.console_level D", "4")

    result = _get_completions()

    assert result == ["DEBUG"]


def test_get_completions_config_set_pos1_bool_values(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set headless <TAB> should offer true/false."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["headless", "prefix"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={"headless": ["true", "false"]},
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set headless ", "4")

    result = _get_completions()

    assert result == ["true", "false"]


def test_get_completions_config_set_pos1_with_interleaved_options(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set --scope user logging.console_level <TAB> should still offer enum values."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        options_by_command={"config.set": ["--scope"]},
        flag_options_by_command={},
        config_keys=["logging.console_level"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={
            "logging.console_level": ["TRACE", "DEBUG", "BUILD", "INFO", "WARN", "ERROR", "NONE"],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set --scope user logging.console_level ", "6")

    result = _get_completions()

    assert "TRACE" in result
    assert "DEBUG" in result


def test_get_completions_config_set_dynamic_plugin_key(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set plugins.modal.enabled <TAB> should offer true/false for dynamic dict keys."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["plugins.modal.enabled", "plugins.kanpan.enabled", "headless"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={
            "plugins.modal.enabled": ["true", "false"],
            "plugins.kanpan.enabled": ["true", "false"],
            "headless": ["true", "false"],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set plugins.modal.enabled ", "4")

    result = _get_completions()

    assert result == ["true", "false"]


def test_get_completions_config_set_agent_type_parent_type(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set agent_types.coder.parent_type <TAB> should offer agent type names."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["agent_types.coder.parent_type", "headless"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={
            "agent_types.coder.parent_type": ["claude", "codex", "coder"],
            "headless": ["true", "false"],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set agent_types.coder.parent_type ", "4")

    result = _get_completions()

    assert "claude" in result
    assert "codex" in result
    assert "coder" in result


def test_get_completions_config_set_provider_backend(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """config set providers.modal.backend <TAB> should offer provider backend names."""
    data = CompletionCacheData(
        commands=["config"],
        subcommand_by_command={"config": ["set"]},
        config_keys=["providers.modal.backend", "headless"],
        positional_nargs_by_command={"config.set": 2},
        positional_completions={"config.set": [["config_keys"], ["config_value_for_key"]]},
        config_value_choices={
            "providers.modal.backend": ["docker", "local", "modal", "ssh"],
            "headless": ["true", "false"],
        },
    )
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mngr config set providers.modal.backend ", "4")

    result = _get_completions()

    assert "docker" in result
    assert "local" in result
    assert "modal" in result
    assert "ssh" in result
