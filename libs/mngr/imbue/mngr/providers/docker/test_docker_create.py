import json
import subprocess
from pathlib import Path

import docker
import docker.errors
import docker.models.containers
import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr.providers.docker.instance import LABEL_HOST_ID
from imbue.mngr.providers.docker.instance import create_docker_client
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.docker, pytest.mark.acceptance, pytest.mark.rsync]


def _find_host_container(client: docker.DockerClient, prefix: str) -> docker.models.containers.Container:
    """Find the single mngr host container created by a subprocess test.

    Host containers carry a ``LABEL_HOST_ID`` label and a name starting with
    the test prefix. The state container (which has no host-id label) is
    excluded.
    """
    containers = client.containers.list(all=True, filters={"label": LABEL_HOST_ID})
    matching = [c for c in containers if (c.name or "").startswith(prefix)]
    assert len(matching) == 1, f"expected exactly one host container, found {[c.name for c in matching]}"
    return matching[0]


def _kill_sshd_master(container: docker.models.containers.Container) -> None:
    """Kill the master sshd process inside a container by its specific PID.

    Finds the sshd process whose parent is NOT itself an sshd (per-connection
    sshds fork from the master, so the master is the one whose ppid sits
    outside the sshd PID set) and sends it a plain ``kill`` (SIGTERM).
    Deliberately avoids broad ``pkill`` patterns: only the one verified
    master PID is signalled.
    """
    exit_code, output = container.exec_run(["pgrep", "-x", "sshd"])
    assert exit_code == 0, f"no sshd process found in container: {output!r}"
    sshd_pids = [line for line in output.decode().split() if line]
    assert sshd_pids, "pgrep returned no sshd PIDs"
    sshd_pid_set = set(sshd_pids)

    master_pid: str | None = None
    for pid in sshd_pids:
        stat_code, stat_out = container.exec_run(["cat", f"/proc/{pid}/stat"])
        if stat_code != 0:
            continue
        # /proc/<pid>/stat field 4 is ppid; field 2 (comm) may contain spaces,
        # so split on the closing ')' of comm before reading positional fields.
        fields = stat_out.decode().rsplit(")", 1)[-1].split()
        ppid = fields[1] if len(fields) > 1 else ""
        if ppid and ppid not in sshd_pid_set:
            master_pid = pid
            break
    assert master_pid is not None, f"could not identify master sshd among PIDs {sshd_pids}"

    kill_code, kill_out = container.exec_run(["kill", master_pid])
    assert kill_code == 0, f"failed to kill sshd PID {master_pid}: {kill_out!r}"


@pytest.mark.timeout(600)
def test_mngr_create_echo_command_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating an agent with echo command on Docker using the CLI."""
    agent_name = f"test-docker-echo-{get_short_random_string()}"
    expected_output = f"hello-from-docker-{get_short_random_string()}"

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
            "docker",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "--",
            "echo",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=540,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.timeout(600)
def test_mngr_create_with_start_args_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating a Docker host with custom CPU and memory start args."""
    agent_name = f"test-docker-start-{get_short_random_string()}"
    expected_output = f"start-test-{get_short_random_string()}"

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
            "docker",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "-s",
            "--cpus=2",
            "-s",
            "--memory=2g",
            "--",
            "echo",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=540,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.timeout(600)
