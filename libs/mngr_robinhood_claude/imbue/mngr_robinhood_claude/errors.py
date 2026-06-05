from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError


class RobinhoodClaudeError(MngrError):
    """Base exception for the mngr_robinhood_claude plugin."""


class UnsupportedClaudeFlagError(RobinhoodClaudeError, UserInputError):
    """Raised when the user passes a claude flag that v1 does not support."""


class InvalidStreamJsonInputError(RobinhoodClaudeError, UserInputError):
    """Raised when a stream-json input line does not match the supported shape."""


class MissingPromptError(RobinhoodClaudeError, UserInputError):
    """Raised when neither a positional prompt nor stdin content is provided."""
