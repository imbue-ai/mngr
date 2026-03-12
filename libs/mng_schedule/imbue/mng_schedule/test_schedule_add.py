"""Acceptance test for mng schedule add with Modal deployment.

This test requires Modal credentials and network access. It is marked
with @pytest.mark.acceptance and @pytest.mark.timeout(600).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from imbue.mng_schedule.implementations.modal.deploy import get_modal_app_name

# Read the real home directory BEFORE the autouse fixture overrides HOME.
# This is needed because the subprocess needs the real Modal credentials.
_REAL_HOME = Path.home()


def _build_subprocess_env() -> dict[str, str]:
    """Build environment for subprocess calls that need real Modal credentials.

    The autouse test fixture overrides HOME to a temp directory for test isolation.
    Subprocesses need the real HOME to find ~/.modal.toml, git config, mng profiles,
    etc. We restore the real HOME and remove test isolation vars so the subprocess
    uses the real mng configuration (which has the correct Modal environment).
    """
    env = os.environ.copy()
    env["HOME"] = str(_REAL_HOME)
    # Remove test isolation vars that would interfere with the real mng config
    env.pop("MNG_HOST_DIR", None)
    env.pop("MNG_PREFIX", None)
    env.pop("MNG_ROOT_NAME", None)
    # Remove pytest marker so mng doesn't reject the call
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


@pytest.mark.release
@pytest.mark.timeout(600)
def test_schedule_add_deploys_to_modal() -> None:
    """Test that schedule add successfully deploys a cron function to Modal.

    This end-to-end test verifies the full flow:
    1. CLI parses arguments correctly
    2. Repo is packaged at the specified commit
    3. Modal App is deployed with the cron function
    4. Cleanup: stop/delete the deployed app
    """
    trigger_name = "test-schedule-add"
    app_name = get_modal_app_name(trigger_name)
    env = _build_subprocess_env()

    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "mng",
                "schedule",
                "add",
                trigger_name,
                "--command",
                "create",
                "--args",
                "test-agent echo --no-connect --await-ready --no-ensure-clean -- hello-from-schedule",
                "--schedule",
                "0 3 * * *",
                "--provider",
                "modal",
                "--verify",
                "none",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )

        assert result.returncode == 0, (
            f"schedule add failed with exit code {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert app_name in result.stdout or app_name in result.stderr, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        _cleanup_modal_app(app_name, env)


@pytest.mark.release
@pytest.mark.timeout(900)
def test_schedule_add_with_verification() -> None:
    """Test that schedule add with quick verification deploys and verifies.

    This test verifies the full flow including post-deploy verification:
    1. CLI deploys the cron function to Modal
    2. Invokes the function once via modal run
    3. Waits for the agent to start
    4. Destroys the verification agent
    5. Cleanup: stop/delete the deployed app
    """
    trigger_name = "test-schedule-verify"
    app_name = get_modal_app_name(trigger_name)
    env = _build_subprocess_env()

    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "mng",
                "schedule",
                "add",
                trigger_name,
                "--command",
                "create",
                "--args",
                "test-agent echo --no-connect --await-ready --no-ensure-clean -- hello-verify",
                "--schedule",
                "0 3 * * *",
                "--provider",
                "modal",
                "--verify",
                "quick",
            ],
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )

        assert result.returncode == 0, (
            f"schedule add with verify failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert app_name in result.stdout or app_name in result.stderr, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        _cleanup_modal_app(app_name, env)


@pytest.mark.release
@pytest.mark.timeout(600)
def test_schedule_list_shows_deployed_schedule() -> None:
    """Test that schedule list shows a schedule after it has been deployed.

    This end-to-end test verifies:
    1. schedule add deploys and saves a creation record
    2. schedule list --json reads and returns the saved record
    3. The record contains the expected trigger data
    """
    trigger_name = "test-schedule-list"
    app_name = get_modal_app_name(trigger_name)
    env = _build_subprocess_env()

    try:
        # Deploy a schedule
        add_result = subprocess.run(
            [
                "uv",
                "run",
                "mng",
                "schedule",
                "add",
                trigger_name,
                "--command",
                "create",
                "--args",
                "test-agent echo --no-connect --await-ready --no-ensure-clean -- hello-list-test",
                "--schedule",
                "0 4 * * *",
                "--provider",
                "modal",
                "--verify",
                "none",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        assert add_result.returncode == 0, (
            f"schedule add failed\nstdout: {add_result.stdout}\nstderr: {add_result.stderr}"
        )

        # List schedules and verify the deployed schedule appears
        list_result = subprocess.run(
            ["uv", "run", "mng", "schedule", "list", "--provider", "modal", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert list_result.returncode == 0, (
            f"schedule list failed\nstdout: {list_result.stdout}\nstderr: {list_result.stderr}"
        )

        list_data = json.loads(list_result.stdout)
        schedules = list_data.get("schedules", [])
        matching = [s for s in schedules if s["trigger"]["name"] == trigger_name]
        assert len(matching) == 1, f"Expected 1 schedule named '{trigger_name}', found {len(matching)} in {schedules}"

        record = matching[0]
        assert record["trigger"]["command"] == "CREATE"
        assert record["trigger"]["schedule_cron"] == "0 4 * * *"
        assert record["trigger"]["provider"] == "modal"
        assert record["app_name"] == app_name
        assert record["hostname"] != ""
        assert record["working_directory"] != ""
        assert record["full_commandline"] != ""
    finally:
        _cleanup_modal_app(app_name, env)


def _cleanup_modal_app(app_name: str, env: dict[str, str]) -> None:
    """Stop and clean up a Modal app created during testing."""
    try:
        list_result = subprocess.run(
            ["uv", "run", "modal", "app", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if list_result.returncode == 0:
            apps = json.loads(list_result.stdout)
            for app in apps:
                if app.get("Description", "") == app_name:
                    app_id = app.get("App ID", "")
                    if app_id:
                        subprocess.run(
                            ["uv", "run", "modal", "app", "stop", app_id],
                            capture_output=True,
                            timeout=30,
                            env=env,
                        )
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
