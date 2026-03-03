import json
import subprocess
from pathlib import Path

import pytest

from imbue.changelings.config.data_types import ChangelingPaths
from imbue.changelings.config.data_types import DeploymentProvider
from imbue.changelings.deployment.local import AgentIdLookupError
from imbue.changelings.deployment.local import MngCreateError
from imbue.changelings.deployment.local import MngNotFoundError
from imbue.changelings.deployment.local import UpdateResult
from imbue.changelings.deployment.local import _create_mng_agent
from imbue.changelings.deployment.local import _generate_auth_code
from imbue.changelings.deployment.local import _raise_if_agent_exists
from imbue.changelings.deployment.local import _run_mng_command
from imbue.changelings.deployment.local import _verify_mng_available
from imbue.changelings.deployment.local import clone_git_repo
from imbue.changelings.deployment.local import commit_files_in_repo
from imbue.changelings.deployment.local import init_empty_git_repo
from imbue.changelings.deployment.local import update_local
from imbue.changelings.errors import AgentAlreadyExistsError
from imbue.changelings.errors import ChangelingError
from imbue.changelings.errors import GitCloneError
from imbue.changelings.errors import GitCommitError
from imbue.changelings.errors import GitInitError
from imbue.changelings.errors import MngCommandError
from imbue.changelings.primitives import AgentName
from imbue.changelings.primitives import GitUrl
from imbue.changelings.testing import FakeConcurrencyGroup
from imbue.changelings.testing import init_and_commit_git_repo
from imbue.changelings.testing import make_fake_concurrency_group
from imbue.changelings.testing import make_finished_process
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mng.primitives import AgentId


def _make_fake_cg(
    agent_id: str | None = None,
    failing_command: str | None = None,
    fail_stderr: str = "command failed",
) -> FakeConcurrencyGroup:
    """Create a FakeConcurrencyGroup configured for testing.

    By default, all commands succeed. The `list` command returns a JSON response
    with the given agent_id (or a generated one). If failing_command is provided,
    that specific mng subcommand will return exit code 1.
    """
    resolved_agent_id = agent_id or str(AgentId())

    results: dict[str, FinishedProcess] = {
        "list": make_finished_process(
            stdout=json.dumps({"agents": [{"id": resolved_agent_id, "name": "my-agent"}]}),
            command=("mng", "list"),
        ),
    }

    if failing_command is not None:
        results[failing_command] = make_finished_process(
            returncode=1,
            stderr=fail_stderr,
            command=("mng", failing_command, "my-agent"),
        )

    return make_fake_concurrency_group(results=results)


def test_verify_mng_available_succeeds_when_mng_exists() -> None:
    """mng should be available in the test environment (it's a dependency)."""
    _verify_mng_available()


def test_generate_auth_code_creates_login_url(tmp_path: Path) -> None:
    paths = ChangelingPaths(data_dir=tmp_path)
    agent_id = AgentId()

    login_url = _generate_auth_code(
        paths=paths,
        agent_id=agent_id,
        forwarding_server_port=8420,
    )

    assert "http://127.0.0.1:8420/login" in login_url
    assert str(agent_id) in login_url
    assert "one_time_code=" in login_url


def test_generate_auth_code_stores_code_on_disk(tmp_path: Path) -> None:
    paths = ChangelingPaths(data_dir=tmp_path)
    agent_id = AgentId()

    _generate_auth_code(
        paths=paths,
        agent_id=agent_id,
        forwarding_server_port=8420,
    )

    assert paths.auth_dir.exists()
    codes_file = paths.auth_dir / "one_time_codes.json"
    assert codes_file.exists()


def test_mng_not_found_error_is_changeling_error() -> None:
    err = MngNotFoundError("test")
    assert isinstance(err, ChangelingError)


def test_mng_create_error_is_changeling_error() -> None:
    err = MngCreateError("test")
    assert isinstance(err, ChangelingError)


def test_agent_id_lookup_error_is_changeling_error() -> None:
    err = AgentIdLookupError("test")
    assert isinstance(err, ChangelingError)


def test_agent_already_exists_error_is_changeling_error() -> None:
    err = AgentAlreadyExistsError("test")
    assert isinstance(err, ChangelingError)


def test_agent_already_exists_error_message() -> None:
    err = AgentAlreadyExistsError(
        "An agent named 'my-agent' already exists. "
        "Use 'changeling update' to update it, or 'changeling destroy' to remove it."
    )
    assert "changeling update" in str(err)
    assert "changeling destroy" in str(err)


