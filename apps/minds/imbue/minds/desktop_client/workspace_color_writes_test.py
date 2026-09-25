from imbue.minds.desktop_client.workspace_color_writes import WorkspaceColorWrites
from imbue.mngr.primitives import AgentId


def test_a_newer_pick_replaces_an_older_one_for_the_same_workspace_only() -> None:
    writes = WorkspaceColorWrites()
    workspace = AgentId.generate()
    other_workspace = AgentId.generate()

    first = writes.register_pick(workspace)
    other = writes.register_pick(other_workspace)
    second = writes.register_pick(workspace)

    assert not writes.is_latest_pick(workspace, first)
    assert writes.is_latest_pick(workspace, second)
    assert writes.is_latest_pick(other_workspace, other)
