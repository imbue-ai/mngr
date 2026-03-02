from pathlib import Path

from imbue.changelings.config.data_types import ChangelingPaths
from imbue.changelings.config.data_types import get_default_data_dir
from imbue.mng.primitives import AgentId


def test_changeling_paths_auth_dir(tmp_path: Path) -> None:
    paths = ChangelingPaths(data_dir=tmp_path)

    assert paths.auth_dir == tmp_path / "auth"


def test_changeling_paths_changeling_dir(tmp_path: Path) -> None:
    paths = ChangelingPaths(data_dir=tmp_path)
    agent_id = AgentId()

    assert paths.changeling_dir(agent_id) == tmp_path / str(agent_id)


def test_get_default_data_dir_returns_home_based_path() -> None:
    data_dir = get_default_data_dir()

    assert data_dir.name == ".changelings"
    assert data_dir.parent == Path.home()
