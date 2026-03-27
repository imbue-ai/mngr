"""Unit tests for environment utilities."""

import pytest

from imbue.mngr.utils.env_utils import parse_bool_env
from imbue.mngr.utils.env_utils import parse_env_file


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", "YES"])
def test_parse_bool_env_truthy(value: str) -> None:
    assert parse_bool_env(value) is True


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "No", "anything", "2"])
def test_parse_bool_env_falsy(value: str) -> None:
    assert parse_bool_env(value) is False


def test_parse_env_file_simple() -> None:
    """Test parsing simple env file."""
    content = "FOO=bar\nBAZ=qux"
    env = parse_env_file(content)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_with_comments() -> None:
    """Test parsing env file with comments."""
    content = "# comment\nFOO=bar\n# another comment\nBAZ=qux"
    env = parse_env_file(content)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_with_quotes() -> None:
    """Test parsing env file with quoted values."""
    content = "FOO=\"bar baz\"\nBAR='qux'"
    env = parse_env_file(content)
    assert env == {"FOO": "bar baz", "BAR": "qux"}


def test_parse_env_file_empty_lines() -> None:
    """Test parsing env file with empty lines."""
    content = "FOO=bar\n\nBAZ=qux\n"
    env = parse_env_file(content)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_with_mixed_quote_styles() -> None:
    """Test parsing env file with mixed quote styles."""
    content = "A=\"val1\"\nB='val2'\nC=val3"
    env = parse_env_file(content)
    assert env == {"A": "val1", "B": "val2", "C": "val3"}


def test_parse_env_file_with_spaces_in_unquoted_value() -> None:
    """Test parsing env file with spaces in unquoted value."""
    content = "KEY=value with spaces"
    env = parse_env_file(content)
    assert env["KEY"] == "value with spaces"


def test_parse_env_file_with_multiple_equals_unquoted() -> None:
    """Test parsing env file with multiple equals signs in value."""
    content = "KEY=a=b=c"
    env = parse_env_file(content)
    assert env["KEY"] == "a=b=c"
