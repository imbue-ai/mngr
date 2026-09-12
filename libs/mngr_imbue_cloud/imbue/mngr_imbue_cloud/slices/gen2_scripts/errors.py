# This subpackage ships into the remote_service_connector's Modal container as a
# source mount, where ``imbue.mngr`` (and so ``MngrError``) does not exist, so it
# carries its own error base.


class Gen2ScriptError(Exception):
    """Base class for every error the shared gen-2 slice script renderers raise."""


class InvalidSliceOrdinalError(Gen2ScriptError, ValueError):
    """Raised when a slice ordinal lies outside the range of per-slice users a box pre-creates."""


class InvalidMachineSizeError(Gen2ScriptError, ValueError):
    """Raised when a machine or box sizing input is not positive, or leaves the box no budget."""


class MalformedBoxOutputError(Gen2ScriptError):
    """Raised when a box command's output lacks the shape the command we rendered prints."""


class EmptySshCaPublicKeyError(Gen2ScriptError, ValueError):
    """Raised when SSH CA trust files are requested for an empty CA public key."""
