"""Tests for completion_writer module."""

import json
from pathlib import Path

import click
import pytest

from imbue.mng.config.completion_writer import COMMAND_COMPLETIONS_CACHE_FILENAME
from imbue.mng.config.completion_writer import get_completion_cache_dir
from imbue.mng.config.completion_writer import write_cli_completions_cache


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
        write_cli_completions_cache(group)
    finally:
        read_only_dir.chmod(0o755)
    # Verify the cache file was NOT created (write failed silently).
    # Check after restoring permissions so Path.exists() doesn't raise PermissionError.
    assert not (read_only_dir / COMMAND_COMPLETIONS_CACHE_FILENAME).exists()


def test_write_cli_completions_cache_writes_valid_json(completion_cache_dir: Path) -> None:
    """write_cli_completions_cache should write valid JSON with expected structure."""
    group = click.Group(
        name="test",
        commands={
            "list": click.Command("list", params=[click.Option(["--format"], type=click.Choice(["json", "human"]))]),
            "create": click.Command("create", params=[click.Option(["--verbose", "-v"], is_flag=True)]),
        },
    )

    write_cli_completions_cache(group)
    cache_path = completion_cache_dir / COMMAND_COMPLETIONS_CACHE_FILENAME
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
            "create": click.Command("create", params=[click.Option(["--base-branch"])]),
        },
    )

    write_cli_completions_cache(group)
    cache_path = completion_cache_dir / COMMAND_COMPLETIONS_CACHE_FILENAME
    data = json.loads(cache_path.read_text())
    assert "git_branch_options" in data
    assert "create.--base-branch" in data["git_branch_options"]
