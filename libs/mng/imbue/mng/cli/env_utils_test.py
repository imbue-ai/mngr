"""Tests for CLI environment variable utilities."""

import pytest

from imbue.mng.cli.env_utils import resolve_env_vars
from imbue.mng.config.data_types import EnvVar


def test_resolve_env_vars_pass_through_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_env_vars should resolve pass-through env vars from os.environ."""
    monkeypatch.setenv("MY_TOKEN", "secret123")
    monkeypatch.setenv("MY_KEY", "key456")

    result = resolve_env_vars(
        pass_env_var_names=("MY_TOKEN", "MY_KEY"),
        explicit_env_var_strings=(),
    )

    result_dict = {ev.key: ev.value for ev in result}
    assert result_dict["MY_TOKEN"] == "secret123"
    assert result_dict["MY_KEY"] == "key456"


def test_resolve_env_vars_explicit_overrides_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_env_vars should let explicit env vars override pass-through values."""
    monkeypatch.setenv("MY_VAR", "from-environ")

    result = resolve_env_vars(
        pass_env_var_names=("MY_VAR",),
        explicit_env_var_strings=("MY_VAR=explicit-value",),
    )

    result_dict = {ev.key: ev.value for ev in result}
    assert result_dict["MY_VAR"] == "explicit-value"


def test_resolve_env_vars_missing_pass_through_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_env_vars should skip pass-through env vars not present in os.environ."""
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)

    result = resolve_env_vars(
        pass_env_var_names=("NONEXISTENT_VAR",),
        explicit_env_var_strings=(),
    )

    result_dict = {ev.key: ev.value for ev in result}
    assert "NONEXISTENT_VAR" not in result_dict


def test_resolve_env_vars_empty_inputs() -> None:
    """resolve_env_vars should return empty tuple when no vars specified."""
    result = resolve_env_vars(
        pass_env_var_names=(),
        explicit_env_var_strings=(),
    )
    assert result == ()


def test_resolve_env_vars_explicit_only() -> None:
    """resolve_env_vars should handle explicit env vars without any pass-through."""
    result = resolve_env_vars(
        pass_env_var_names=(),
        explicit_env_var_strings=("FOO=bar", "BAZ=qux"),
    )

    result_dict = {ev.key: ev.value for ev in result}
    assert result_dict["FOO"] == "bar"
    assert result_dict["BAZ"] == "qux"


def test_resolve_env_vars_returns_env_var_instances() -> None:
    """resolve_env_vars should return EnvVar instances."""
    result = resolve_env_vars(
        pass_env_var_names=(),
        explicit_env_var_strings=("KEY=value",),
    )
    assert len(result) == 1
    assert isinstance(result[0], EnvVar)
    assert result[0].key == "KEY"
    assert result[0].value == "value"
