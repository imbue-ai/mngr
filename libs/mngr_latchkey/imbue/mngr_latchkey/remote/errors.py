from imbue.mngr_latchkey.core import LatchkeyError


class RemoteGatewayError(LatchkeyError, RuntimeError):
    """Raised when an exchange with a remote machine's latchkey gateway fails."""
