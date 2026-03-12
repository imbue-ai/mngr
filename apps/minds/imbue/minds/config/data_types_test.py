from pathlib import Path

from imbue.minds.config.data_types import MindPaths
from imbue.mng.primitives import AgentId


def test_mind_paths_mind_dir_uses_agent_id(tmp_path: Path) -> None:
    """Verify mind_dir incorporates the agent_id into the path."""
    paths = MindPaths(data_dir=tmp_path)
    agent_id = AgentId()

    result = paths.mind_dir(agent_id)
    assert result.parent == tmp_path
    assert str(agent_id) in str(result)
