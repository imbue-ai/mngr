import json
import subprocess
from pathlib import Path

import pytest

from imbue.mng.conftest import ModalSubprocessTestEnv
from imbue.mng.utils.testing import get_short_random_string


@pytest.fixture
def temp_source_dir(tmp_path: Path) -> Path:
    """Create a temporary source directory for tests."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    return source_dir


def _create_modal_agent(
    agent_name: str,
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Create a long-running agent on Modal for exec testing."""
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
            "--no-connect",
            "--await-ready",
            "--no-ensure-clean",
            "--source",
            str(temp_source_dir),
            "--",
            "sleep 99123",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=modal_subprocess_env.env,
    )
    assert result.returncode == 0, f"Failed to create agent: {result.stderr}\n{result.stdout}"


def _exec_on_agent(
    agent_name: str,
    command: str,
    modal_subprocess_env: ModalSubprocessTestEnv,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run mng exec on a Modal agent and return the result."""
    args = ["uv", "run", "mng", "exec", agent_name, command]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
        env=modal_subprocess_env.env,
    )


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_exec_echo_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test executing a simple command on a Modal agent."""
    agent_name = f"test-exec-echo-{get_short_random_string()}"
    _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)

    result = _exec_on_agent(agent_name, "echo hello-from-modal", modal_subprocess_env)

    assert result.returncode == 0, f"exec failed: {result.stderr}\n{result.stdout}"
    assert "hello-from-modal" in result.stdout


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_exec_cwd_override_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that --cwd overrides the working directory on a Modal agent."""
    agent_name = f"test-exec-cwd-{get_short_random_string()}"
    _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)

    result = _exec_on_agent(agent_name, "pwd", modal_subprocess_env, extra_args=["--cwd", "/tmp"])

    assert result.returncode == 0, f"exec failed: {result.stderr}\n{result.stdout}"
    assert "/tmp" in result.stdout


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_exec_failure_propagates_exit_code_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test that a failing command returns exit code 1 on a Modal agent."""
    agent_name = f"test-exec-fail-{get_short_random_string()}"
    _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)

    result = _exec_on_agent(agent_name, "false", modal_subprocess_env)

    assert result.returncode == 1


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_exec_json_output_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test JSON output format when executing on a Modal agent."""
    agent_name = f"test-exec-json-{get_short_random_string()}"
    _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env)

    result = _exec_on_agent(
        agent_name, "echo json-test", modal_subprocess_env, extra_args=["--format", "json", "--quiet"]
    )

    assert result.returncode == 0, f"exec failed: {result.stderr}\n{result.stdout}"
    output = json.loads(result.stdout.strip())
    assert output["total_executed"] == 1
    assert output["total_failed"] == 0
    assert len(output["results"]) == 1
    assert output["results"][0]["agent"] == agent_name
    assert "json-test" in output["results"][0]["stdout"]
    assert output["results"][0]["success"] is True