def test_raise_if_agent_exists_raises_when_agent_found() -> None:
    """Verify that _raise_if_agent_exists raises when the JSON output contains agents."""
    mng_output = json.dumps({"agents": [{"id": "agent-abc123", "name": "my-agent"}]})

    with pytest.raises(AgentAlreadyExistsError, match="already exists"):
        _raise_if_agent_exists(AgentName("my-agent"), mng_output)


def test_raise_if_agent_exists_does_not_raise_when_no_agents() -> None:
    """Verify that _raise_if_agent_exists does not raise when agents list is empty."""
    mng_output = json.dumps({"agents": []})

    _raise_if_agent_exists(AgentName("my-agent"), mng_output)


def test_raise_if_agent_exists_does_not_raise_for_invalid_json() -> None:
    """Verify that _raise_if_agent_exists silently proceeds on malformed JSON."""
    _raise_if_agent_exists(AgentName("my-agent"), "not valid json {{{")


def test_raise_if_agent_exists_error_mentions_update_and_destroy() -> None:
    """Verify the error message mentions changeling update and changeling destroy."""
    mng_output = json.dumps({"agents": [{"id": "agent-abc123"}]})

    with pytest.raises(AgentAlreadyExistsError) as exc_info:
        _raise_if_agent_exists(AgentName("my-agent"), mng_output)

    assert "changeling update" in str(exc_info.value)
    assert "changeling destroy" in str(exc_info.value)


def test_git_clone_error_is_changeling_error() -> None:
    err = GitCloneError("test")
    assert isinstance(err, ChangelingError)


def test_clone_git_repo_clones_local_repo(tmp_path: Path) -> None:
    """Verify that clone_git_repo clones a local git repo into the given directory."""
    repo_dir = tmp_path / "source-repo"
    repo_dir.mkdir()
    (repo_dir / "hello.txt").write_text("hello")
    init_and_commit_git_repo(repo_dir, tmp_path)

    clone_dir = tmp_path / "my-clone"
    clone_git_repo(GitUrl(str(repo_dir)), clone_dir)

    assert clone_dir.is_dir()
    assert (clone_dir / "hello.txt").read_text() == "hello"
    assert (clone_dir / ".git").is_dir()


def test_clone_git_repo_raises_for_invalid_url(tmp_path: Path) -> None:
    """Verify that clone_git_repo raises GitCloneError for an invalid URL."""
    clone_dir = tmp_path / "bad-clone"
    with pytest.raises(GitCloneError, match="git clone failed"):
        clone_git_repo(GitUrl("/nonexistent/repo/path"), clone_dir)


# --- init_empty_git_repo tests ---


def test_init_empty_git_repo_creates_git_directory(tmp_path: Path) -> None:
    """Verify that init_empty_git_repo creates a .git directory."""
    repo_dir = tmp_path / "new-repo"
    init_empty_git_repo(repo_dir)

    assert repo_dir.is_dir()
    assert (repo_dir / ".git").is_dir()


def test_init_empty_git_repo_creates_parent_dirs(tmp_path: Path) -> None:
    """Verify that init_empty_git_repo creates parent directories if needed."""
    repo_dir = tmp_path / "nested" / "dir" / "repo"
    init_empty_git_repo(repo_dir)

    assert repo_dir.is_dir()
    assert (repo_dir / ".git").is_dir()


def test_git_init_error_is_changeling_error() -> None:
    err = GitInitError("test")
    assert isinstance(err, ChangelingError)


# --- commit_files_in_repo tests ---


def test_commit_files_in_repo_commits_new_files(tmp_path: Path) -> None:
    """Verify that commit_files_in_repo stages and commits files."""
    repo_dir = tmp_path / "repo"
    init_empty_git_repo(repo_dir)

    (repo_dir / "hello.txt").write_text("hello")

    committed = commit_files_in_repo(repo_dir, "test commit")

    assert committed is True

    # Verify the file is tracked
    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert "test commit" in result.stdout


def test_commit_files_in_repo_returns_false_when_nothing_to_commit(tmp_path: Path) -> None:
    """Verify that commit_files_in_repo returns False when there are no changes."""
    repo_dir = tmp_path / "repo"
    init_empty_git_repo(repo_dir)

    # Create a file and commit it first
    (repo_dir / "hello.txt").write_text("hello")
    commit_files_in_repo(repo_dir, "first commit")

    # Now try to commit again with no changes
    committed = commit_files_in_repo(repo_dir, "empty commit")
    assert committed is False


