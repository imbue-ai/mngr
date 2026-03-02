"""Tests for completion_writer module."""

import json
from pathlib import Path

import click
import pytest

from imbue.mng.config.completion_writer import AGENT_COMPLETIONS_CACHE_FILENAME
from imbue.mng.config.completion_writer import COMMAND_COMPLETIONS_CACHE_FILENAME
from imbue.mng.config.completion_writer import get_completion_cache_dir
from imbue.mng.config.completion_writer import write_agent_names_cache
from imbue.mng.config.completion_writer import write_cli_completions_cache


def test_get_completion_cache_dir_uses_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_completion_cache_dir should use MNG_COMPLETION_CACHE_DIR when set."""
    cache_dir = tmp_path / "custom_cache"
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(cache_dir))
    result = get_completion_cache_dir()
    assert result == cache_dir
    assert cache_dir.exists()


def test_write_agent_names_cache_writes_json(tmp_path: Path) -> None:
    """write_agent_names_cache should write a JSON file with sorted unique names."""
    write_agent_names_cache(tmp_path, ["beta", "alpha", "alpha"])
    cache_path = tmp_path / AGENT_COMPLETIONS_CACHE_FILENAME
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["names"] == ["alpha", "beta"]
    assert "updated_at" in data


def test_write_agent_names_cache_handles_oserror(tmp_path: Path) -> None:
    """write_agent_names_cache should silently handle OSError (read-only dir)."""
    # Create a read-only directory path that doesn't exist (will fail to write)
    read_only_dir = tmp_path / "readonly"
    read_only_dir.mkdir()
    read_only_dir.chmod(0o444)
    try:
        # This should not raise -- OSError is caught internally
        write_agent_names_cache(read_only_dir, ["agent1"])
    finally:
        read_only_dir.chmod(0o755)


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


def test_write_cli_completions_cache_writes_valid_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """write_cli_completions_cache should write valid JSON with expected structure."""
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(tmp_path))

    group = click.Group(
        name="test",
        commands={
            "list": click.Command("list", params=[click.Option(["--format"], type=click.Choice(["json", "human"]))]),
            "create": click.Command("create", params=[click.Option(["--verbose", "-v"], is_flag=True)]),
        },
    )

    write_cli_completions_cache(group)
    cache_path = tmp_path / COMMAND_COMPLETIONS_CACHE_FILENAME
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert "commands" in data
    assert "create" in data["commands"]
    assert "list" in data["commands"]
