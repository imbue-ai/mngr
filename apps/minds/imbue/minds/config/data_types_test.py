from pathlib import Path

from imbue.minds.config.data_types import MindPaths
from imbue.minds.config.data_types import get_default_data_dir
from imbue.mng.primitives import AgentId


def test_mind_paths_mind_dir_uses_agent_id(tmp_path: Path) -> None:
    """Verify mind_dir incorporates the agent_id into the path."""
    paths = MindPaths(data_dir=tmp_path)
    agent_id = AgentId()

    result = paths.mind_dir(agent_id)
    assert result.parent == tmp_path
    assert str(agent_id) in str(result)


def test_mind_paths_auth_dir_is_under_data_dir(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    assert paths.auth_dir == tmp_path / "auth"


def test_get_default_data_dir_returns_home_minds() -> None:
    result = get_default_data_dir()
    assert result.name == ".minds"
    assert result.parent == Path.home()
