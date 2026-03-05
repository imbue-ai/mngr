"""End-to-end tests for changeling deployment and chat using the test-coder agent type.

These tests deploy a real changeling locally using the test-coder agent type
(which uses the echo model instead of real LLMs), verify it works, and clean up.

No API keys are required because the test-coder agent type:
- Runs a simple idle loop instead of Claude Code
- Uses the llm echo model for chat (returns "Echo: <input>")
"""

import json
import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest


def _clean_env() -> dict[str, str]:
    """Build an environment dict for subprocesses that strips pytest markers.

    mng refuses to run when PYTEST_CURRENT_TEST is set (safety check to
    prevent tests from accidentally using real mng state). We strip it
    so that our end-to-end subprocess calls work against the real system.
    """
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _run_mng(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run a `uv run mng` command and return the result."""
    return subprocess.run(
        ["uv", "run", "mng", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )


def _run_changeling(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a `uv run changeling` command and return the result."""
    return subprocess.run(
        ["uv", "run", "changeling", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )


def _parse_mng_list_json(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from mng list --json stdout.

    The stdout may contain non-JSON lines (e.g. SSH error tracebacks)
    mixed with the JSON. We find the first line starting with '{' and
    parse from there.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                return list(data.get("agents", []))
            except json.JSONDecodeError:
                continue
    return []


def _find_agent(agent_name: str) -> dict[str, object] | None:
    """Find an agent by name, returning its full record or None."""
    result = _run_mng(
        "list",
        "--include",
        f'name == "{agent_name}"',
        "--json",
        "--provider",
        "local",
    )
    if result.returncode != 0:
        return None
    agents = _parse_mng_list_json(result.stdout)
    if agents:
        return agents[0]
    return None


def _extract_response(exec_result: subprocess.CompletedProcess[str]) -> str:
    """Extract the model response from mng exec output.

    Filters out mng's "Command succeeded/failed" status lines,
    returning only the first line of actual model output.
    """
    response_lines = [
        line for line in exec_result.stdout.strip().splitlines() if line and not line.startswith("Command ")
    ]
    assert len(response_lines) >= 1, f"No response from model: {exec_result.stdout!r}"
    return response_lines[0]


def _cleanup_agent(agent_name: str) -> None:
    """Destroy an agent and clean up its changeling directory."""
    agent = _find_agent(agent_name)
    agent_id = str(agent["id"]) if agent else None

    _run_mng("destroy", agent_name, "--force", timeout=30.0)

    if agent_id:
        changeling_dir = Path.home() / ".changelings" / agent_id
        if changeling_dir.exists():
            shutil.rmtree(changeling_dir, ignore_errors=True)


@pytest.fixture
def deployed_test_coder() -> Generator[dict[str, object], None, None]:
    """Deploy a test-coder changeling and yield its agent record.

    Handles deployment and cleanup so individual tests only need to
    exercise the deployed agent.
    """
    agent_name = f"e2e-test-{uuid4().hex}"

    deploy_result = _run_changeling(
        "deploy",
        "--agent-type",
        "test-coder",
        "--name",
        agent_name,
        "--provider",
        "local",
        "--no-self-deploy",
    )
    assert deploy_result.returncode == 0, (
        f"Deploy failed:\nstdout: {deploy_result.stdout}\nstderr: {deploy_result.stderr}"
    )

    agent = _find_agent(agent_name)
    assert agent is not None, f"Agent {agent_name} not found in mng list"

    try:
        yield agent
    finally:
        _cleanup_agent(agent_name)


@pytest.mark.release
@pytest.mark.timeout(120)
def test_deploy_test_coder_and_verify_echo_model(deployed_test_coder: dict[str, object]) -> None:
    """Deploy a test-coder changeling and verify the echo model works via mng exec."""
    agent_name = str(deployed_test_coder["name"])

    # Verify the echo model returns the expected response
    test_message = "Hello from end-to-end test"
    exec_result = _run_mng("exec", agent_name, f'llm -m echo "{test_message}"')
    assert exec_result.returncode == 0, f"llm echo failed:\nstdout: {exec_result.stdout}\nstderr: {exec_result.stderr}"
    assert _extract_response(exec_result) == f"Echo: {test_message}"

    # Verify the chat settings have the echo model configured
    work_dir = str(deployed_test_coder["work_dir"])
    settings_path = Path(work_dir) / ".changelings" / "settings.toml"
    assert settings_path.exists(), f"Settings file not found at {settings_path}"
    settings_content = settings_path.read_text()
    assert 'model = "echo"' in settings_content, f"Echo model not configured in settings:\n{settings_content}"


@pytest.mark.release
@pytest.mark.timeout(120)
def test_echo_model_with_custom_response_via_env(deployed_test_coder: dict[str, object]) -> None:
    """Verify the LLM_ECHO_RESPONSE env var overrides the default echo behavior."""
    agent_name = str(deployed_test_coder["name"])

    custom_response = "I am a test bot and this is my canned response."
    exec_result = _run_mng(
        "exec",
        agent_name,
        f'LLM_ECHO_RESPONSE="{custom_response}" llm -m echo "anything"',
    )
    assert exec_result.returncode == 0, f"llm echo failed:\nstdout: {exec_result.stdout}\nstderr: {exec_result.stderr}"
    assert _extract_response(exec_result) == custom_response


@pytest.mark.release
@pytest.mark.timeout(120)
def test_echo_model_with_responses_file(deployed_test_coder: dict[str, object]) -> None:
    """Verify the LLM_ECHO_RESPONSES_FILE substring-matching feature."""
    agent_name = str(deployed_test_coder["name"])

    responses = {"hello": "Hi there! I am a test agent.", "help": "I can help with testing."}
    responses_json = json.dumps(responses)
    _run_mng("exec", agent_name, f"echo '{responses_json}' > /tmp/test_responses.json")

    # Matching input
    exec_result = _run_mng(
        "exec",
        agent_name,
        'LLM_ECHO_RESPONSES_FILE=/tmp/test_responses.json llm -m echo "hello world"',
    )
    assert exec_result.returncode == 0
    assert _extract_response(exec_result) == "Hi there! I am a test agent."

    # Non-matching input falls back to default echo
    exec_result = _run_mng(
        "exec",
        agent_name,
        'LLM_ECHO_RESPONSES_FILE=/tmp/test_responses.json llm -m echo "something else"',
    )
    assert exec_result.returncode == 0
    assert _extract_response(exec_result) == "Echo: something else"