def test_git_commit_error_is_changeling_error() -> None:
    err = GitCommitError("test")
    assert isinstance(err, ChangelingError)


# --- UpdateResult tests ---


def test_update_result_fields() -> None:
    result = UpdateResult(
        agent_name=AgentName("my-agent"),
        did_snapshot=True,
        did_push=False,
        did_provision=True,
    )
    assert result.agent_name == "my-agent"
    assert result.did_snapshot is True
    assert result.did_push is False
    assert result.did_provision is True


# --- MngCommandError tests ---


def test_mng_command_error_is_changeling_error() -> None:
    err = MngCommandError("test")
    assert isinstance(err, ChangelingError)


# --- _run_mng_command tests ---


def test_run_mng_command_raises_on_failure() -> None:
    """Verify that _run_mng_command raises MngCommandError when the command fails."""
    cg = make_fake_concurrency_group(
        results={
            "stop": make_finished_process(
                returncode=1,
                stderr="something went wrong",
                command=("mng", "stop", "my-agent"),
            ),
        }
    )

    with pytest.raises(MngCommandError, match="mng stop failed"):
        _run_mng_command(
            command_name="stop",
            args=["stop", "my-agent"],
            concurrency_group=cg,
        )


def test_run_mng_command_succeeds_on_zero_exit() -> None:
    """Verify that _run_mng_command does not raise when the command succeeds."""
    cg = make_fake_concurrency_group()

    _run_mng_command(
        command_name="stop",
        args=["stop", "my-agent"],
        concurrency_group=cg,
    )


def test_run_mng_command_includes_stderr_in_error() -> None:
    """Verify that the error message includes stderr output."""
    cg = make_fake_concurrency_group(
        results={
            "snapshot": make_finished_process(
                returncode=1,
                stderr="detailed error info",
                command=("mng", "snapshot", "my-agent"),
            ),
        }
    )

    with pytest.raises(MngCommandError, match="detailed error info"):
        _run_mng_command(
            command_name="snapshot",
            args=["snapshot", "my-agent"],
            concurrency_group=cg,
        )


def test_run_mng_command_falls_back_to_stdout_in_error() -> None:
    """Verify that when stderr is empty, stdout is used in the error message."""
    cg = make_fake_concurrency_group(
        results={
            "push": make_finished_process(
                returncode=1,
                stdout="stdout error info",
                stderr="",
                command=("mng", "push", "my-agent"),
            ),
        }
    )

    with pytest.raises(MngCommandError, match="stdout error info"):
        _run_mng_command(
            command_name="push",
            args=["push", "my-agent"],
            concurrency_group=cg,
        )


def test_run_mng_command_records_command() -> None:
    """Verify that _run_mng_command passes the correct command to the concurrency group."""
    cg = make_fake_concurrency_group()

    _run_mng_command(
        command_name="stop",
        args=["stop", "my-agent"],
        concurrency_group=cg,
    )

    assert len(cg.commands_run) == 1
    assert cg.commands_run[0] == ["mng", "stop", "my-agent"]


# --- update_local tests ---


def test_update_local_all_steps_enabled() -> None:
    """Verify that all mng commands are called when all flags are True."""
    cg = _make_fake_cg()

    result = update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=True,
        do_push=True,
        do_provision=True,
        concurrency_group=cg,
    )

    assert result.agent_name == "my-agent"
    assert result.did_snapshot is True
    assert result.did_push is True
    assert result.did_provision is True

    # Verify all expected commands were called
    subcommands = cg.subcommands_run
    assert "list" in subcommands
    assert "snapshot" in subcommands
    assert "stop" in subcommands
    assert "push" in subcommands
    assert "provision" in subcommands
    assert "start" in subcommands


def test_update_local_skips_snapshot_when_disabled() -> None:
    """Verify that snapshot is skipped when do_snapshot is False."""
    cg = _make_fake_cg()

    result = update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=False,
        do_push=True,
        do_provision=True,
        concurrency_group=cg,
    )

    assert result.did_snapshot is False
    subcommands = cg.subcommands_run
    assert "snapshot" not in subcommands


def test_update_local_skips_push_when_disabled() -> None:
    """Verify that push is skipped when do_push is False."""
    cg = _make_fake_cg()

    result = update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=True,
        do_push=False,
        do_provision=True,
        concurrency_group=cg,
    )

    assert result.did_push is False
    subcommands = cg.subcommands_run
    assert "push" not in subcommands


