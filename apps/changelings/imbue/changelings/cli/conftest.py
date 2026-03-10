from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest
from click.testing import CliRunner
from click.testing import Result

from imbue.changelings.main import cli
from imbue.changelings.testing import init_and_commit_git_repo
from imbue.mng.utils.testing import isolate_home
from imbue.mng.utils.testing import isolate_tmux_server

DEPLOY_TEST_RUNNER = CliRunner()


def create_git_repo_with_agent_type(tmp_path: Path, agent_type: str = "elena-code") -> Path:
    """Create a minimal git repo with changelings.toml specifying the agent type."""
    repo_dir = tmp_path / "my-agent-repo"
    repo_dir.mkdir()
    (repo_dir / "changelings.toml").write_text('agent_type = "{}"\n'.format(agent_type))
    init_and_commit_git_repo(repo_dir, tmp_path)
    return repo_dir


def data_dir_args(tmp_path: Path) -> list[str]:
    """Return the --data-dir CLI args pointing to a temp directory."""
    return ["--data-dir", str(tmp_path / "changelings-data")]


def deploy_with_agent_type(
    tmp_path: Path,
    agent_type: str = "elena-code",
    name: str | None = "test-bot",
    add_paths: list[str] | None = None,
    provider: str = "local",
    input_text: str | None = None,
) -> Result:
    """Invoke changeling deploy with --agent-type and standard non-interactive flags."""
    args: list[str] = ["deploy", "--agent-type", agent_type]

    if name is not None:
        args.extend(["--name", name])

    for ap in add_paths or []:
        args.extend(["--add-path", ap])

    args.extend(["--provider", provider, "--no-self-deploy"])
    args.extend(data_dir_args(tmp_path))

    return DEPLOY_TEST_RUNNER.invoke(cli, args, input=input_text)


def deploy_with_git_url(
    tmp_path: Path,
    git_url: str,
    name: str | None = "test-bot",
    add_paths: list[str] | None = None,
    provider: str = "local",
    input_text: str | None = None,
    agent_type: str | None = None,
) -> Result:
    """Invoke changeling deploy with a git URL and standard non-interactive flags."""
    args: list[str] = ["deploy", git_url]

    if agent_type is not None:
        args.extend(["--agent-type", agent_type])

    if name is not None:
        args.extend(["--name", name])

    for ap in add_paths or []:
        args.extend(["--add-path", ap])

    args.extend(["--provider", provider, "--no-self-deploy"])
    args.extend(data_dir_args(tmp_path))

    return DEPLOY_TEST_RUNNER.invoke(cli, args, input=input_text)


@pytest.fixture(autouse=True)
def isolate_changeling_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Isolate changeling CLI tests from the real mng environment.

    Sets HOME, MNG_HOST_DIR, and MNG_PREFIX to temp/unique values so that
    tests do not create agents in the real ~/.mng or pollute the real tmux
    server. Uses the shared isolate_tmux_server() for tmux isolation.
    """
    test_id = uuid4().hex
    host_dir = tmp_path / ".mng"
    host_dir.mkdir()

    isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("MNG_HOST_DIR", str(host_dir))
    monkeypatch.setenv("MNG_PREFIX", "mng_{}-".format(test_id))
    monkeypatch.setenv("MNG_ROOT_NAME", "mng-test-{}".format(test_id))
    monkeypatch.setenv("MNG_COMPLETION_CACHE_DIR", str(host_dir))

    # Create .gitconfig so git commands work in the temp HOME
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@test.com\n")

    with isolate_tmux_server(monkeypatch):
        yield
