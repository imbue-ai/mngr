from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import UserInputError


class UncappedClaudeError(BaseMngrError):
    """Base exception for the mngr_uncapped_claude plugin."""


class UnsupportedClaudeFlagError(UncappedClaudeError, UserInputError):
    """Raised when the user passes a claude flag that v1 does not support."""


class InvalidStreamJsonInputError(UncappedClaudeError, UserInputError):
    """Raised when a stream-json input line does not match the supported shape."""


class MissingPromptError(UncappedClaudeError, UserInputError):
    """Raised when neither a positional prompt nor stdin content is provided."""
