"""Tests for the mngr_uncapped_claude error hierarchy."""

import click
import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_uncapped_claude.errors import InvalidStreamJsonInputError
from imbue.mngr_uncapped_claude.errors import MissingPromptError
from imbue.mngr_uncapped_claude.errors import UncappedClaudeError
from imbue.mngr_uncapped_claude.errors import UnsupportedClaudeFlagError


@pytest.mark.parametrize(
    "error_subclass",
    [UncappedClaudeError, UnsupportedClaudeFlagError, InvalidStreamJsonInputError, MissingPromptError],
    ids=lambda c: c.__name__,
)
def test_uncapped_claude_errors_are_mngr_errors(error_subclass: type) -> None:
    """The plugin's base error and its subclasses are all MngrError (and ClickException) subclasses.

    The base used to inherit only from BaseMngrError while its subclasses were already
    user-facing via UserInputError; the consolidation makes the whole tree consistent.
    """
    assert issubclass(error_subclass, MngrError)
    assert issubclass(error_subclass, click.ClickException)
