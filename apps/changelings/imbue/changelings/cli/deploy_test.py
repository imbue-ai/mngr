import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from click.testing import Result

from imbue.changelings.cli.deploy import _copy_add_paths
from imbue.changelings.cli.deploy import _move_to_permanent_location
from imbue.changelings.cli.deploy import _parse_add_path
from imbue.changelings.cli.deploy import _prepare_repo
from imbue.changelings.cli.deploy import _print_result
from imbue.changelings.cli.deploy import _resolve_agent_name
from imbue.changelings.cli.deploy import _resolve_agent_type
from imbue.changelings.cli.deploy import _resolve_provider
from imbue.changelings.cli.deploy import _resolve_self_deploy
from imbue.changelings.config.data_types import DeploymentProvider
from imbue.changelings.config.data_types import SelfDeployChoice
from imbue.changelings.deployment.local import DeploymentResult
from imbue.changelings.errors import ChangelingError
from imbue.changelings.errors import MissingAgentTypeError
from imbue.changelings.main import cli
from imbue.changelings.primitives import AgentName
from imbue.changelings.testing import capture_loguru_messages
from imbue.changelings.testing import init_and_commit_git_repo
from imbue.mng.primitives import AgentId

_RUNNER = CliRunner()


def _create_git_repo_with_agent_type(tmp_path: Path, agent_type: str = "elena-code") -> Path:
    """Create a minimal git repo with changelings.toml specifying the agent type."""
    repo_dir = tmp_path / "my-agent-repo"
    repo_dir.mkdir()
    (repo_dir / "changelings.toml").write_text('agent_type = "{}"\n'.format(agent_type))
    init_and_commit_git_repo(repo_dir, tmp_path)
    return repo_dir


def _data_dir_args(tmp_path: Path) -> list[str]:
    """Return the --data-dir CLI args pointing to a temp directory."""
    return ["--data-dir", str(tmp_path / "changelings-data")]


def _deploy_with_agent_type(
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
    args.extend(_data_dir_args(tmp_path))

    return _RUNNER.invoke(cli, args, input=input_text)


def _deploy_with_git_url(
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
    args.extend(_data_dir_args(tmp_path))

    return _RUNNER.invoke(cli, args, input=input_text)


# --- Tests for git URL deployment ---


def test_deploy_fails_for_invalid_git_url(tmp_path: Path) -> None:
    result = _RUNNER.invoke(cli, ["deploy", "/nonexistent/repo/path", *_data_dir_args(tmp_path)])

    assert result.exit_code != 0
    assert "git clone failed" in result.output


def test_deploy_fails_when_no_agent_type(tmp_path: Path) -> None:
    """Cloning a repo without agent type in changelings.toml should fail."""
    repo_dir = tmp_path / "empty-repo"
    repo_dir.mkdir()
    init_and_commit_git_repo(repo_dir, tmp_path, allow_empty=True)

    result = _RUNNER.invoke(cli, ["deploy", str(repo_dir), *_data_dir_args(tmp_path)])

    assert result.exit_code != 0
    assert "agent type" in result.output.lower() or "agent_type" in result.output


def test_deploy_cleans_up_temp_dir_on_clone_failure(tmp_path: Path) -> None:
    """Verify that a failed clone does not leave temporary directories behind."""
    data_dir = tmp_path / "changelings-data"

    _RUNNER.invoke(cli, ["deploy", "/nonexistent/repo/path", "--data-dir", str(data_dir)])

    if data_dir.exists():
        leftover = [p for p in data_dir.iterdir() if p.name.startswith(".tmp-")]
        assert leftover == []


def test_deploy_cleans_up_temp_dir_on_missing_agent_type(tmp_path: Path) -> None:
    """Verify that a missing agent type does not leave temporary directories behind."""
    repo_dir = tmp_path / "empty-repo"
    repo_dir.mkdir()
    init_and_commit_git_repo(repo_dir, tmp_path, allow_empty=True)
    data_dir = tmp_path / "changelings-data"

    _RUNNER.invoke(cli, ["deploy", str(repo_dir), "--data-dir", str(data_dir)])

    leftover = [p for p in data_dir.iterdir() if p.name.startswith(".tmp-")]
    assert leftover == []


@pytest.mark.acceptance
def test_deploy_cleans_up_temp_dir_after_deployment(tmp_path: Path) -> None:
    """Verify that no .tmp- directories remain after deployment (success or failure)."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)
    data_dir = tmp_path / "changelings-data"

    _deploy_with_git_url(tmp_path, str(repo_dir), name="my-bot", provider="local")

    if data_dir.exists():
        leftover = [p for p in data_dir.iterdir() if p.name.startswith(".tmp-")]
        assert leftover == []


@pytest.mark.acceptance
def test_deploy_shows_prompts(tmp_path: Path) -> None:
    """Verify all three prompts appear when deploying without flags."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), *_data_dir_args(tmp_path)],
        input="my-agent\n2\nN\n",
    )

    assert "What would you like to name this agent" in result.output
    assert "Where do you want to run" in result.output
    assert "launch its own agents" in result.output


@pytest.mark.acceptance
def test_deploy_displays_clone_url(tmp_path: Path) -> None:
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), *_data_dir_args(tmp_path)],
        input="test-bot\n1\nN\n",
    )

    assert "Cloning repository" in result.output


