"""The two choices a folder sync offers, as their own module.

Both :mod:`folder_sync`, which runs a sync, and :mod:`folder_sync_store`, which
remembers that the user wanted one, need to name a direction and a conflict
rule. Keeping them here rather than in either module is what lets the store be
about storage and the sync be about syncing, without one importing the other.
"""

from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum


class FolderSyncDirection(UpperCaseStrEnum):
    """Which way files move in a sync.

    Not a choice the user makes any more: it follows the access the agent was
    granted, because the two say the same thing. Read means the agent may look
    at the folder, so changes travel from this computer to the workspace; read
    and write means it may change the folder too, so they travel both ways.

    There is deliberately no workspace-to-this-computer-only direction. A synced
    folder is always seeded from a folder on this computer, so a sync that only
    ever copied the other way would begin by emptying it.
    """

    BOTH = auto()
    TO_WORKSPACE = auto()


class FolderSyncConflict(UpperCaseStrEnum):
    """Which side wins when a two-way sync finds the same file changed on both."""

    NEWER = auto()
    THIS_COMPUTER = auto()
    WORKSPACE = auto()


class FolderSyncActivity(UpperCaseStrEnum):
    """What has become of a folder the user once asked to keep synced.

    A remembered sync is not simply on or off, because turning it off leaves
    the machine holding a copy that the user may want back, may want gone, and
    may in the meantime have lost -- an agent can do what it likes to its own
    filesystem, so this is what Minds last did, not what is certainly there.
    """

    ACTIVE = auto()
    # Not syncing; the copy was set aside under ~/inactive_synced_folders.
    INACTIVE = auto()
    # Not syncing, and the copy that was set aside has been deleted.
    DISCARDED = auto()
