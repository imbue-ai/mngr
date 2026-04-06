"""Tests for DockerOverSsh command building and error handling."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from imbue.mngr_vps_docker.docker_over_ssh import DockerOverSsh
from imbue.mngr_vps_docker.errors import ContainerSetupError
from imbue.mngr_vps_docker.errors import VpsConnectionError


@pytest.fixture()
def docker_ssh(tmp_path: Path) -> DockerOverSsh:
    """Create a DockerOverSsh instance for testing."""
    key_path = tmp_path / "test_key"
    key_path.write_text("fake key")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")
    return DockerOverSsh(
        vps_ip="192.168.1.100",
        ssh_user="root",
        ssh_key_path=key_path,
        known_hosts_path=known_hosts,
    )


def test_build_ssh_command_structure(docker_ssh: DockerOverSsh) -> None:
    cmd = docker_ssh._build_ssh_command("echo hello")
    assert cmd[0] == "ssh"
    assert "-o" in cmd
    assert "StrictHostKeyChecking=yes" in cmd
    assert "BatchMode=yes" in cmd
    assert "root@192.168.1.100" in cmd
    assert cmd[-1] == "echo hello"


def test_build_ssh_command_includes_key_and_known_hosts(docker_ssh: DockerOverSsh) -> None:
    cmd = docker_ssh._build_ssh_command("ls")
    assert "-i" in cmd
    key_idx = cmd.index("-i")
    assert str(docker_ssh.ssh_key_path) == cmd[key_idx + 1]
    # Check UserKnownHostsFile
    known_hosts_opt = f"UserKnownHostsFile={docker_ssh.known_hosts_path}"
    assert known_hosts_opt in cmd


def test_run_ssh_timeout_raises_connection_error(docker_ssh: DockerOverSsh) -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 5)):
        with pytest.raises(VpsConnectionError, match="timed out"):
            docker_ssh.run_ssh("echo hello", timeout_seconds=5.0)


def test_run_ssh_os_error_raises_connection_error(docker_ssh: DockerOverSsh) -> None:
    with patch("subprocess.run", side_effect=OSError("No such file")):
        with pytest.raises(VpsConnectionError, match="SSH command failed"):
            docker_ssh.run_ssh("echo hello")


def test_run_ssh_connection_refused_raises_connection_error(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=255, stdout="", stderr="Connection refused"
    )
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(VpsConnectionError, match="Cannot reach VPS"):
            docker_ssh.run_ssh("echo hello")


def test_run_ssh_no_route_raises_connection_error(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=255, stdout="", stderr="No route to host"
    )
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(VpsConnectionError, match="Cannot reach VPS"):
            docker_ssh.run_ssh("echo hello")


def test_run_ssh_nonzero_exit_raises_container_setup_error(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=1, stdout="", stderr="command not found"
    )
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ContainerSetupError, match="Remote command failed"):
            docker_ssh.run_ssh("bad-command")


def test_run_ssh_success_returns_stdout(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="hello world\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        result = docker_ssh.run_ssh("echo hello world")
        assert result == "hello world\n"


def test_run_docker_builds_correct_command(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="container_id\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        docker_ssh.run_docker(["ps", "-a"])
        call_args = mock_run.call_args[0][0]
        # The last argument to ssh should be the remote command
        remote_cmd = call_args[-1]
        assert remote_cmd == "docker ps -a"


def test_run_docker_quotes_special_args(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        docker_ssh.run_docker(["exec", "container", "sh", "-c", "echo hello world"])
        call_args = mock_run.call_args[0][0]
        remote_cmd = call_args[-1]
        # "echo hello world" should be quoted
        assert "'echo hello world'" in remote_cmd


def test_check_file_exists_returns_true_on_success(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        assert docker_ssh.check_file_exists("/var/run/mngr-ready") is True


def test_check_file_exists_returns_false_on_failure(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=1, stdout="", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        assert docker_ssh.check_file_exists("/nonexistent") is False


def test_check_docker_ready_returns_true_on_success(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        assert docker_ssh.check_docker_ready() is True


def test_check_docker_ready_returns_false_on_connection_error(docker_ssh: DockerOverSsh) -> None:
    with patch("subprocess.run", side_effect=OSError("Connection failed")):
        assert docker_ssh.check_docker_ready() is False


def test_inspect_container_parses_json_list(docker_ssh: DockerOverSsh) -> None:
    inspect_data = [{"Id": "abc123", "State": {"Running": True}}]
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout=f"{__import__('json').dumps(inspect_data)}\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        result = docker_ssh.inspect_container("test-container")
        assert result["Id"] == "abc123"


def test_inspect_container_returns_dict_when_not_list(docker_ssh: DockerOverSsh) -> None:
    inspect_data = {"Id": "abc123"}
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout=f"{__import__('json').dumps(inspect_data)}\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        result = docker_ssh.inspect_container("test-container")
        assert result["Id"] == "abc123"


def test_container_is_running_true(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="true\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        assert docker_ssh.container_is_running("test-container") is True


def test_container_is_running_false(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=0, stdout="false\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_result):
        assert docker_ssh.container_is_running("test-container") is False


def test_container_is_running_error_returns_false(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(
        args=["ssh"], returncode=1, stdout="", stderr="No such container"
    )
    with patch("subprocess.run", return_value=mock_result):
        assert docker_ssh.container_is_running("nonexistent") is False


def test_upload_directory_success(docker_ssh: DockerOverSsh, tmp_path: Path) -> None:
    local_dir = tmp_path / "context"
    local_dir.mkdir()
    (local_dir / "Dockerfile").write_text("FROM ubuntu")
    mock_result = subprocess.CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        docker_ssh.upload_directory(local_dir, "/tmp/build-ctx")
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "rsync"
        assert "-az" in call_args
        assert "--delete" in call_args
        assert str(local_dir) + "/" in call_args
        assert "root@192.168.1.100:/tmp/build-ctx/" in call_args


def test_upload_directory_timeout(docker_ssh: DockerOverSsh, tmp_path: Path) -> None:
    local_dir = tmp_path / "context"
    local_dir.mkdir()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("rsync", 5)):
        with pytest.raises(VpsConnectionError, match="timed out"):
            docker_ssh.upload_directory(local_dir, "/tmp/build-ctx", timeout_seconds=5.0)


def test_upload_directory_rsync_failure(docker_ssh: DockerOverSsh, tmp_path: Path) -> None:
    local_dir = tmp_path / "context"
    local_dir.mkdir()
    mock_result = subprocess.CompletedProcess(
        args=["rsync"], returncode=1, stdout="", stderr="rsync error: some failure"
    )
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ContainerSetupError, match="Upload failed"):
            docker_ssh.upload_directory(local_dir, "/tmp/build-ctx")


def test_build_image_success(docker_ssh: DockerOverSsh) -> None:
    mock_result = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        tag = docker_ssh.build_image("my-tag", "/tmp/ctx", ("--file=Dockerfile",))
        assert tag == "my-tag"
        call_args = mock_run.call_args[0][0]
        remote_cmd = call_args[-1]
        assert "docker build" in remote_cmd
        assert "my-tag" in remote_cmd
        assert "/tmp/ctx" in remote_cmd
