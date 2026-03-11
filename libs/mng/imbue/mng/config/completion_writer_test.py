"""Tests for completion_writer module."""

import json
from pathlib import Path

import click
import pytest

from imbue.mng.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mng.config.completion_cache import get_completion_cache_dir
from imbue.mng.config.completion_writer import flatten_dict_keys
from imbue.mng.config.completion_writer import write_cli_completions_cache
from imbue.mng.config.data_types import MngContext


def test_get_completion_cache_dir_uses_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_completion_cache_dir should use MNG_COMPLETION_CACHE_DIR when set."""
    cache_dir = tmp_path / "custom_cache"
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(cache_dir))
    result = get_completion_cache_dir()
    assert result == cache_dir
    assert cache_dir.exists()


def test_get_completion_cache_dir_falls_back_to_default_host_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """get_completion_cache_dir should use read_default_host_dir when env var is unset."""
    monkeypatch.delenv("MNG_COMPLETION_CACHE_DIR", raising=False)
    monkeypatch.setenv("MNG_HOST_DIR", str(tmp_path / "default_host"))
    result = get_completion_cache_dir()
    assert result == tmp_path / "default_host"
    assert result.exists()


def test_write_cli_completions_cache_handles_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """write_cli_completions_cache should silently handle OSError."""
    # Point to a read-only directory so the atomic_write fails
    read_only_dir = tmp_path / "readonly"
    read_only_dir.mkdir()
    read_only_dir.chmod(0o444)
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(read_only_dir))

    # Create a minimal click.Group
    group = click.Group(name="test", commands={"hello": click.Command("hello")})

    try:
        # Should not raise despite filesystem error
        write_cli_completions_cache(cli_group=group)
    finally:
        read_only_dir.chmod(0o755)
    # Verify the cache file was NOT created (write failed silently).
    # Check after restoring permissions so Path.exists() doesn't raise PermissionError.
    assert not (read_only_dir / COMPLETION_CACHE_FILENAME).exists()


def test_write_cli_completions_cache_writes_valid_json(completion_cache_dir: Path) -> None:
    """write_cli_completions_cache should write valid JSON with expected structure."""
    group = click.Group(
        name="test",
        commands={
            "list": click.Command("list", params=[click.Option(["--format"], type=click.Choice(["json", "human"]))]),
            "create": click.Command("create", params=[click.Option(["--verbose", "-v"], is_flag=True)]),
        },
    )

    write_cli_completions_cache(cli_group=group)
    cache_path = completion_cache_dir / COMPLETION_CACHE_FILENAME
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert "commands" in data
    assert "create" in data["commands"]
    assert "list" in data["commands"]


def test_write_cli_completions_cache_includes_git_branch_options(completion_cache_dir: Path) -> None:
    """write_cli_completions_cache should include git_branch_options for the create command."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command("create", params=[click.Option(["--branch"])]),
        },
    )

    write_cli_completions_cache(cli_group=group)
    cache_path = completion_cache_dir / COMPLETION_CACHE_FILENAME
    data = json.loads(cache_path.read_text())
    assert "git_branch_options" in data
    assert "create.--branch" in data["git_branch_options"]


def _read_cache(cache_dir: Path) -> dict:
    """Read and return the completion cache data."""
    return json.loads((cache_dir / COMPLETION_CACHE_FILENAME).read_text())


def test_write_cli_completions_cache_includes_host_name_options(completion_cache_dir: Path) -> None:
    """Cache should include host_name_options for create --host and --target-host."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command(
                "create",
                params=[
                    click.Option(["--host"]),
                    click.Option(["--target-host"]),
                ],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert "create.--host" in data["host_name_options"]
    assert "create.--target-host" in data["host_name_options"]


def test_write_cli_completions_cache_includes_positional_completions_for_events(
    completion_cache_dir: Path,
) -> None:
    """Cache should include per-position positional_completions for the events command."""
    group = click.Group(
        name="test",
        commands={
            "events": click.Command("events"),
            "list": click.Command("list"),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert "events" in data["positional_completions"]
    assert data["positional_completions"]["events"] == [["agent_names", "host_names"], []]
    assert "list" not in data["positional_completions"]


def test_write_cli_completions_cache_includes_plugin_name_options(completion_cache_dir: Path) -> None:
    """Cache should detect --plugin/--enable-plugin/--disable-plugin as plugin name options."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command(
                "create",
                params=[
                    click.Option(["--plugin"]),
                    click.Option(["--name"]),
                ],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert "create.--plugin" in data["plugin_name_options"]


