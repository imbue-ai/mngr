import json
from collections.abc import Callable
from pathlib import Path

import pytest

from imbue.mng.cli.complete import _filter_aliases
from imbue.mng.cli.complete import _get_completions
from imbue.mng.cli.complete import _read_agent_names
from imbue.mng.cli.complete import _read_cache


def _write_command_cache(cache_dir: Path, data: dict[str, object]) -> None:
    """Write a command completions cache file for testing."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ".command_completions.json").write_text(json.dumps(data))


def _write_agent_cache(cache_dir: Path, names: list[str]) -> None:
    """Write an agent completions cache file for testing."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = {"names": names, "updated_at": "2025-01-01T00:00:00+00:00"}
    (cache_dir / ".agent_completions.json").write_text(json.dumps(data))


def _make_cache_data(
    commands: list[str] | None = None,
    aliases: dict[str, str] | None = None,
    subcommand_by_command: dict[str, list[str]] | None = None,
    options_by_command: dict[str, list[str]] | None = None,
    flag_options_by_command: dict[str, list[str]] | None = None,
    option_choices: dict[str, list[str]] | None = None,
    agent_name_arguments: list[str] | None = None,
) -> dict:
    """Build a command completions cache dict with sensible defaults."""
    return {
        "commands": commands or [],
        "aliases": aliases or {},
        "subcommand_by_command": subcommand_by_command or {},
        "options_by_command": options_by_command or {},
        "flag_options_by_command": flag_options_by_command or {},
        "option_choices": option_choices or {},
        "agent_name_arguments": agent_name_arguments or [],
    }


@pytest.fixture
def completion_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temporary completion cache directory via MNG_COMPLETION_CACHE_DIR."""
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(tmp_path))
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
    data = _make_cache_data(commands=["create", "list"])
    _write_command_cache(completion_cache_dir, data)

    result = _read_cache()

    assert result["commands"] == ["create", "list"]


def test_read_cache_returns_empty_dict_when_missing(completion_cache_dir: Path) -> None:
    result = _read_cache()

    assert result == {}


def test_read_cache_returns_empty_dict_for_malformed_json(completion_cache_dir: Path) -> None:
    (completion_cache_dir / ".command_completions.json").write_text("not json {{{")

    result = _read_cache()

    assert result == {}


# =============================================================================
# _read_agent_names tests
# =============================================================================


def test_read_agent_names_returns_names(completion_cache_dir: Path) -> None:
    _write_agent_cache(completion_cache_dir, ["beta", "alpha"])

    result = _read_agent_names()

    assert result == ["alpha", "beta"]


def test_read_agent_names_returns_empty_when_missing(completion_cache_dir: Path) -> None:
    result = _read_agent_names()

    assert result == []


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
    data = _make_cache_data(
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
    data = _make_cache_data(commands=["ask", "create", "list"])
    _write_command_cache(completion_cache_dir, data)
    set_comp_env("mng ", "1")

    result = _get_completions()

    assert result == ["ask", "create", "list"]


def test_get_completions_alias_filtering(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Aliases should be filtered when their canonical name also matches."""
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
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
    data = _make_cache_data(
        commands=["connect", "list"],
        agent_name_arguments=["connect"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng connect ", "2")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_agent_names_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names with a prefix filter."""
    data = _make_cache_data(
        commands=["connect"],
        agent_name_arguments=["connect"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng connect my", "2")

    result = _get_completions()

    assert result == ["my-agent"]


def test_get_completions_no_agent_names_for_non_agent_command(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Commands not in agent_name_arguments should not complete agent names."""
    data = _make_cache_data(
        commands=["list"],
        agent_name_arguments=["connect"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent"])
    set_comp_env("mng list ", "2")

    result = _get_completions()

    assert result == []


def test_get_completions_subcommand_agent_names(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for group subcommands (e.g. mng snapshot create <TAB>)."""
    data = _make_cache_data(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create", "destroy", "list"]},
        agent_name_arguments=["snapshot.create", "snapshot.destroy", "snapshot.list"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng snapshot create ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_subcommand_agent_names_with_prefix(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Completing agent names for group subcommands with a prefix filter."""
    data = _make_cache_data(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create", "destroy", "list"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng snapshot create my", "3")

    result = _get_completions()

    assert result == ["my-agent"]


def test_get_completions_subcommand_no_agent_names_for_non_agent_subcommand(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """Subcommands not in agent_name_arguments should not complete agent names."""
    data = _make_cache_data(
        commands=["config"],
        subcommand_by_command={"config": ["get", "list", "set"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent"])
    set_comp_env("mng config get ", "3")

    result = _get_completions()

    assert result == []


def test_get_completions_alias_resolves_to_canonical(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """An alias typed as the command should resolve to the canonical name for option lookup."""
    data = _make_cache_data(
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
    data = _make_cache_data(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create"]},
        options_by_command={"snapshot.create": ["--all", "--dry-run", "--name"]},
        flag_options_by_command={"snapshot.create": ["--all", "--dry-run", "-a"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent"])
    set_comp_env("mng snapshot create --name ", "4")

    result = _get_completions()

    assert result == []


def test_get_completions_long_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a --long flag (--force), positional candidates should be offered."""
    data = _make_cache_data(
        commands=["destroy"],
        options_by_command={"destroy": ["--all", "--force"]},
        flag_options_by_command={"destroy": ["--all", "--force", "-a", "-f"]},
        agent_name_arguments=["destroy"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng destroy --force ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_short_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a -short flag (-f), positional candidates should be offered."""
    data = _make_cache_data(
        commands=["destroy"],
        options_by_command={"destroy": ["--all", "--force"]},
        flag_options_by_command={"destroy": ["--all", "--force", "-a", "-f"]},
        agent_name_arguments=["destroy"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng destroy -f ", "3")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]


def test_get_completions_subcommand_flag_allows_positional(
    completion_cache_dir: Path,
    set_comp_env: Callable[[str, str], None],
) -> None:
    """After a flag on a subcommand (--dry-run), positional candidates should be offered."""
    data = _make_cache_data(
        commands=["snapshot"],
        subcommand_by_command={"snapshot": ["create"]},
        options_by_command={"snapshot.create": ["--all", "--dry-run", "--name"]},
        flag_options_by_command={"snapshot.create": ["--all", "--dry-run", "-a"]},
        agent_name_arguments=["snapshot.create"],
    )
    _write_command_cache(completion_cache_dir, data)
    _write_agent_cache(completion_cache_dir, ["my-agent", "other-agent"])
    set_comp_env("mng snapshot create --dry-run ", "4")

    result = _get_completions()

    assert result == ["my-agent", "other-agent"]
