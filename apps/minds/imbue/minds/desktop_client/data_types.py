"""Frozen domain objects for the desktop client."""

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class RemoteWorkspaceKind(LowerCaseStrEnum):
    """Why a synced workspace record renders as a remote tile instead of a live row.

    The lowercase values are the wire strings the landing page branches on.
    """

    # Hosted by another Imbue Studio install (a docker / lima machine on another device).
    OTHER_DEVICE = auto()
    # A cloud workspace any signed-in device could reach, that this device's
    # discovery does not currently report (provider signed out, errored, or
    # the host genuinely gone).
    CLOUD = auto()


class BackupAccessState(LowerCaseStrEnum):
    """Whether this device can read a remote workspace's backups right now (lowercase wire values)."""

    # The restic credentials are on this device (its own canonical env, or
    # synced secrets it can decrypt).
    AVAILABLE = auto()
    # Synced credentials exist but the account's master password has not been
    # entered on this device.
    LOCKED = auto()
    # No credentials can reach this device: the device that created the
    # machine never synced them (no master password there, or no backups
    # configured).
    UNAVAILABLE = auto()


class CloudRowKeyState(LowerCaseStrEnum):
    """Why this device cannot open a live cloud row it lists (lowercase wire values).

    A cloud workspace's per-host SSH key arrives through the synced record, so
    a device that has not decrypted the record has nothing to connect with.
    Each state names what the user can do about it.
    """

    # The synced key is here but needs the account's master password.
    LOCKED = auto()
    # The account is unlocked; the key has not landed with a sync yet.
    SYNCING = auto()
    # No key bundle exists anywhere, so no key will ever reach this device.
    UNAVAILABLE = auto()
    # The host refused this device's credential (or its host key could not be
    # verified): discovery reports it UNAUTHENTICATED, and no restart helps.
    REJECTED = auto()


class ShareGatewayState(LowerCaseStrEnum):
    """How the workspace's share gateway reports its bring-up (lowercase wire values).

    Written by the template's share gateway to its status file and read by the
    sharing readiness poll to explain a share that is not live yet.
    """

    # The share stack is up.
    UP = auto()
    # The last attempt failed and the next one is scheduled.
    RETRYING = auto()
    # The connector refused permanently; only a re-share tries again.
    HALTED = auto()


class SharedWorkspaceRecordState(LowerCaseStrEnum):
    """What the account's workspace records say about a share's workspace (lowercase wire values)."""

    # The workspace's record is active: the share is of a workspace that exists.
    ACTIVE = auto()
    # The workspace was destroyed; its share should have been retired with it.
    DESTROYED = auto()
    # No record names the share's workspace or host: nothing can retire it but its owner.
    MISSING = auto()


class ShareRemovalOutcome(LowerCaseStrEnum):
    """How a share was removed from the shared-workspaces page (lowercase wire values)."""

    # The workspace runs on this device, so it was unpublished: materials cleared, then the share deleted.
    STOPPED_SHARING = auto()
    # The share alone was deleted at the connector (the workspace is elsewhere, or gone).
    REMOVED = auto()


class RemoteWorkspaceTile(FrozenModel):
    """A workspace known only from a synced record (not in local discovery), for the landing list."""

    agent_id: str = Field(description="The workspace agent id (drives backup status)")
    name: str = Field(description="Display name from the record")
    accent: str = Field(description="Accent color hex")
    kind: RemoteWorkspaceKind = Field(description="Other-device machine, or a cloud workspace this device cannot see")
    location: str = Field(
        description="Where it lives: the other device's label, or the cloud provider's friendly name for cloud rows"
    )
    host_id: str = Field(description="The record's host id (drives remove-from-list)")
    state: str = Field(
        default="",
        description=(
            "Derived access state for cloud rows: '' (plain), 'locked' (the account's key is not on this device), "
            "'missing_connection' (the row's connection is not held here), 'no_credentials' (its credentials are "
            "unusable here), 'read_only' (a newer app wrote it), 'connecting', 'unreachable', or 'error'"
        ),
    )
    state_detail: str | None = Field(default=None, description="Failure detail for the 'error' state (chip tooltip)")
    backup_access: BackupAccessState = Field(description="Whether this device can read the workspace's backups now")


class WorkspaceProbeOutcome(FrozenModel):
    """What one readiness probe of a workspace's system interface, through the plugin, came back with."""

    status_code: int | None = Field(
        description="The HTTP status the plugin answered with; None when the probe failed before any response"
    )
    failure: str | None = Field(
        default=None,
        description=(
            "The transport failure that produced no response, as the exception's class and message; "
            "None whenever a status arrived"
        ),
    )

    @property
    def is_ready(self) -> bool:
        return self.status_code == 200

    @property
    def summary(self) -> str:
        """The one phrase a log line or a failure message names this outcome by."""
        if self.status_code is not None:
            return f"HTTP {self.status_code}"
        return self.failure or "no response"


class MachineSizeSelection(FrozenModel):
    """The size a user picked for a machine, in the three numbers every sized provider is told; a None dimension is unset."""

    cpu_count: int | None = Field(default=None, description="Whole CPUs, None when not chosen")
    memory_gb: int | None = Field(default=None, description="Memory in whole GB, None when not chosen")
    disk_gb: int | None = Field(default=None, description="Data disk in whole GB, None when not chosen")

    @property
    def is_empty(self) -> bool:
        return self.cpu_count is None and self.memory_gb is None and self.disk_gb is None