def test_write_cli_completions_cache_includes_positional_completions_for_plugin(
    completion_cache_dir: Path,
) -> None:
    """Cache should include positional_completions for plugin enable/disable subcommands."""
    plugin_group = click.Group(
        name="plugin",
        commands={
            "enable": click.Command("enable"),
            "disable": click.Command("disable"),
            "list": click.Command("list"),
        },
    )
    group = click.Group(name="test", commands={"plugin": plugin_group})

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_completions"]["plugin.enable"] == [["plugin_names"]]
    assert data["positional_completions"]["plugin.disable"] == [["plugin_names"]]


def test_write_cli_completions_cache_includes_positional_completions_for_config(
    completion_cache_dir: Path,
) -> None:
    """Cache should include positional_completions for config get/set/unset subcommands."""
    config_group = click.Group(
        name="config",
        commands={
            "get": click.Command("get"),
            "set": click.Command("set"),
            "unset": click.Command("unset"),
            "list": click.Command("list"),
        },
    )
    group = click.Group(name="test", commands={"config": config_group})

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_completions"]["config.get"] == [["config_keys"]]
    assert data["positional_completions"]["config.set"] == [["config_keys"], []]
    assert data["positional_completions"]["config.unset"] == [["config_keys"]]


def test_write_cli_completions_cache_with_mng_ctx(
    completion_cache_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """When mng_ctx is provided, dynamic completions should be injected into the cache."""
    group = click.Group(
        name="test",
        commands={
            "create": click.Command(
                "create",
                params=[
                    click.Option(["--type"]),
                    click.Option(["--template"]),
                    click.Option(["--in"]),
                ],
            ),
            "list": click.Command(
                "list",
                params=[
                    click.Option(["--provider"]),
                ],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group, mng_ctx=temp_mng_ctx)
    data = _read_cache(completion_cache_dir)

    # Agent types include at least the built-in registered types
    assert "create.--type" in data["option_choices"]
    assert len(data["option_choices"]["create.--type"]) > 0
    # Provider names always include "local"
    assert "local" in data["option_choices"]["create.--in"]
    assert "local" in data["option_choices"]["list.--provider"]
    # Config keys are flattened from the config model
    assert len(data["config_keys"]) > 0
    # Plugin names come from the plugin manager
    assert isinstance(data["plugin_names"], list)


def test_write_cli_completions_cache_no_mng_ctx(completion_cache_dir: Path) -> None:
    """When mng_ctx is None, plugin_names and config_keys should be empty."""
    group = click.Group(
        name="test",
        commands={"list": click.Command("list")},
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["plugin_names"] == []
    assert data["config_keys"] == []


# =============================================================================
# flatten_dict_keys tests
# =============================================================================


def test_flatten_dict_keys_flat() -> None:
    data = {"a": 1, "b": 2, "c": 3}
    assert flatten_dict_keys(data) == ["a", "b", "c"]


def test_flatten_dict_keys_nested() -> None:
    data = {"logging": {"console_level": "INFO", "file_level": "DEBUG"}, "prefix": "mng"}
    result = flatten_dict_keys(data)
    assert result == ["logging.console_level", "logging.file_level", "prefix"]


def test_flatten_dict_keys_empty() -> None:
    assert flatten_dict_keys({}) == []


# =============================================================================
# positional_nargs_by_command tests
# =============================================================================


def test_positional_nargs_fixed_args(completion_cache_dir: Path) -> None:
    """Commands with fixed positional args should have their nargs summed."""
    config_group = click.Group(
        name="config",
        commands={
            "set": click.Command(
                "set",
                params=[
                    click.Argument(["key"]),
                    click.Argument(["value"]),
                ],
            ),
            "get": click.Command(
                "get",
                params=[
                    click.Argument(["key"]),
                ],
            ),
            "list": click.Command("list"),
        },
    )
    group = click.Group(name="test", commands={"config": config_group})

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    nargs = data["positional_nargs_by_command"]
    assert nargs["config.set"] == 2
    assert nargs["config.get"] == 1
    assert nargs["config.list"] == 0


def test_positional_nargs_unlimited(completion_cache_dir: Path) -> None:
    """Commands with nargs=-1 should have None in positional_nargs_by_command."""
    group = click.Group(
        name="test",
        commands={
            "destroy": click.Command(
                "destroy",
                params=[click.Argument(["agents"], nargs=-1)],
            ),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_nargs_by_command"]["destroy"] is None


def test_positional_nargs_simple_command(completion_cache_dir: Path) -> None:
    """Simple commands (not groups) should have nargs tracked."""
    group = click.Group(
        name="test",
        commands={
            "connect": click.Command(
                "connect",
                params=[click.Argument(["agent"])],
            ),
            "list": click.Command("list"),
        },
    )

    write_cli_completions_cache(cli_group=group)
    data = _read_cache(completion_cache_dir)

    assert data["positional_nargs_by_command"]["connect"] == 1
    assert data["positional_nargs_by_command"]["list"] == 0
