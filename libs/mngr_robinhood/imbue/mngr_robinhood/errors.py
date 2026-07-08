from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError


class RobinhoodError(MngrError):
    """Base exception for the mngr_robinhood plugin."""


class UnsupportedClaudeFlagError(RobinhoodError, UserInputError):
    """Raised when the user passes a claude flag that v1 does not support."""


class InvalidStreamJsonInputError(RobinhoodError, UserInputError):
    """Raised when a stream-json input line does not match the supported shape."""


class MissingPromptError(RobinhoodError, UserInputError):
    """Raised when neither a positional prompt nor stdin content is provided."""


class AgentSdkNotImplementedError(RobinhoodError, NotImplementedError):
    """Raised by mngr-backed Agent SDK surfaces the transport cannot support (e.g. fork_session)."""