def test_update_local_skips_provision_when_disabled() -> None:
    """Verify that provision is skipped when do_provision is False."""
    cg = _make_fake_cg()

    result = update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=True,
        do_push=True,
        do_provision=False,
        concurrency_group=cg,
    )

    assert result.did_provision is False
    subcommands = cg.subcommands_run
    assert "provision" not in subcommands


def test_update_local_all_optional_steps_disabled() -> None:
    """Verify that only stop and start are called when all optional flags are False."""
    cg = _make_fake_cg()

    result = update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=False,
        do_push=False,
        do_provision=False,
        concurrency_group=cg,
    )

    assert result.did_snapshot is False
    assert result.did_push is False
    assert result.did_provision is False

    subcommands = cg.subcommands_run
    assert "snapshot" not in subcommands
    assert "push" not in subcommands
    assert "provision" not in subcommands
    assert "stop" in subcommands
    assert "start" in subcommands


def test_update_local_raises_when_agent_not_found() -> None:
    """Verify that update_local raises AgentIdLookupError when agent is not found."""
    cg = make_fake_concurrency_group(
        results={
            "list": make_finished_process(
                stdout=json.dumps({"agents": []}),
                command=("mng", "list"),
            ),
        }
    )

    with pytest.raises(AgentIdLookupError, match="No agent found"):
        update_local(
            agent_name=AgentName("ghost"),
            do_snapshot=True,
            do_push=True,
            do_provision=True,
            concurrency_group=cg,
        )


def test_update_local_raises_when_stop_fails() -> None:
    """Verify that update_local raises when mng stop fails."""
    cg = _make_fake_cg(failing_command="stop", fail_stderr="agent not responding")

    with pytest.raises(MngCommandError, match="mng stop failed"):
        update_local(
            agent_name=AgentName("my-agent"),
            do_snapshot=True,
            do_push=True,
            do_provision=True,
            concurrency_group=cg,
        )


def test_update_local_raises_when_snapshot_fails() -> None:
    """Verify that update_local raises when mng snapshot fails."""
    cg = _make_fake_cg(failing_command="snapshot", fail_stderr="snapshots not supported")

    with pytest.raises(MngCommandError, match="mng snapshot failed"):
        update_local(
            agent_name=AgentName("my-agent"),
            do_snapshot=True,
            do_push=True,
            do_provision=True,
            concurrency_group=cg,
        )


def test_update_local_raises_when_push_fails() -> None:
    """Verify that update_local raises when mng push fails."""
    cg = _make_fake_cg(failing_command="push", fail_stderr="push error")

    with pytest.raises(MngCommandError, match="mng push failed"):
        update_local(
            agent_name=AgentName("my-agent"),
            do_snapshot=True,
            do_push=True,
            do_provision=True,
            concurrency_group=cg,
        )


def test_update_local_raises_when_start_fails() -> None:
    """Verify that update_local raises when mng start fails."""
    cg = _make_fake_cg(failing_command="start", fail_stderr="start error")

    with pytest.raises(MngCommandError, match="mng start failed"):
        update_local(
            agent_name=AgentName("my-agent"),
            do_snapshot=True,
            do_push=True,
            do_provision=True,
            concurrency_group=cg,
        )


def test_update_local_executes_steps_in_correct_order() -> None:
    """Verify that update steps execute in the correct order."""
    cg = _make_fake_cg()

    update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=True,
        do_push=True,
        do_provision=True,
        concurrency_group=cg,
    )

    # Extract the mng subcommands in order
    subcommands = cg.subcommands_run
    assert subcommands == ["list", "snapshot", "stop", "push", "provision", "start"]


def test_update_local_provision_uses_no_restart_flag() -> None:
    """Verify that provision is called with --no-restart since we start separately."""
    cg = _make_fake_cg()

    update_local(
        agent_name=AgentName("my-agent"),
        do_snapshot=False,
        do_push=False,
        do_provision=True,
        concurrency_group=cg,
    )

    provision_commands = [cmd for cmd in cg.commands_run if len(cmd) > 1 and cmd[1] == "provision"]
    assert len(provision_commands) == 1
    assert "--no-restart" in provision_commands[0]


# --- _create_mng_agent tests ---


