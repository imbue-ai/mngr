"""The vocabulary of a folder sync, as its own module.

Both :mod:`folder_sync`, which runs a sync, and :mod:`folder_sync_store`, which
remembers that the user wanted one, need to name a direction and a conflict
rule. Keeping them here rather than in either module is what lets the store be
about storage and the sync be about syncing, without one importing the other.
The state a sync is in lives here too, so the API models that report it can
name it without importing the machinery that runs one.
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
    filesystem, so this is what Imbue Studio last did, not what is certainly there.
    """

    ACTIVE = auto()
    # Not syncing; the copy was set aside under ~/inactive_synced_folders.
    INACTIVE = auto()
    # Not syncing, and the copy that was set aside has been deleted.
    DISCARDED = auto()


class FolderSyncState(UpperCaseStrEnum):
    """Where one folder actually is right now, as opposed to where the user asked it to be.

    The user sets the destination -- see :class:`FolderSyncActivity`, which is
    what a click writes -- and this is the app's progress towards it. Every
    move between destinations crosses the network, so each has a state for
    being under way, and a click never waits for one.

    STARTING covers everything between spawning ``mngr pair`` and unison
    reporting for duty: resolving the workspace, moving a set-aside copy back,
    installing a usable unison on a remote host, the SSH handshake. RESTARTING
    is the same work for a sync whose settings changed under it -- told apart
    because the files are already on the machine, so the wait is a handshake
    rather than a first copy, and a user who has just changed a dropdown is
    owed the difference. After that a sync is SYNCING until unison's first
    pass has compared the two sides and left them level, and then alternates
    between SYNCING (moving bytes right now) and SYNCED (level, and watching
    for the next change) -- which is where it spends nearly all its time. DEACTIVATING and
    DISCARDING are the same idea in the other direction: the sync has stopped
    and the machine is being told what to do with its copy.

    Three states are terminal, and they are worth keeping apart because the
    user is owed a different thing by each. STOPPED means it ended because it
    was asked to -- the user unticked the box, or Imbue Studio quit -- and is where a
    settled inactive folder rests, so the unticked checkbox says everything and
    the row needs no badge. FAILED means it ended on its own, and carries the
    reason. UNKNOWN is neither: the folder is one the user still wants synced,
    nothing is running, and Imbue Studio cannot say why. That is not a resting place,
    it is a disagreement between what the app says and what it is doing -- and
    it used to be spelled STOPPED, which made it invisible.
    """

    STARTING = auto()
    RESTARTING = auto()
    SYNCING = auto()
    SYNCED = auto()
    DEACTIVATING = auto()
    DISCARDING = auto()
    STOPPED = auto()
    UNKNOWN = auto()
    FAILED = auto()