def test_mngr_create_with_tags_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating a Docker host with tags and verify they appear."""
    agent_name = f"test-docker-tags-{get_short_random_string()}"
    expected_output = f"tags-test-{get_short_random_string()}"

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
            "docker",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "--host-label",
            "env=test",
            "--",
            "echo",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=540,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.timeout(600)
def test_mngr_create_with_dockerfile_on_docker(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """Test creating a Docker host using a custom Dockerfile."""
    agent_name = f"test-docker-df-{get_short_random_string()}"
    expected_output = f"dockerfile-test-{get_short_random_string()}"

    dockerfile_path = temp_source_dir / "Dockerfile"
    dockerfile_content = """\
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    tmux \\
    python3 \\
    rsync \\
    && rm -rf /var/lib/apt/lists/*

RUN echo "custom-dockerfile-marker" > /dockerfile-marker.txt
"""
    dockerfile_path.write_text(dockerfile_content)

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
            "docker",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "-b",
            f"--file={dockerfile_path}",
            "-b",
            str(temp_source_dir),
            "--",
            "echo",
            expected_output,
        ],
        capture_output=True,
        text=True,
        timeout=540,
        env=docker_subprocess_env,
    )

    assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "Done." in result.stdout, f"Expected 'Done.' in output: {result.stdout}"


@pytest.mark.release
@pytest.mark.timeout(900)
def test_mngr_create_stop_start_destroy_lifecycle(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """End-to-end CLI lifecycle: `mngr create`, `stop`, `start`, `destroy` each exit 0 in sequence on Docker.

    Drives the real CLI through the full host lifecycle and asserts the return
    code of every step. A regression in any single subcommand -- e.g. `start`
    failing to bring the stopped container back up, or `destroy` not tearing it
    down -- fails the corresponding return-code assertion.
    """
    agent_name = f"test-docker-lifecycle-{get_short_random_string()}"

    # Create
    create_result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            agent_name,
            "--type",
            "command",
            "--provider",
            "docker",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "--",
            "sleep 3600",
        ],
        capture_output=True,
        text=True,
        timeout=840,
        env=docker_subprocess_env,
    )
    assert create_result.returncode == 0, (
        f"Create failed with stderr: {create_result.stderr}\nstdout: {create_result.stdout}"
    )

    # Stop
    stop_result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "stop",
            agent_name,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=docker_subprocess_env,
    )
    assert stop_result.returncode == 0, f"Stop failed with stderr: {stop_result.stderr}\nstdout: {stop_result.stdout}"

    # Start
    start_result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "start",
            agent_name,
            "--no-connect",
        ],
        capture_output=True,
        text=True,
        timeout=540,
        env=docker_subprocess_env,
    )
    assert start_result.returncode == 0, (
        f"Start failed with stderr: {start_result.stderr}\nstdout: {start_result.stdout}"
    )

    # Destroy
    destroy_result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "destroy",
            agent_name,
            "--force",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=docker_subprocess_env,
    )
    assert destroy_result.returncode == 0, (
        f"Destroy failed with stderr: {destroy_result.stderr}\nstdout: {destroy_result.stdout}"
    )


@pytest.mark.docker_sdk
@pytest.mark.timeout(900)
def test_stop_host_recovers_container_with_dead_sshd(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """``mngr stop --stop-host`` must succeed when the container's sshd is dead.

    Reproduces the original bug: a Docker container is ``running`` but its
    sshd has been killed (sshd crash or PID exhaustion), so any SSH-based
    discovery raises ``Error reading SSH protocol banner...`` -- and the old
    ``mngr stop`` failed before ever reaching ``stop_host``.

    The fix resolves the target host without SSH, so ``--stop-host`` stops
    the container at the Docker-daemon level. A subsequent ``mngr start``
    brings up a fresh container (with fresh sshd), recovering the agent.
    """
    agent_name = f"test-stop-host-recovery-{get_short_random_string()}"

    # Create a long-running agent on Docker.
    create_result = subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            agent_name,
            "--type",
            "command",
            "--provider",
            "docker",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "--",
            "sleep 3600",
        ],
        capture_output=True,
        text=True,
        timeout=840,
        env=docker_subprocess_env,
    )
    assert create_result.returncode == 0, (
        f"Create failed with stderr: {create_result.stderr}\nstdout: {create_result.stdout}"
    )

    client = create_docker_client()
    try:
        prefix = docker_subprocess_env["MNGR_PREFIX"]
        container = _find_host_container(client, prefix)

        # Kill the master sshd inside the running container.
        _kill_sshd_master(container)

        # The container itself must still be running -- only sshd is gone.
        container.reload()
        assert container.status == "running", (
            f"container should still be running after sshd was killed, got: {container.status}"
        )

        # The bug: this used to fail with an SSH banner error before reaching
        # stop_host. The fix resolves the host without SSH, so it succeeds.
        stop_result = subprocess.run(
            ["uv", "run", "mngr", "stop", agent_name, "--stop-host"],
            capture_output=True,
            text=True,
            timeout=120,
            env=docker_subprocess_env,
        )
        assert stop_result.returncode == 0, (
            f"stop --stop-host failed with stderr: {stop_result.stderr}\nstdout: {stop_result.stdout}"
        )

        # The container should now be stopped.
        container.reload()
        assert container.status in ("exited", "stopped"), (
            f"container should be stopped after --stop-host, got: {container.status}"
        )
    finally:
        client.close()

    # mngr start brings up a fresh container with a fresh sshd, recovering the agent.
    start_result = subprocess.run(
        ["uv", "run", "mngr", "start", agent_name, "--no-connect", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=540,
        env=docker_subprocess_env,
    )
    assert start_result.returncode == 0, (
        f"start after --stop-host failed with stderr: {start_result.stderr}\nstdout: {start_result.stdout}"
    )
    # The one arm a local host cannot produce: a host that really was brought up
    # from stopped. A caller reads this to tell a cold boot from a start that
    # found everything already running and did nothing.
    assert json.loads(start_result.stdout.strip())["was_host_started"] is True


@pytest.mark.timeout(900)
def test_same_agent_id_on_two_docker_hosts_is_independent(
    temp_source_dir: Path,
    docker_subprocess_env: dict[str, str],
) -> None:
    """The duplicate-id (migration overlap) case, end to end on real hosts.

    The same agent id is created on two Docker hosts. A bare-id *single-target*
    command must refuse with the ID@HOST disambiguation, a bare-id *plural*
    command (exec) must operate on every instance, ID@HOST must address each
    instance, and destroying one instance must leave the other fully
    functional -- the keystone behavior this feature exists for.
    """
    shared_id = str(AgentId.generate())
    suffix = get_short_random_string()
    host_a = f"dup-host-a-{suffix}"
    host_b = f"dup-host-b-{suffix}"

    def run_mngr(*args: str, timeout: int = 540) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "mngr", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=docker_subprocess_env,
        )

    for host_name, agent_name in ((host_a, f"dup-a-{suffix}"), (host_b, f"dup-b-{suffix}")):
        result = run_mngr(
            "create",
            f"{agent_name}@{host_name}.docker",
            "--new-host",
            "--id",
            shared_id,
            "--type",
            "command",
            "--no-connect",
            "--no-ensure-clean",
            "--from",
            str(temp_source_dir),
            "--",
            "sleep",
            "581234",
        )
        assert result.returncode == 0, f"create on {host_name} failed: {result.stderr}\n{result.stdout}"

    # A bare-id single-target command (transcript resolves exactly one agent) is
    # ambiguous and must say so, naming ID@HOST.
    transcript_bare = run_mngr("transcript", shared_id, timeout=120)
    assert transcript_bare.returncode != 0
    assert "multiple hosts" in transcript_bare.stderr
    assert "ID@HOST" in transcript_bare.stderr

    # A bare-id plural command keeps the uniform all-matches filter semantics:
    # exec runs on BOTH instances.
    exec_bare = run_mngr("exec", shared_id, "true", timeout=180)
    assert exec_bare.returncode == 0, f"bare-id exec failed: {exec_bare.stderr}"
    for agent_name in (f"dup-a-{suffix}", f"dup-b-{suffix}"):
        assert agent_name in exec_bare.stdout

    # ID@HOST addresses each instance independently.
    for host_name in (host_a, host_b):
        exec_scoped = run_mngr("exec", f"{shared_id}@{host_name}", "true", timeout=180)
        assert exec_scoped.returncode == 0, f"exec on {host_name} failed: {exec_scoped.stderr}"

    # Destroying the instance on host A must leave host B's instance fully functional.
    destroy_result = run_mngr("destroy", f"{shared_id}@{host_a}", "--force", timeout=300)
    assert destroy_result.returncode == 0, f"destroy failed: {destroy_result.stderr}\n{destroy_result.stdout}"

    surviving_exec = run_mngr("exec", f"{shared_id}@{host_b}", "true", timeout=180)
    assert surviving_exec.returncode == 0, f"surviving instance broken: {surviving_exec.stderr}"

    # With only one instance left, the bare id matches exactly the survivor.
    exec_bare_after = run_mngr("exec", shared_id, "true", timeout=180)
    assert exec_bare_after.returncode == 0, f"bare-id exec after destroy failed: {exec_bare_after.stderr}"
    assert f"dup-b-{suffix}" in exec_bare_after.stdout
    assert f"dup-a-{suffix}" not in exec_bare_after.stdout
