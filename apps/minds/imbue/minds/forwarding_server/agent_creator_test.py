from pathlib import Path

import pytest

from imbue.minds.config.data_types import MindPaths
from imbue.minds.errors import GitCloneError
from imbue.minds.forwarding_server.agent_creator import AgentCreationStatus
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.agent_creator import DEFAULT_AGENT_TYPE
from imbue.minds.forwarding_server.agent_creator import clone_git_repo
from imbue.minds.forwarding_server.agent_creator import extract_repo_name
from imbue.minds.forwarding_server.agent_creator import generate_login_url
from imbue.minds.forwarding_server.agent_creator import resolve_agent_type
from imbue.minds.primitives import GitUrl
from imbue.minds.testing import init_and_commit_git_repo
from imbue.mng.primitives import AgentId


def test_extract_repo_name_from_https_url() -> None:
    assert extract_repo_name("https://github.com/user/my-repo.git") == "my-repo"


def test_extract_repo_name_from_ssh_url() -> None:
    assert extract_repo_name("git@github.com:user/my-repo.git") == "my-repo"


def test_extract_repo_name_strips_trailing_slash() -> None:
    assert extract_repo_name("https://github.com/user/my-repo/") == "my-repo"


def test_extract_repo_name_without_git_suffix() -> None:
    assert extract_repo_name("https://github.com/user/my-repo") == "my-repo"


def test_extract_repo_name_replaces_special_chars() -> None:
    assert extract_repo_name("https://github.com/user/my repo!test") == "my-repo-test"


def test_extract_repo_name_falls_back_to_mind() -> None:
    assert extract_repo_name("") == "mind"
    assert extract_repo_name("/") == "mind"
    assert extract_repo_name(".git") == "mind"


def test_resolve_agent_type_returns_default_when_no_toml(tmp_path: Path) -> None:
    assert resolve_agent_type(tmp_path) == DEFAULT_AGENT_TYPE


def test_resolve_agent_type_reads_from_minds_toml(tmp_path: Path) -> None:
    (tmp_path / "minds.toml").write_text('agent_type = "custom-type"\n')
    assert resolve_agent_type(tmp_path) == "custom-type"


def test_resolve_agent_type_returns_default_for_toml_without_agent_type(tmp_path: Path) -> None:
    (tmp_path / "minds.toml").write_text("[some_section]\nkey = 1\n")
    assert resolve_agent_type(tmp_path) == DEFAULT_AGENT_TYPE


def test_resolve_agent_type_returns_default_for_malformed_toml(tmp_path: Path) -> None:
    (tmp_path / "minds.toml").write_text("not valid toml {{{")
    assert resolve_agent_type(tmp_path) == DEFAULT_AGENT_TYPE


# -- clone_git_repo tests --


def test_clone_git_repo_clones_local_repo(tmp_path: Path) -> None:
    """Verify clone_git_repo can clone a local git repo."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    init_and_commit_git_repo(source, tmp_path)

    dest = tmp_path / "dest"
    clone_git_repo(GitUrl(str(source)), dest)

    assert dest.exists()
    assert (dest / "hello.txt").read_text() == "hello"


def test_clone_git_repo_raises_on_bad_url(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(GitCloneError, match="git clone failed"):
        clone_git_repo(GitUrl("/nonexistent/path"), dest)


# -- generate_login_url tests --


def test_generate_login_url_creates_url_with_code(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    agent_id = AgentId()

    url = generate_login_url(paths=paths, agent_id=agent_id, forwarding_server_port=8420)

    assert "http://127.0.0.1:8420/login" in url
    assert str(agent_id) in url
    assert "one_time_code=" in url


# -- AgentCreator tests --


def test_agent_creator_get_creation_info_returns_none_for_unknown() -> None:
    creator = AgentCreator(
        paths=MindPaths(data_dir=Path("/tmp/test")),
        forwarding_server_port=8420,
    )
    assert creator.get_creation_info(AgentId()) is None


def test_agent_creator_start_creation_returns_agent_id_and_tracks_status(tmp_path: Path) -> None:
    """Verify start_creation returns an agent ID and sets initial CLONING status.

    The actual background thread will fail (since the git URL is invalid),
    but the initial status should be immediately available.
    """
    creator = AgentCreator(
        paths=MindPaths(data_dir=tmp_path / "minds"),
        forwarding_server_port=8420,
    )

    agent_id = creator.start_creation("file:///nonexistent-repo")
    info = creator.get_creation_info(agent_id)

    assert info is not None
    assert info.agent_id == agent_id
    assert info.status == AgentCreationStatus.CLONING
