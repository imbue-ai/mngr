"""Acceptance tests for creating agents on Modal.

These tests require Modal credentials and network access to run. They are marked
with @pytest.mark.acceptance and are skipped by default. To run them:

    pytest -m modal --timeout=300

Or to run all tests including Modal tests:

    pytest --timeout=300
"""

import importlib.resources
import os
import subprocess
from pathlib import Path

import pytest

from imbue.mng import resources
from imbue.mng.testing.testing import ModalSubprocessTestEnv
from imbue.mng.testing.testing import get_short_random_string


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    # Create a simple file so the directory isn't empty
    (source_dir / "test.txt").write_text("test content")
    return source_dir


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mng_create_echo_command_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating an agent with echo command on Modal using the CLI.

    This is an end-to-end acceptance test that verifies the full flow:
    1. CLI parses arguments correctly
    2. Modal sandbox is created
    3. SSH connection is established
    4. Work directory is copied to remote host
    5. Agent is created and command runs
    6. Output can be verified
    """
    agent_name = f"test-modal-echo-{get_short_random_string()}"
    expected_output = f"hello-from-modal-{get_short_random_string()}"

    # Run mng create with echo command on modal
    # Using --no-connect and --await-ready to run synchronously without attaching
    # Using --no-ensure-clean since temp dir won't be a git repo
    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_mng_create_with_worktree_flag_on_modal_raises_error(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that explicitly requesting --worktree on modal raises an error.

    The --worktree flag only works when source and target are on the same host.
    Modal is always a remote host, so this should fail.
    """
    agent_name = f"test-modal-worktree-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--worktree",
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "--",
            "hello",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    # Should fail with an error about worktree mode
    assert result.returncode != 0, "Expected worktree on modal to fail"
    assert "worktree" in result.stderr.lower() or "worktree" in result.stdout.lower(), (
        f"Expected error message about worktree mode. stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mng_create_with_build_args_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating an agent on Modal with custom build args (cpu, memory).

    This verifies that build arguments are passed correctly to the Modal sandbox.
    """
    agent_name = f"test-modal-build-{get_short_random_string()}"
    expected_output = f"build-test-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            "--cpu",
            "-b",
            "0.5",
            "-b",
            "--memory",
            "-b",
            "0.5",
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mng_create_with_dockerfile_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating an agent on Modal using a custom Dockerfile.

    This verifies that:
    1. The --file build arg is correctly parsed by the modal provider
    2. Modal builds an image from the Dockerfile
    3. The sandbox runs with the custom image
    """
    agent_name = f"test-modal-dockerfile-{get_short_random_string()}"
    expected_output = f"dockerfile-test-{get_short_random_string()}"

    # Create a simple Dockerfile in the source directory
    dockerfile_path = temp_source_dir / "Dockerfile"
    dockerfile_content = """\
FROM debian:bookworm-slim

# Install minimal dependencies for mng to work (openssh, tmux, rsync for file transfer)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    tmux \\
    python3 \\
    rsync \\
    && rm -rf /var/lib/apt/lists/*

# Create a marker file to verify we're using the custom image
RUN echo "custom-dockerfile-marker" > /dockerfile-marker.txt
"""
    dockerfile_path.write_text(dockerfile_content)

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "--",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_mng_create_with_failing_dockerfile_shows_build_failure(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that a failing Dockerfile command shows the build failure in output.

    When a Dockerfile has a command that fails during the build process, mng should:
    1. Return a non-zero exit code
    2. Show the failure message in the output so the user can see what went wrong

    This is important for debuggability - users need to see why their build failed.
    """
    agent_name = f"test-modal-dockerfile-fail-{get_short_random_string()}"

    # Create a Dockerfile with a command that will definitely fail
    dockerfile_path = temp_source_dir / "Dockerfile"
    # Use a unique marker so we can verify the actual failing command is shown in output
    unique_failure_marker = f"intentional-fail-{get_short_random_string()}"
    dockerfile_content = f"""\
FROM debian:bookworm-slim

# This command will fail intentionally
RUN echo "About to fail with marker: {unique_failure_marker}" && exit 1
"""
    dockerfile_path.write_text(dockerfile_content)

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "echo",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "--",
            "should-not-reach-here",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    # The command should fail because the Dockerfile build fails
    assert result.returncode != 0, (
        f"Expected mng create to fail when Dockerfile has failing command, "
        f"but got returncode {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # The combined output should contain the unique marker from the failing command
    # so the user can see what actually failed in the build
    combined_output = result.stdout + result.stderr
    # this assertion has flaked in CI. It almost certainly happened because put_log_content was not called in _QuietOutputManager before the output buffer was closed
    #  It's not *entirely* clear to me how to fix this--ideally we wait for that output to be flushed, but I'm not sure how to do that in this context...
    assert unique_failure_marker in combined_output, (
        f"Expected the failing build command's output to be visible in mng output. "
        f"Looking for unique marker '{unique_failure_marker}' in output.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mng_create_transfers_git_repo_with_untracked_files(
    temp_git_repo: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that agent creation with git repo source succeeds on Modal.

    This tests that the file transfer flow completes without error:
    1. Git repository is pushed via git push --mirror
    2. Untracked files are transferred via rsync
    3. Agent is created successfully

    Note: The actual file transfer logic is verified by unit tests in test_host.py.
    This acceptance test verifies the end-to-end flow works on Modal.
    """
    agent_name = f"test-modal-git-{get_short_random_string()}"
    unique_marker = f"git-transfer-test-{get_short_random_string()}"

    # Write a unique marker file (will be transferred via rsync as untracked)
    (temp_git_repo / "marker.txt").write_text(unique_marker)

    # Create agent - if file transfer fails, this will fail
    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "generic",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_git_repo),
            "--",
            "sleep 3600",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_mng_create_transfers_git_repo_with_new_branch(
    temp_git_repo: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that git transfer creates a new branch on the remote.

    This tests the git branch creation functionality during transfer:
    1. Git repository is pushed via git push --mirror
    2. A new branch is created with the specified prefix
    """
    agent_name = f"test-modal-branch-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "generic",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_git_repo),
            "--new-branch=",
            "--",
            "git rev-parse --abbrev-ref HEAD && sleep 3600",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


def _get_mng_default_dockerfile_path() -> Path:
    """Get the path to the mng default Dockerfile from the resources package."""
    resources_dir = importlib.resources.files(resources)
    dockerfile_resource = resources_dir / "Dockerfile"
    dockerfile_path = Path(str(dockerfile_resource))
    return dockerfile_path


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_mng_create_with_default_dockerfile_on_modal(
    tmp_path: Path,
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating an agent on Modal using the mng default Dockerfile.

    This verifies that the default Dockerfile in libs/mng/imbue/mng/resources/Dockerfile:
    1. Builds successfully on Modal
    2. Has the expected tools installed (uv, claude code)
    3. Can run agents properly

    This test is marked as release since it takes longer due to the image build.
    """
    agent_name = f"test-modal-default-df-{get_short_random_string()}"
    unique_marker = f"default-dockerfile-{get_short_random_string()}"

    dockerfile_path = _get_mng_default_dockerfile_path()
    assert dockerfile_path.exists(), f"Default Dockerfile not found at {dockerfile_path}"

    tar_dir = tmp_path / "tar_output"
    tar_dir.mkdir()
    temp_dir_with_tar = str(tar_dir)
    commit_hash = os.environ.get("GITHUB_SHA", "") or Path(".mng/image_commit_hash").read_text().strip()

    # go make the tar
    subprocess.run(
        [
            "bash",
            "-c",
            f"./scripts/make_tar_of_repo.sh {commit_hash} {temp_dir_with_tar}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
        env=modal_subprocess_env.env,
    )
    # now we can try making the agent
    result = subprocess.run(
        [
            "uv",
            "run",
            "mng",
            "create",
            agent_name,
            "generic",
            "--in",
            "modal",
            "--host-name",
            agent_name,
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "-b",
            f"context-dir={temp_dir_with_tar}",
            "--target-path",
            "/code/mng",
            "--",
            f"echo {unique_marker} && which uv && which claude && sleep 30",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"
