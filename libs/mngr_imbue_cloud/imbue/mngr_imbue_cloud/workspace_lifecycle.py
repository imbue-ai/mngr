"""Pure lifecycle rules every imbue_cloud client shares (specs/workspace-stop-kinds.md)."""

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStopKind


@pure
def is_owner_startable(stop_kind: WorkspaceStopKind | None) -> bool:
    """Whether the machine's owner may end this stop with a start.

    ``None`` (running, a legacy stop, or an old connector) and ``owner`` /
    ``idle`` are the owner's to end; ``maintenance`` and ``suspension`` are
    operator holds, ``retired`` is final (nobody starts it), and a kind this
    client does not recognize is treated as a hold (shown but not actionable,
    per the remote-compatibility invariants).
    """
    return stop_kind in (None, WorkspaceStopKind.OWNER, WorkspaceStopKind.IDLE)
