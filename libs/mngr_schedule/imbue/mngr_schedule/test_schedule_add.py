"""Release test for mngr schedule add with Modal deployment.

This test requires Modal credentials and network access. It is marked
with @pytest.mark.release and @pytest.mark.timeout(600).
"""

import json
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_schedule.implementations.modal.deploy import get_modal_app_name
from imbue.mngr_schedule.testing import build_subprocess_env
from imbue.mngr_schedule.testing import cleanup_modal_app
from imbue.mngr_schedule.testing import resolve_modal_environment


@pytest.mark.release
@pytest.mark.timeout(600)
def test_schedule_add_deploys_to_modal(monorepo_root: Path) -> None:
    """Test that schedule add successfully deploys a cron function to Modal.

    This end-to-end test verifies the full flow:
    1. CLI parses arguments correctly
    2. Repo is packaged at the specified commit
    3. Modal App is deployed with the cron function
    4. Cleanup: stop/delete the deployed app
    """
    trigger_name = "test-schedule-add"
    app_name = get_modal_app_name(trigger_name)
    env = build_subprocess_env()

    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "schedule",
                "add",
                trigger_name,
                "--command",
                "create",
                "--args",
                "test-agent echo --no-connect --no-ensure-clean -- hello-from-schedule",
                "--schedule",
                "0 3 * * *",
                "--provider",
                "modal",
                "--verify",
                "none",
                "--no-ensure-safe-commands",
                "--no-auto-merge",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
            cwd=monorepo_root,
        )

        assert result.returncode == 0, (
            f"schedule add failed with exit code {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert app_name in result.stdout or app_name in result.stderr, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        cleanup_modal_app(
            app_name,
            env,
            resolve_modal_environment(result.stderr if result is not None else ""),
            cwd=monorepo_root,
        )


@pytest.mark.release
@pytest.mark.timeout(1500)
def test_schedule_add_with_verification(monorepo_root: Path) -> None:
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
    env = build_subprocess_env()

    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "schedule",
                "add",
                trigger_name,
                "--command",
                "create",
                "--args",
                "test-agent echo --no-connect --no-ensure-clean -- hello-verify",
                "--schedule",
                "0 3 * * *",
                "--provider",
                "modal",
                "--verify",
                "quick",
                "--no-ensure-safe-commands",
                "--no-auto-merge",
            ],
            capture_output=True,
            text=True,
            timeout=1500,
            env=env,
            cwd=monorepo_root,
        )

        assert result.returncode == 0, (
            f"schedule add with verify failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert app_name in result.stdout or app_name in result.stderr, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        cleanup_modal_app(
            app_name,
            env,
            resolve_modal_environment(result.stderr if result is not None else ""),
            cwd=monorepo_root,
        )


@pytest.mark.release
@pytest.mark.timeout(600)
def test_schedule_list_shows_deployed_schedule(monorepo_root: Path) -> None:
    """Test that schedule list shows a schedule after it has been deployed.

    This end-to-end test verifies:
    1. schedule add deploys and saves a creation record
    2. schedule list --format=json reads and returns the saved record
    3. The record contains the expected trigger data
    """
    trigger_name = "test-schedule-list"
    app_name = get_modal_app_name(trigger_name)
    env = build_subprocess_env()

    add_result: subprocess.CompletedProcess[str] | None = None
    try:
        # Deploy a schedule
        add_result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "schedule",
                "add",
                trigger_name,
                "--command",
                "create",
                "--args",
                "test-agent echo --no-connect --no-ensure-clean -- hello-list-test",
                "--schedule",
                "0 4 * * *",
                "--provider",
                "modal",
                "--verify",
                "none",
                "--no-ensure-safe-commands",
                "--no-auto-merge",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
            cwd=monorepo_root,
        )
        assert add_result.returncode == 0, (
            f"schedule add failed\nstdout: {add_result.stdout}\nstderr: {add_result.stderr}"
        )

        # List schedules and verify the deployed schedule appears
        list_result = subprocess.run(
            ["uv", "run", "mngr", "schedule", "list", "--provider", "modal", "--format=json"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=monorepo_root,
        )
        assert list_result.returncode == 0, (
            f"schedule list failed\nstdout: {list_result.stdout}\nstderr: {list_result.stderr}"
        )

        list_data = json.loads(list_result.stdout)
        schedules = list_data.get("schedules", [])
        matching = [s for s in schedules if s["trigger"]["name"] == trigger_name]
        assert len(matching) == 1, (
            f"Expected 1 schedule named '{trigger_name}', found {len(matching)} in {schedules}\n"
            f"add stdout: {add_result.stdout[:500]}\n"
            f"add stderr: {add_result.stderr[:500]}\n"
            f"list stdout: {list_result.stdout[:500]}\n"
            f"list stderr: {list_result.stderr[:500]}\n"
            f"MNGR_PREFIX: {env.get('MNGR_PREFIX', 'NOT SET')}"
        )

        record = matching[0]
        assert record["trigger"]["command"] == "CREATE"
        assert record["trigger"]["schedule_cron"] == "0 4 * * *"
        assert record["trigger"]["provider"] == "modal"
        assert record["app_name"] == app_name
        assert record["hostname"] != ""
        assert record["working_directory"] != ""
        assert record["full_commandline"] != ""
    finally:
        cleanup_modal_app(
            app_name,
            env,
            resolve_modal_environment(add_result.stderr if add_result is not None else ""),
            cwd=monorepo_root,
        )
