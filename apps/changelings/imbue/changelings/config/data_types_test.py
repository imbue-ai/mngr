from pathlib import Path

from imbue.changelings.config.data_types import ChangelingPaths
from imbue.mng.primitives import AgentId


def test_changeling_paths_changeling_dir_uses_agent_id(tmp_path: Path) -> None:
    """Verify changeling_dir incorporates the agent_id into the path."""
    paths = ChangelingPaths(data_dir=tmp_path)
    agent_id = AgentId()

    result = paths.changeling_dir(agent_id)
    assert result.parent == tmp_path
    assert str(agent_id) in str(result)
