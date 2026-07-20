import click


class MindError(click.ClickException):
    """Base exception for all minds errors.

    Inherits from click.ClickException so that minds errors are
    automatically formatted and displayed by click without needing
    manual re-raising as ClickException at every call site.
    """

    ...


class SigningKeyError(MindError):
    """Raised when the cookie signing key cannot be loaded or created."""

    ...


class GitCloneError(MindError):
    """Raised when git clone fails."""

    ...


class GitOperationError(MindError):
    """Raised when a git operation (other than clone) fails."""

    ...


class MngrCommandError(MindError):
    """Raised when an mngr CLI command fails (timed out, exited nonzero, or could not be launched)."""

    def __init__(self, message: str, *, error_class: str | None = None) -> None:
        super().__init__(message)
        # mngr's exception class name, parsed from a structured JSONL ``error``
        # event when available (e.g. ``FastPathUnavailableError``). Lets callers
        # branch on the failure *type* without matching human-formatted text.
        self.error_class = error_class


class MngrCommandTimeoutError(MngrCommandError):
    """Raised when an mngr CLI command did not finish within its timeout.

    A distinct subclass so callers can tell "the command ran and failed" (still
    a ``MngrCommandError``, with a body to inspect) apart from "the command
    never completed". The recovery host-health probe keys on this: a listing
    that times out is evidence the provider/network is unreachable, not that the
    host is reachable-but-wedged, so it must not offer a destructive restart.
    """

    ...


class MalformedMngrOutputError(MindError, ValueError):
    """Raised when ``mngr list --format json`` produces output we can't parse.

    The right fix is to track down whichever process is leaking non-JSON to
    stdout (stdout is reserved for JSON data; logs belong on stderr) -- silently
    skipping the bad line would just hide the underlying problem.
    """

    ...


class InvalidJsonBodyError(MindError, ValueError):
    """Raised when a request body is missing or not valid JSON.

    Subclasses ``ValueError`` so the desktop client's request handlers can keep
    catching ``(json.JSONDecodeError, ValueError)`` around body parsing.
    """

    ...


class MindsConfigError(MindError):
    """Raised when minds config cannot be parsed or validated."""

    ...


class DeployLifecycleConfigError(MindError, ValueError):
    """Raised when a deploy lifecycle config combination is invalid."""

    ...


class EnvelopeStreamConsumerError(MindError, RuntimeError):
    """Raised when the envelope stream consumer is used out of lifecycle order."""

    ...


class BackupProvisioningError(MindError):
    """Raised when configuring restic backups for a workspace fails."""

    ...


class SyncCryptoError(MindError):
    """Raised when a workspace-sync DEK / key-bundle file operation fails."""

    ...


class WorkspaceSyncError(MindError):
    """Raised when a workspace-record sync (push/pull/reconcile) operation fails."""

    ...


class LimaImageError(MindError):
    """Base exception for the pre-baked Lima image cache."""

    ...


class LimaImageDownloadError(LimaImageError):
    """Raised when downloading/assembling a published image fails (network, disk, desync)."""

    ...


class LimaImageVerificationError(LimaImageError):
    """Raised when a downloaded manifest signature or assembled image hash does not verify.

    An unverified image is never used: this is a hard failure (the create is
    blocked with a retryable error) rather than a fall-through to build-in-VM.
    """

    ...


class LimaImageToolError(LimaImageError):
    """Raised when a required external tool (desync, minisign, qemu-img) is missing or errors."""

    ...


class SpecCorpusRootNotFoundError(MindError, FileNotFoundError):
    """Raised when the behavioral-spec corpus root passed to a scan is not an existing directory."""

    ...


class SpecValidationFailedError(MindError):
    """Raised by ``minds specs validate`` when the corpus has language violations (after listing them)."""

    ...


class SpecListingIncompleteError(MindError):
    """Raised by ``minds specs list`` when some units could not be represented as records.

    The representable records are still emitted on stdout first; this error
    (after per-problem stderr diagnostics) makes the incompleteness visible to
    pipelines via the exit code.
    """

    ...


class SpecTestsRootNotFoundError(MindError, FileNotFoundError):
    """Raised by ``minds specs matrix`` when a ``--tests`` path does not exist."""

    ...


class SpecWitnessCollectionError(MindError):
    """Raised by ``minds specs matrix`` when the inner ``pytest --collect-only`` run cannot collect.

    Exit codes 0 (items collected) and 5 (none collected) are both fine; any
    other exit code, a timeout, or unparseable plugin output is a hard failure
    that carries an excerpt of the pytest output.
    """

    ...


class SpecDanglingWitnessError(MindError):
    """Raised by ``minds specs matrix`` when a ``witnesses`` marker does not name a real spec unit.

    Covers a coordinate matching no corpus unit (dangling) and invalid marker
    usage (no positional coordinate, or a non-string one). The matrix records
    are still emitted on stdout first; this error (after per-marker stderr
    diagnostics) makes the broken links visible via the exit code.
    """

    ...


class InvalidSha256HexError(LimaImageError, ValueError):
    """Raised when a string is not a valid lowercase hex SHA-256 digest.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure when
    raised from the ``Sha256Hex`` primitive's constructor.
    """

    ...
