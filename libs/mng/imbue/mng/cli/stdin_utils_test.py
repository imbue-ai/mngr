from io import StringIO

import pytest

from imbue.mng.cli.stdin_utils import expand_stdin_placeholder
from imbue.mng.errors import UserInputError


class _TtyStringIO(StringIO):
    """A StringIO that reports itself as a TTY."""

    def isatty(self) -> bool:
        return True


# =============================================================================
# expand_stdin_placeholder tests
# =============================================================================


def test_expand_stdin_placeholder_no_dash_returns_identifiers_unchanged() -> None:
    result = expand_stdin_placeholder(("agent1", "agent2"))
    assert result == ["agent1", "agent2"]


def test_expand_stdin_placeholder_empty_tuple_returns_empty_list() -> None:
    result = expand_stdin_placeholder(())
    assert result == []


def test_expand_stdin_placeholder_dash_reads_from_stdin() -> None:
    result = expand_stdin_placeholder(("-",), stdin=StringIO("agent-a\nagent-b\nagent-c\n"))
    assert result == ["agent-a", "agent-b", "agent-c"]


def test_expand_stdin_placeholder_dash_strips_whitespace() -> None:
    result = expand_stdin_placeholder(("-",), stdin=StringIO("  agent-a  \n  agent-b  \n"))
    assert result == ["agent-a", "agent-b"]


def test_expand_stdin_placeholder_dash_skips_empty_lines() -> None:
    result = expand_stdin_placeholder(("-",), stdin=StringIO("agent-a\n\n\nagent-b\n\n"))
    assert result == ["agent-a", "agent-b"]


def test_expand_stdin_placeholder_preserves_non_dash_args_around_dash() -> None:
    result = expand_stdin_placeholder(("before", "-", "after"), stdin=StringIO("stdin-agent\n"))
    assert result == ["before", "stdin-agent", "after"]


def test_expand_stdin_placeholder_multiple_dashes_raises_error() -> None:
    with pytest.raises(UserInputError, match="can only be specified once"):
        expand_stdin_placeholder(("-", "-"))


def test_expand_stdin_placeholder_dash_with_tty_raises_error() -> None:
    with pytest.raises(UserInputError, match="requires piped input"):
        expand_stdin_placeholder(("-",), stdin=_TtyStringIO(""))
