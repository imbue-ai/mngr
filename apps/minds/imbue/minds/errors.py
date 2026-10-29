import click

from imbue.minds.mngr_settings.errors import MindsSettingsError


class MindError(click.ClickException):
    """Base exception for all Imbue Studio errors.

    Inherits from click.ClickException so that Imbue Studio errors are
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

    def __init__(self, message: str, *, error_class: str | None = None, output_tail: str | None = None) -> None:
        super().__init__(message)
        # mngr's exception class name, parsed from a structured JSONL ``error``
        # event when available (e.g. ``FastPathUnavailableError``). Lets callers
        # branch on the failure *type* without matching human-formatted text.
        self.error_class = error_class
        # Bounded tails of whatever the subprocess wrote to stdout/stderr, kept
        # off the message so user-facing surfaces stay short and the text-matching
        # consumers of ``str(exc)`` see only mngr's verdict. The message names the
        # failure; this is the step-by-step record a failure nobody anticipated is
        # diagnosed from, and it is what the error log record carries.
        self.output_tail = output_tail


class MngrCommandTimeoutError(MngrCommandError):
    """Raised when an mngr CLI command did not finish within its timeout.

    A distinct subclass so callers can tell "the command ran and failed" (still
    a ``MngrCommandError``, with a body to inspect) apart from "the command
    never completed". The difference matters wherever a failure is read as
    evidence about the host: a command that ran and failed says something about
    the host it reached, while one that never returned says only that the
    provider or the network did not answer in time.

    A killed command has no verdict of its own, so ``output_tail`` is the only
    record of which step it died in.
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
    """Raised when Imbue Studio config cannot be parsed or validated."""

    ...


class SettingValueError(MindError, ValueError):
    """Raised when a declared setting resolves to a value of the wrong shape."""

    ...


class NaiveTimestampError(MindError, ValueError):
    """Raised when a timezone-aware datetime is required but a naive one was passed."""

    ...


class WorkspaceNameInUseError(MindError, ValueError):
    """Raised when a create targets a workspace name an in-flight create attempt already holds.

    The mngr-side ``HostNameConflictError`` pre-flight only sees hosts that
    already exist; this guards the window before the provider reserves the
    name, where two concurrent Imbue Studio create attempts could otherwise race.
    """

    ...


class PendingCreateAttemptStoreError(MindError):
    """Raised when a pending-create-attempt record cannot be written or deleted."""

    ...


class WorkspaceFactsCacheError(MindError):
    """Raised when a per-workspace facts cache record cannot be written."""

    ...


class PendingRequestsUnavailableError(MindError):
    """Raised when a verdict must be recorded but no pending-requests view is configured."""

    ...


class DeployLifecycleConfigError(MindError, ValueError):
    """Raised when a deploy lifecycle config combination is invalid."""

    ...


class OriginsConfigError(MindError, ValueError):
    """Raised when a deploy.toml ``[origins]`` block is invalid.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure
    when raised inside a model validator.
    """

    ...


class ContentDomainError(MindError, ValueError):
    """Raised when a deploy.toml ``content_domain`` is not dot-joined lowercase DNS labels.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure
    when raised while validating the field.
    """

    ...


class ManagementPlaneConfigError(MindError, ValueError):
    """Raised when a deploy.toml ``[management_plane]`` table is invalid.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure
    when raised inside a model validator.
    """

    ...


class SshCaConfigError(MindError, ValueError):
    """Raised when a deploy.toml ``[ssh_ca]`` block does not hold an OpenSSH public key line.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure
    when raised inside a model validator.
    """

    ...


class WebTemplateRefRequiredError(MindError):
    """Raised when a dev-tier deploy with web workspaces enabled has no explicit ``MINDS_WEB_TEMPLATE_REF``."""

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


class DeviceIdError(MindError):
    """Raised when this install's device id file cannot be read, created, or validated."""

    ...


class WorkspaceSyncError(MindError):
    """Raised when a workspace-record sync (push/pull/reconcile) operation fails."""

    ...