def test_create_mng_agent_local_uses_in_place(tmp_path: Path) -> None:
    """Verify that local deployment uses --in-place."""
    cg = make_fake_concurrency_group()

    _create_mng_agent(
        changeling_dir=tmp_path,
        agent_name=AgentName("my-agent"),
        agent_id=AgentId(),
        provider=DeploymentProvider.LOCAL,
        concurrency_group=cg,
    )

    assert len(cg.commands_run) == 1
    cmd = cg.commands_run[0]
    assert "--in-place" in cmd
    assert "--in" not in cmd
    assert "--source-path" not in cmd


def test_create_mng_agent_modal_uses_in_modal_and_source_path(tmp_path: Path) -> None:
    """Verify that modal deployment uses --in modal and --source-path."""
    cg = make_fake_concurrency_group()

    _create_mng_agent(
        changeling_dir=tmp_path,
        agent_name=AgentName("my-agent"),
        agent_id=AgentId(),
        provider=DeploymentProvider.MODAL,
        concurrency_group=cg,
    )

    assert len(cg.commands_run) == 1
    cmd = cg.commands_run[0]
    assert "--in-place" not in cmd
    assert "--in" in cmd
    in_index = cmd.index("--in")
    assert cmd[in_index + 1] == "modal"
    assert "--source-path" in cmd
    sp_index = cmd.index("--source-path")
    assert cmd[sp_index + 1] == str(tmp_path)


def test_create_mng_agent_docker_uses_in_docker_and_source_path(tmp_path: Path) -> None:
    """Verify that docker deployment uses --in docker and --source-path."""
    cg = make_fake_concurrency_group()

    _create_mng_agent(
        changeling_dir=tmp_path,
        agent_name=AgentName("my-agent"),
        agent_id=AgentId(),
        provider=DeploymentProvider.DOCKER,
        concurrency_group=cg,
    )

    assert len(cg.commands_run) == 1
    cmd = cg.commands_run[0]
    assert "--in" in cmd
    in_index = cmd.index("--in")
    assert cmd[in_index + 1] == "docker"
    assert "--source-path" in cmd
    sp_index = cmd.index("--source-path")
    assert cmd[sp_index + 1] == str(tmp_path)


def test_create_mng_agent_always_includes_changeling_label(tmp_path: Path) -> None:
    """Verify that all providers include --label changeling=true."""
    for provider in DeploymentProvider:
        cg = make_fake_concurrency_group()

        _create_mng_agent(
            changeling_dir=tmp_path,
            agent_name=AgentName("my-agent"),
            agent_id=AgentId(),
            provider=provider,
            concurrency_group=cg,
        )

        cmd = cg.commands_run[0]
        assert "--label" in cmd
        label_index = cmd.index("--label")
        assert cmd[label_index + 1] == "changeling=true"


def test_create_mng_agent_includes_template_and_no_connect(tmp_path: Path) -> None:
    """Verify that the mng create command always includes -t entrypoint and --no-connect."""
    cg = make_fake_concurrency_group()

    _create_mng_agent(
        changeling_dir=tmp_path,
        agent_name=AgentName("my-agent"),
        agent_id=AgentId(),
        provider=DeploymentProvider.LOCAL,
        concurrency_group=cg,
    )

    cmd = cg.commands_run[0]
    assert "-t" in cmd
    t_index = cmd.index("-t")
    assert cmd[t_index + 1] == "entrypoint"
    assert "--no-connect" in cmd


def test_create_mng_agent_passes_agent_id(tmp_path: Path) -> None:
    """Verify that the mng create command includes --agent-id with the provided ID."""
    cg = make_fake_concurrency_group()
    agent_id = AgentId()

    _create_mng_agent(
        changeling_dir=tmp_path,
        agent_name=AgentName("my-agent"),
        agent_id=agent_id,
        provider=DeploymentProvider.LOCAL,
        concurrency_group=cg,
    )

    cmd = cg.commands_run[0]
    assert "--agent-id" in cmd
    id_index = cmd.index("--agent-id")
    assert cmd[id_index + 1] == str(agent_id)


def test_create_mng_agent_raises_on_failure(tmp_path: Path) -> None:
    """Verify that _create_mng_agent raises MngCreateError when mng create fails."""
    cg = make_fake_concurrency_group(
        results={
            "create": make_finished_process(
                returncode=1,
                stderr="create failed",
                command=("mng", "create"),
            ),
        }
    )

    with pytest.raises(MngCreateError, match="mng create failed"):
        _create_mng_agent(
            changeling_dir=tmp_path,
            agent_name=AgentName("my-agent"),
            agent_id=AgentId(),
            provider=DeploymentProvider.LOCAL,
            concurrency_group=cg,
        )
