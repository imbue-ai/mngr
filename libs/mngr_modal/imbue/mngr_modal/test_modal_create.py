"""Acceptance tests for creating agents on Modal.

These tests require Modal credentials and network access to run. They are marked
with @pytest.mark.acceptance and are skipped by default. To run them:

    pytest -m modal --timeout=300

Or to run all tests including Modal tests:

    pytest --timeout=300
"""

import importlib.resources
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from imbue.mngr import resources
from imbue.mngr.utils.testing import ModalSubprocessTestEnv
from imbue.mngr.utils.testing import get_short_random_string


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mngr_create_echo_command_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating a command agent on Modal and that the host runs commands.

    Verifies the full create flow (CLI parsing, Modal sandbox creation, SSH
    setup, work-dir copy, agent creation -> "Done.") and then that the resulting
    host actually executes commands, by running a unique marker command via
    ``mngr exec`` and asserting its output.

    Note on scope: we verify command execution by exec-ing a command directly
    rather than by inspecting the side effect of the *agent's own* command. A
    command-type agent created with ``--no-connect`` runs its command in a
    detached session whose filesystem side effects are not synchronously
    observable from a subsequent ``mngr exec`` on Modal (confirmed empirically),
    so exec-ing a fresh marker command is the reliable signal that the created
    host can run the user's command -- the regression this guards against.
    """
    agent_name = f"test-modal-echo-{get_short_random_string()}"
    expected_output = f"hello-from-modal-{get_short_random_string()}"

    # Run mngr create with a long-lived command so the host stays up for exec.
    # Using --no-connect to create without attaching and --no-ensure-clean since
    # the temp source dir is not a git repo.
    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "--",
            "sleep",
            "100316",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify the created host actually runs commands by exec-ing a unique marker
    # and asserting it appears in the output (the default human output prints the
    # command's stdout, so a substring check suffices).
    exec_result = subprocess.run(
        ["uv", "run", "mngr", "exec", agent_name, f"echo {expected_output}"],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )
    assert exec_result.returncode == 0, f"exec failed with stderr: {exec_result.stderr}\nstdout: {exec_result.stdout}"
    assert expected_output in exec_result.stdout, (
        f"Expected marker '{expected_output}' in exec stdout: {exec_result.stdout}\nstderr: {exec_result.stderr}"
    )


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_mngr_create_with_transfer_git_worktree_on_modal_raises_error(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that explicitly requesting --transfer=git-worktree on modal raises an error.

    The git-worktree transfer mode only works when source and target are on the same host.
    Modal is always a remote host, so this should fail.
    """
    agent_name = f"test-modal-worktree-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--transfer=git-worktree",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "--",
            "echo",
            "hello",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    # Should fail with an error about git-worktree transfer mode
    assert result.returncode != 0, "Expected git-worktree on modal to fail"
    assert "git-worktree" in result.stderr.lower() or "git-worktree" in result.stdout.lower(), (
        f"Expected error message about git-worktree transfer mode. stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_mngr_create_with_invalid_snapshot_id_fails(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that --snapshot with a non-existent snapshot ID fails with a snapshot-context error.

    snap-123abc is a fake snapshot ID that does not exist. This verifies the
    --snapshot flag is accepted and that create propagates a meaningful error
    when the snapshot cannot be resolved. There is no companion success-path
    test since that would require a pre-existing snapshot in Modal.
    """
    agent_name = f"test-modal-bad-snapshot-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            agent_name,
            "--type",
            "command",
            "--provider",
            "modal",
            "--snapshot",
            "snap-123abc",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )

    assert result.returncode != 0, "Expected create with invalid snapshot ID to fail"
    combined = (result.stdout + result.stderr).lower()
    # The snapshot id is loaded via modal.Image.from_id("snap-123abc") during
    # create, so a real snapshot-resolution failure must reference the bad id AND
    # carry a not-found / resolution phrase. The previous "host creation failed"
    # alternative was too broad -- it matched the generic failure banner emitted
    # on *any* create error (bad image, network, quota), so it would pass even if
    # the --snapshot flag were silently ignored. Require both signals together.
    assert "snap-123abc" in combined, (
        f"Expected the bad snapshot id 'snap-123abc' in the error output. "
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    not_found_phrases = ("not found", "no such", "does not exist", "could not", "invalid", "unable to")
    assert any(phrase in combined for phrase in not_found_phrases), (
        f"Expected a snapshot/image resolution-failure phrase ({not_found_phrases}) in the error "
        f"output. stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mngr_create_with_build_args_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating an agent on Modal with custom build args (cpu, memory).

    This verifies that build arguments are passed correctly to the Modal sandbox.
    """
    agent_name = f"test-modal-build-{get_short_random_string()}"
    # Request a distinctive, non-default memory size (the test fixture default is
    # 2.0 GB) so the value read back is unambiguously attributable to this build
    # arg rather than the default.
    requested_memory_gb = 1.5

    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
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
            str(requested_memory_gb),
            "--",
            "sleep",
            "100314",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify the requested --memory build arg actually flowed through to the
    # host's recorded resources. Modal does not expose --memory as an in-sandbox
    # cgroup limit (the sandbox sees the host's total RAM), so instead we read it
    # back via `mngr list`: the host "resource" block is produced by
    # ModalProviderInstance.get_host_resources from the SandboxConfig the build
    # args were parsed into. A regression where --memory was dropped or ignored
    # would surface as the default 2.0 GB here.
    list_result = subprocess.run(
        ["uv", "run", "mngr", "list", "--provider", "modal", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )
    assert list_result.returncode == 0, f"mngr list failed: {list_result.stderr}\n{list_result.stdout}"
    listing = json.loads(list_result.stdout)
    host_resource = next(
        (a.get("host", {}).get("resource") for a in listing.get("agents", []) if a.get("name") == agent_name),
        None,
    )
    assert host_resource is not None, (
        f"Could not find host resource for agent {agent_name} in listing: {list_result.stdout}"
    )
    assert host_resource.get("memory_gb") == requested_memory_gb, (
        f"Expected host memory_gb == {requested_memory_gb} (from the --memory build arg), but got: {host_resource}"
    )


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mngr_create_with_dockerfile_on_modal(
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
    # Use a unique marker baked into the custom image so we can prove via exec
    # that the sandbox is actually running the image built from this Dockerfile.
    dockerfile_marker = f"custom-dockerfile-marker-{get_short_random_string()}"

    # Create a simple Dockerfile in the source directory
    dockerfile_path = temp_source_dir / "Dockerfile"
    dockerfile_content = f"""\
FROM debian:bookworm-slim

# Install minimal dependencies for mngr to work (openssh, tmux, rsync for file transfer)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    tmux \\
    python3 \\
    rsync \\
    && rm -rf /var/lib/apt/lists/*

# Create a marker file to verify we're using the custom image
RUN echo "{dockerfile_marker}" > /dockerfile-marker.txt
"""
    dockerfile_path.write_text(dockerfile_content)

    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "--",
            "sleep",
            "100315",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify the sandbox is running the custom image by reading the marker file
    # that only this Dockerfile creates.
    exec_result = subprocess.run(
        ["uv", "run", "mngr", "exec", agent_name, "cat /dockerfile-marker.txt"],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )
    assert exec_result.returncode == 0, f"exec failed with stderr: {exec_result.stderr}\nstdout: {exec_result.stdout}"
    assert dockerfile_marker in exec_result.stdout, (
        f"Expected dockerfile marker '{dockerfile_marker}' in exec stdout (proving the custom "
        f"image is in use): {exec_result.stdout}\nstderr: {exec_result.stderr}"
    )


@pytest.mark.flaky
@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_mngr_create_with_failing_dockerfile_shows_build_failure(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that a failing Dockerfile command shows the build failure in output.

    When a Dockerfile has a command that fails during the build process, mngr should:
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
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "--",
            "echo",
            "should-not-reach-here",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    # The command should fail because the Dockerfile build fails
    assert result.returncode != 0, (
        f"Expected mngr create to fail when Dockerfile has failing command, "
        f"but got returncode {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # The combined output should contain the unique marker from the failing command
    # so the user can see what actually failed in the build
    combined_output = result.stdout + result.stderr
    # this assertion has flaked in CI. It almost certainly happened because put_log_content was not called in _QuietOutputManager before the output buffer was closed
    #  It's not *entirely* clear to me how to fix this--ideally we wait for that output to be flushed, but I'm not sure how to do that in this context...
    assert unique_failure_marker in combined_output, (
        f"Expected the failing build command's output to be visible in mngr output. "
        f"Looking for unique marker '{unique_failure_marker}' in output.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_mngr_create_transfers_git_repo_with_untracked_files(
    temp_git_repo: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that agent creation with git repo source succeeds on Modal.

    This tests that the file transfer flow completes without error:
    1. All local branches and tags are pushed via git
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
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_git_repo),
            "--",
            "sleep",
            "100310",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify the untracked marker file actually landed on the remote work_dir.
    # exec runs in the agent's work_dir, so a relative path resolves correctly.
    exec_result = subprocess.run(
        ["uv", "run", "mngr", "exec", agent_name, "cat marker.txt"],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )
    assert exec_result.returncode == 0, f"exec failed with stderr: {exec_result.stderr}\nstdout: {exec_result.stdout}"
    assert unique_marker in exec_result.stdout, (
        f"Expected untracked marker '{unique_marker}' transferred to remote work_dir: "
        f"{exec_result.stdout}\nstderr: {exec_result.stderr}"
    )


@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_mngr_create_transfers_git_repo_with_new_branch(
    temp_git_repo: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that git transfer creates a new branch on the remote.

    This tests the git branch creation functionality during transfer:
    1. All local branches and tags are pushed via git
    2. A new branch is created (via --branch main:<new>) and checked out on the
       remote work_dir
    """
    agent_name = f"test-modal-branch-{get_short_random_string()}"
    # Branch off main into a uniquely-named new branch so we can assert the
    # remote work_dir was actually checked out onto exactly this branch.
    new_branch = f"modal-branch-test-{get_short_random_string()}"

    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
            "--no-ensure-clean",
            "--branch",
            f"main:{new_branch}",
            "--source",
            str(temp_git_repo),
            "--",
            "sleep",
            "100311",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify the remote work_dir is checked out onto the new branch.
    # exec runs in the agent's work_dir, which is the transferred git repo.
    exec_result = subprocess.run(
        # --format "{stdout}" so the captured output is exactly the branch name,
        # without mngr exec's human "Command succeeded on agent ..." status line.
        ["uv", "run", "mngr", "exec", agent_name, "git rev-parse --abbrev-ref HEAD", "--format", "{stdout}"],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )
    assert exec_result.returncode == 0, f"exec failed with stderr: {exec_result.stderr}\nstdout: {exec_result.stdout}"
    assert exec_result.stdout.strip() == new_branch, (
        f"Expected remote work_dir on branch '{new_branch}', got: {exec_result.stdout.strip()!r}\n"
        f"stderr: {exec_result.stderr}"
    )


def _get_mngr_default_dockerfile_path() -> Path:
    """Get the path to the mngr default Dockerfile from the resources package."""
    resources_dir = importlib.resources.files(resources)
    dockerfile_resource = resources_dir / "Dockerfile"
    dockerfile_path = Path(str(dockerfile_resource))
    return dockerfile_path


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_mngr_create_with_default_dockerfile_on_modal(
    tmp_path: Path,
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test creating an agent on Modal using the mngr default Dockerfile.

    This verifies that the default Dockerfile in libs/mngr/imbue/mngr/resources/Dockerfile
    builds successfully on Modal and that ``mngr create`` can launch an agent on the
    resulting image (reporting "Done."). A synchronous ``mngr exec`` after create then
    asserts the resulting image actually contains tools the default Dockerfile installs
    (``ttyd`` and ``uv``), confirming the sandbox is running the default image rather
    than just that create returned.

    This test is marked as release since it takes longer due to the image build.
    """
    agent_name = f"test-modal-default-df-{get_short_random_string()}"

    dockerfile_path = _get_mngr_default_dockerfile_path()
    assert dockerfile_path.exists(), f"Default Dockerfile not found at {dockerfile_path}"

    # Resolve repo root from this test file's location so the test does not
    # depend on the pytest cwd (offload sandboxes run pytest from a different
    # cwd than /code/mngr, which is where .mngr/image_commit_hash and the
    # make_tar_of_repo.sh script live).
    repo_root = Path(__file__).resolve().parents[4]

    tar_dir = tmp_path / "tar_output"
    tar_dir.mkdir()
    commit_hash = os.environ.get("GITHUB_SHA", "") or (repo_root / ".mngr/image_commit_hash").read_text().strip()

    # Package the repo at commit_hash via make_tar_of_repo.sh, then unpack
    # producer-side so the Modal build context is a real source tree. The
    # shared mngr Dockerfile no longer special-cases current.tar.gz; both
    # mngr_schedule's deploy path and this test extract the tarball before
    # handing it off as context_dir, matching offload's "context_dir is a
    # real source tree" contract.
    subprocess.run(
        [
            "bash",
            str(repo_root / "scripts" / "make_tar_of_repo.sh"),
            commit_hash,
            str(tar_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
        env=modal_subprocess_env.env,
        cwd=repo_root,
    )
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    with tarfile.open(tar_dir / "current.tar.gz", "r:gz") as tf:
        tf.extractall(context_dir, filter="data")

    # now we can try making the agent
    result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            f"{agent_name}@{agent_name}.modal:/code/mngr",
            "--type",
            "command",
            "--new-host",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "-b",
            f"context-dir={context_dir}",
            "--",
            "sleep",
            "100312",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=modal_subprocess_env.env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"

    # Verify the sandbox is running the default image by checking for tools that
    # only the default Dockerfile installs: ttyd (to /usr/local/bin) and uv (to
    # /root/.local/bin). Their presence is a property unique to the default image.
    exec_result = subprocess.run(
        ["uv", "run", "mngr", "exec", agent_name, "test -x /usr/local/bin/ttyd -a -x /root/.local/bin/uv"],
        capture_output=True,
        text=True,
        timeout=120,
        env=modal_subprocess_env.env,
    )
    assert exec_result.returncode == 0, (
        f"Expected the default image to contain ttyd and uv, but the existence check failed.\n"
        f"stdout: {exec_result.stdout}\nstderr: {exec_result.stderr}"
    )