class WorkspaceRecordLeaseActiveError(WorkspaceSyncError):
    """Raised when a record cannot be removed because its cloud workspace still holds a pool lease.

    The connector is tombstone-first: destroying the workspace is what releases
    the lease and retires the record, so remove-from-list is refused while the
    machine is live.
    """

    ...


class WorkspaceRecordTooNewError(WorkspaceSyncError):
    """Raised when a state-changing operation targets a record whose record_format postdates this app.

    The record was written by a newer app version, so modifying it here could
    corrupt semantics this version cannot see. The record stays readable; the
    remedy is updating the app.
    """

    ...


class AccountDocumentTooNewError(WorkspaceSyncError):
    """Raised when a state-changing operation targets an account document written by a newer app version.

    The document stays readable; the remedy is updating the app. Each document
    kind raises its own subclass so route-level handlers keep their messages.
    """

    ...


class AccountDocumentRemovedElsewhereError(WorkspaceSyncError):
    """Raised when a push would rebase an active account document onto a tombstone another device wrote.

    The removal wins: resurrecting the document would bring it back on every
    device after one of them deliberately removed it.
    """

    ...


class SettingsRecordTooNewError(AccountDocumentTooNewError):
    """Raised when a write targets an account settings record whose settings_format postdates this app."""

    ...


class ConnectionRequiresMasterPasswordError(WorkspaceSyncError):
    """Raised when a connection would be added to an account that cannot encrypt it here.

    Synced connections ride the account's DEK, so the account must be unlocked
    on this device and have a non-empty master password; the local profile is
    the alternative for an account that has neither.
    """

    ...


class ConnectionNotFoundError(WorkspaceSyncError):
    """Raised when a connection id or instance name matches no active connection."""

    ...


class ConnectionTooNewError(AccountDocumentTooNewError):
    """Raised when a state-changing operation targets a connection written by a newer app version."""

    ...


class ConnectionRemovedElsewhereError(AccountDocumentRemovedElsewhereError):
    """Raised when a push would rebase an active connection onto a tombstone another device wrote.

    The removal wins: resurrecting the document would bring the connection
    back on every device after one of them deliberately removed it.
    """

    ...


class ConnectionUpdateInProgressError(WorkspaceSyncError):
    """Raised when a removal or another update targets a connection whose new credentials are still being validated.

    The update route settles such a row (clears or restores its rollback)
    when the validation ends; a writer that changed it meanwhile would
    either be undone by that settlement or carry the rollback into a row
    that must never have one (a tombstone).
    """

    ...


class ConnectionOperationInProgressError(WorkspaceSyncError):
    """Raised when a registration or credential update is started on a connection that already has one running."""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(f"A registration or update of connection {operation_id!r} is still running")


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
    """Raised when a required external tool (desync, minisign) is missing or errors."""

    ...


class InvalidSha256HexError(LimaImageError, ValueError):
    """Raised when a string is not a valid lowercase hex SHA-256 digest.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure when
    raised from the ``Sha256Hex`` primitive's constructor.
    """

    ...


class FolderSyncError(MindError):
    """Raised when a folder sync cannot be started or stopped."""

    ...


class FolderSyncStoreError(MindError):
    """Raised when the record of which folders to keep synced cannot be written."""

    ...


class AccountsIndexUnreadableError(MindsSettingsError):
    """Raised when the plugin's ``accounts.json`` index exists but cannot be read as a signed-in account list.

    Distinct from an absent index (no account was ever signed in): a caller
    deriving the provider set must not treat an unreadable listing as an
    empty one, since writing it would delete every account and connection block.
    """


class SnapshotContainerStatesError(MindError):
    """Raised when the container states a snapshot sandbox was resumed into cannot be read."""

    ...


class ProgressSeedError(MindError, ValueError):
    """Raised when the shipped create-progress seed table is malformed."""


class WorkspaceMessageTypeError(MindError, ValueError):
    """Raised when a workspace message type is not a lowercase ``prefix:kebab-name``, or claims the shell's ``shell:``.

    Subclasses ``ValueError`` so pydantic treats it as a validation failure
    when raised while validating the field.
    """

    ...
