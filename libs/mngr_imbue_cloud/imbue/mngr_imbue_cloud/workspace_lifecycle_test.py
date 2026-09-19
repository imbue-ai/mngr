from imbue.mngr_imbue_cloud.wire_types import WorkspaceStopKind
from imbue.mngr_imbue_cloud.workspace_lifecycle import is_owner_startable


def test_is_owner_startable_treats_holds_and_unknown_kinds_as_not_the_owners() -> None:
    assert is_owner_startable(None) is True
    assert is_owner_startable(WorkspaceStopKind.OWNER) is True
    assert is_owner_startable(WorkspaceStopKind.IDLE) is True
    assert is_owner_startable(WorkspaceStopKind.MAINTENANCE) is False
    assert is_owner_startable(WorkspaceStopKind.SUSPENSION) is False
    assert is_owner_startable(WorkspaceStopKind.RETIRED) is False
    assert is_owner_startable(WorkspaceStopKind.UNKNOWN) is False