@pytest.mark.acceptance
def test_deploy_name_flag_skips_prompt(tmp_path: Path) -> None:
    """Verify that --name skips the name prompt."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _deploy_with_git_url(tmp_path, str(repo_dir), name="my-custom-name")

    assert "What would you like to name this agent" not in result.output


@pytest.mark.acceptance
def test_deploy_provider_flag_skips_prompt(tmp_path: Path) -> None:
    """Verify that --provider skips the provider prompt."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), "--provider", "local", "--no-self-deploy", *_data_dir_args(tmp_path)],
        input="test-bot\n",
    )

    assert "Where do you want to run" not in result.output


@pytest.mark.acceptance
def test_deploy_self_deploy_flag_skips_prompt(tmp_path: Path) -> None:
    """Verify that --no-self-deploy skips the self-deploy prompt."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), "--no-self-deploy", "--provider", "local", *_data_dir_args(tmp_path)],
        input="test-bot\n",
    )

    assert "launch its own agents" not in result.output


@pytest.mark.acceptance
def test_deploy_all_flags_skip_all_prompts(tmp_path: Path) -> None:
    """Verify that providing all flags skips all interactive prompts."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _deploy_with_git_url(tmp_path, str(repo_dir), name="bot")

    assert "What would you like to name this agent" not in result.output
    assert "Where do you want to run" not in result.output
    assert "launch its own agents" not in result.output


def test_resolve_provider_accepts_modal() -> None:
    """Verify that modal is accepted as a valid provider value."""
    assert _resolve_provider("modal") == DeploymentProvider.MODAL


def test_resolve_provider_accepts_docker() -> None:
    """Verify that docker is accepted as a valid provider value."""
    assert _resolve_provider("docker") == DeploymentProvider.DOCKER


# --- Tests for --agent-type (no git URL) ---


def test_deploy_fails_without_git_url_or_agent_type(tmp_path: Path) -> None:
    """Verify that deploy fails when neither GIT_URL nor --agent-type is provided."""
    result = _RUNNER.invoke(cli, ["deploy", *_data_dir_args(tmp_path)])

    assert result.exit_code != 0
    assert "Either GIT_URL or --agent-type must be provided" in result.output


@pytest.mark.acceptance
def test_deploy_agent_type_shows_creating_message(tmp_path: Path) -> None:
    """Verify that --agent-type shows a 'Creating changeling repo' message instead of 'Cloning'."""
    result = _deploy_with_agent_type(tmp_path)

    assert "Cloning repository" not in result.output
    assert "Deploying changeling from" in result.output


@pytest.mark.acceptance
def test_deploy_agent_type_defaults_name_to_agent_type(tmp_path: Path) -> None:
    """Verify that --agent-type defaults the agent name prompt to the agent type value."""
    result = _deploy_with_agent_type(tmp_path, name=None, input_text="elena-code\n")

    assert "elena-code" in result.output


# --- Tests for --add-path validation ---


def test_deploy_add_path_fails_for_nonexistent_source(tmp_path: Path) -> None:
    """Verify that --add-path fails when the source path does not exist."""
    result = _deploy_with_agent_type(
        tmp_path,
        name="bad-bot",
        add_paths=["/nonexistent/path:dest.txt"],
    )

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_deploy_add_path_fails_for_invalid_format(tmp_path: Path) -> None:
    """Verify that --add-path fails when the format is not SRC:DEST."""
    result = _deploy_with_agent_type(
        tmp_path,
        name="bad-bot",
        add_paths=["no-colon-here"],
    )

    assert result.exit_code != 0
    assert "SRC:DEST" in result.output


