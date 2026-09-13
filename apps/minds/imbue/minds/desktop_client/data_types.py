"""Frozen domain objects for the desktop client."""

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class RemoteWorkspaceKind(LowerCaseStrEnum):
    """Why a synced workspace record renders as a remote tile instead of a live row.

    The lowercase values are the wire strings the landing page branches on.
    """

    # Hosted by another minds install (a docker / lima machine on another device).
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
            "Derived access state for cloud rows: '' (plain), 'signed_out', 'connecting', 'unreachable', or 'error'"
        ),
    )
    state_detail: str | None = Field(default=None, description="Failure detail for the 'error' state (chip tooltip)")
    backup_access: BackupAccessState = Field(description="Whether this device can read the workspace's backups now")