def test_deploy_add_path_fails_for_absolute_dest(tmp_path: Path) -> None:
    """Verify that --add-path fails when DEST is an absolute path."""
    extra_file = tmp_path / "file.txt"
    extra_file.write_text("content")

    result = _deploy_with_agent_type(
        tmp_path,
        name="bad-bot",
        add_paths=["{}:/absolute/dest.txt".format(extra_file)],
    )

    assert result.exit_code != 0
    assert "must be relative" in result.output


# --- Tests for _prepare_repo (repo preparation logic) ---


def test_prepare_repo_creates_git_repo(tmp_path: Path) -> None:
    """Verify that _prepare_repo creates a git repo."""
    repo_dir = tmp_path / "repo"

    _prepare_repo(
        temp_dir=repo_dir,
        git_url=None,
        branch=None,
        add_paths=(),
    )

    assert (repo_dir / ".git").is_dir()


def test_prepare_repo_with_git_url_clones(tmp_path: Path) -> None:
    """Verify that _prepare_repo clones a git URL."""
    source = _create_git_repo_with_agent_type(tmp_path)
    clone_dir = tmp_path / "clone"

    _prepare_repo(
        temp_dir=clone_dir,
        git_url=str(source),
        branch=None,
        add_paths=(),
    )

    assert (clone_dir / ".git").is_dir()
    assert (clone_dir / "changelings.toml").exists()


def test_prepare_repo_add_path_copies_file(tmp_path: Path) -> None:
    """Verify that _prepare_repo copies --add-path files into the repo."""
    extra_file = tmp_path / "extra.txt"
    extra_file.write_text("extra content")

    repo_dir = tmp_path / "repo"
    _prepare_repo(
        temp_dir=repo_dir,
        git_url=None,
        branch=None,
        add_paths=((extra_file, Path("extra.txt")),),
    )

    assert (repo_dir / "extra.txt").read_text() == "extra content"


def test_prepare_repo_add_path_copies_directory(tmp_path: Path) -> None:
    """Verify that _prepare_repo recursively copies --add-path directories."""
    src_dir = tmp_path / "src-config"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("file a")
    sub = src_dir / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("file b")

    repo_dir = tmp_path / "repo"
    _prepare_repo(
        temp_dir=repo_dir,
        git_url=None,
        branch=None,
        add_paths=((src_dir, Path("config")),),
    )

    assert (repo_dir / "config" / "a.txt").read_text() == "file a"
    assert (repo_dir / "config" / "sub" / "b.txt").read_text() == "file b"


def test_prepare_repo_add_path_multiple(tmp_path: Path) -> None:
    """Verify that multiple --add-path args are all copied."""
    file_a = tmp_path / "a.txt"
    file_a.write_text("aaa")
    file_b = tmp_path / "b.txt"
    file_b.write_text("bbb")

    repo_dir = tmp_path / "repo"
    _prepare_repo(
        temp_dir=repo_dir,
        git_url=None,
        branch=None,
        add_paths=((file_a, Path("a.txt")), (file_b, Path("b.txt"))),
    )

    assert (repo_dir / "a.txt").read_text() == "aaa"
    assert (repo_dir / "b.txt").read_text() == "bbb"


def test_prepare_repo_add_path_files_are_committed(tmp_path: Path) -> None:
    """Verify that --add-path files are included in the git commit."""
    extra_file = tmp_path / "extra.txt"
    extra_file.write_text("committed content")

    repo_dir = tmp_path / "repo"
    _prepare_repo(
        temp_dir=repo_dir,
        git_url=None,
        branch=None,
        add_paths=((extra_file, Path("extra.txt")),),
    )

    ls_result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert "extra.txt" in ls_result.stdout


def test_prepare_repo_add_path_with_clone_commits_added_files(tmp_path: Path) -> None:
    """Verify that --add-path files are committed when used with a git URL."""
    source = _create_git_repo_with_agent_type(tmp_path)
    extra_file = tmp_path / "extra.txt"
    extra_file.write_text("extra from clone")

    clone_dir = tmp_path / "clone"
    _prepare_repo(
        temp_dir=clone_dir,
        git_url=str(source),
        branch=None,
        add_paths=((extra_file, Path("extra.txt")),),
    )

    ls_result = subprocess.run(
        ["git", "ls-files"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
    )
    assert "extra.txt" in ls_result.stdout
    assert (clone_dir / "changelings.toml").exists()


def test_prepare_repo_add_path_preserves_changelings_toml(tmp_path: Path) -> None:
    """Verify that --add-path can add a changelings.toml to the repo."""
    toml_file = tmp_path / "changelings.toml"
    toml_file.write_text('agent_type = "custom-type"\n')

    repo_dir = tmp_path / "repo"
    _prepare_repo(
        temp_dir=repo_dir,
        git_url=None,
        branch=None,
        add_paths=((toml_file, Path("changelings.toml")),),
    )

    content = (repo_dir / "changelings.toml").read_text()
    assert "custom-type" in content


# --- Tests for _copy_add_paths ---


def test_copy_add_paths_returns_count(tmp_path: Path) -> None:
    """Verify that _copy_add_paths returns the number of paths copied."""
    file_a = tmp_path / "a.txt"
    file_a.write_text("aaa")
    file_b = tmp_path / "b.txt"
    file_b.write_text("bbb")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    copied = _copy_add_paths(
        ((file_a, Path("a.txt")), (file_b, Path("b.txt"))),
        repo_dir,
    )

    assert copied == 2
    assert (repo_dir / "a.txt").read_text() == "aaa"
    assert (repo_dir / "b.txt").read_text() == "bbb"


# --- Tests for _move_to_permanent_location ---


def test_move_to_permanent_location_moves_directory(tmp_path: Path) -> None:
    """Verify that _move_to_permanent_location moves the source to the target."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("content")

    target = tmp_path / "target"

    _move_to_permanent_location(source, target)

    assert not source.exists()
    assert target.is_dir()
    assert (target / "file.txt").read_text() == "content"


def test_move_to_permanent_location_raises_when_target_exists(tmp_path: Path) -> None:
    """Verify that _move_to_permanent_location raises when the target already exists."""
    source = tmp_path / "source"
    source.mkdir()

    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ChangelingError, match="already exists"):
        _move_to_permanent_location(source, target)


def test_move_to_permanent_location_preserves_contents(tmp_path: Path) -> None:
    """Verify that _move_to_permanent_location preserves all directory contents."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("aaa")
    sub = source / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("bbb")

    target = tmp_path / "target"

    _move_to_permanent_location(source, target)

    assert (target / "a.txt").read_text() == "aaa"
    assert (target / "sub" / "b.txt").read_text() == "bbb"


# --- Tests for _print_result ---


def test_print_result_includes_agent_name_and_login_url() -> None:
    """Verify _print_result shows agent name and login URL."""
    agent_id = AgentId()
    login_url = "http://127.0.0.1:8420/login?agent_id={}&one_time_code=yyy".format(agent_id)
    result = DeploymentResult(
        agent_name=AgentName("my-agent"),
        agent_id=agent_id,
        login_url=login_url,
    )

    with capture_loguru_messages() as messages:
        _print_result(result, DeploymentProvider.LOCAL)

    combined = "".join(messages)
    assert "my-agent" in combined
    assert login_url in combined
    assert "local" in combined


def test_print_result_shows_provider_name() -> None:
    """Verify _print_result shows the correct provider name."""
    result = DeploymentResult(
        agent_name=AgentName("my-agent"),
        agent_id=AgentId(),
        login_url="http://127.0.0.1:8420/login?agent_id=xxx&one_time_code=yyy",
    )

    with capture_loguru_messages() as messages:
        _print_result(result, DeploymentProvider.MODAL)

    combined = "".join(messages)
    assert "modal" in combined


# --- Tests for _resolve_* functions ---


def test_resolve_agent_name_with_explicit_name() -> None:
    """Verify _resolve_agent_name uses the provided name when given."""
    name = _resolve_agent_name("my-bot", "elena-code")

    assert name == "my-bot"


def test_resolve_provider_with_explicit_provider() -> None:
    """Verify _resolve_provider returns the explicit value without prompting."""
    assert _resolve_provider("local") == DeploymentProvider.LOCAL
    assert _resolve_provider("modal") == DeploymentProvider.MODAL
    assert _resolve_provider("docker") == DeploymentProvider.DOCKER


def test_resolve_self_deploy_with_explicit_true() -> None:
    """Verify _resolve_self_deploy returns YES when True is passed."""
    assert _resolve_self_deploy(True) == SelfDeployChoice.YES


def test_resolve_self_deploy_with_explicit_false() -> None:
    """Verify _resolve_self_deploy returns NOT_NOW when False is passed."""
    assert _resolve_self_deploy(False) == SelfDeployChoice.NOT_NOW


# --- Tests for _resolve_agent_type ---


def test_resolve_agent_type_uses_cli_flag(tmp_path: Path) -> None:
    """Verify _resolve_agent_type returns the CLI flag when provided."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    assert _resolve_agent_type(repo_dir, "elena-code") == "elena-code"


def test_resolve_agent_type_reads_changelings_toml(tmp_path: Path) -> None:
    """Verify _resolve_agent_type reads agent_type from changelings.toml."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "changelings.toml").write_text('agent_type = "custom-type"\n')

    assert _resolve_agent_type(repo_dir, None) == "custom-type"


def test_resolve_agent_type_cli_flag_overrides_toml(tmp_path: Path) -> None:
    """Verify CLI --agent-type takes precedence over changelings.toml."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "changelings.toml").write_text('agent_type = "toml-type"\n')

    assert _resolve_agent_type(repo_dir, "cli-type") == "cli-type"


def test_resolve_agent_type_raises_when_missing(tmp_path: Path) -> None:
    """Verify _resolve_agent_type raises when no agent type can be determined."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with pytest.raises(MissingAgentTypeError, match="agent type"):
        _resolve_agent_type(repo_dir, None)


def test_resolve_agent_type_raises_on_malformed_toml(tmp_path: Path) -> None:
    """Verify _resolve_agent_type raises with a clear message for malformed TOML."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "changelings.toml").write_text("this is not valid [toml = ")

    with pytest.raises(MissingAgentTypeError, match="Failed to parse"):
        _resolve_agent_type(repo_dir, None)


# --- Tests for _parse_add_path ---


def test_parse_add_path_valid_file(tmp_path: Path) -> None:
    """Verify _parse_add_path parses a valid SRC:DEST pair."""
    src_file = tmp_path / "source.txt"
    src_file.write_text("content")

    src, dest = _parse_add_path("{}:dest.txt".format(src_file))

    assert src == src_file
    assert dest == Path("dest.txt")


def test_parse_add_path_no_colon() -> None:
    """Verify _parse_add_path raises for missing colon."""
    with pytest.raises(Exception, match="SRC:DEST"):
        _parse_add_path("no-colon-here")


def test_parse_add_path_empty_src() -> None:
    """Verify _parse_add_path raises for empty SRC."""
    with pytest.raises(Exception, match="non-empty"):
        _parse_add_path(":dest.txt")


def test_parse_add_path_empty_dest() -> None:
    """Verify _parse_add_path raises for empty DEST."""
    with pytest.raises(Exception, match="non-empty"):
        _parse_add_path("/some/path:")


def test_parse_add_path_nonexistent_source() -> None:
    """Verify _parse_add_path raises for nonexistent source."""
    with pytest.raises(Exception, match="does not exist"):
        _parse_add_path("/nonexistent/source:dest.txt")


def test_parse_add_path_absolute_dest(tmp_path: Path) -> None:
    """Verify _parse_add_path raises for absolute DEST."""
    src_file = tmp_path / "source.txt"
    src_file.write_text("content")

    with pytest.raises(Exception, match="must be relative"):
        _parse_add_path("{}:/absolute/dest.txt".format(src_file))


# --- Tests for deploy with self-deploy enabled ---


@pytest.mark.acceptance
def test_deploy_with_self_deploy_flag(tmp_path: Path) -> None:
    """Verify --self-deploy flag is accepted and skips the self-deploy prompt."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        [
            "deploy",
            str(repo_dir),
            "--name",
            "my-bot",
            "--provider",
            "local",
            "--self-deploy",
            *_data_dir_args(tmp_path),
        ],
    )

    assert "launch its own agents" not in result.output


# --- Tests for deploy prompt interaction via CLI ---


@pytest.mark.acceptance
def test_deploy_provider_prompt_accepts_local_selection(
    tmp_path: Path,
) -> None:
    """Verify interactive provider selection with local (choice 1) proceeds to deployment."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), *_data_dir_args(tmp_path)],
        input="test-bot\n1\nN\n",
    )

    assert "Where do you want to run" in result.output
    assert "Deploying changeling from" in result.output


@pytest.mark.acceptance
def test_deploy_self_deploy_yes_via_interactive_input(tmp_path: Path) -> None:
    """Verify that interactive input 'y' for self-deploy is accepted."""
    repo_dir = _create_git_repo_with_agent_type(tmp_path)

    result = _RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), "--provider", "local", *_data_dir_args(tmp_path)],
        input="test-bot\ny\n",
    )

    assert "launch its own agents" in result.output
