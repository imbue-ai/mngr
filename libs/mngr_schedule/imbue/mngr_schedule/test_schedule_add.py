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
        combined_output = result.stdout + result.stderr
        assert app_name in combined_output, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # `--verify none` must skip the post-deploy verification entirely. The
        # deploy wraps verification in a `log_span("Verifying deployment of
        # schedule '{}'")` (deploy.py) that is only entered when verify_mode !=
        # NONE, and `verify_schedule_deployment` logs "Invoking deployed
        # function to verify deployment" (verification.py) only when it runs.
        # Both must be ABSENT here -- if a bug made `none` behave like quick or
        # full, one of these would appear.
        assert "Verifying deployment of schedule" not in combined_output, (
            f"Expected NO verification for --verify none, but found the verification log span\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "Invoking deployed function to verify deployment" not in combined_output, (
            f"Expected NO verification for --verify none, but the verifier was invoked\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        cleanup_modal_app(
            app_name,
            env,
            resolve_modal_environment(result.stderr if result is not None else ""),
            cwd=monorepo_root,
        )


@pytest.mark.release
@pytest.mark.timeout(500)
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
            timeout=500,
            env=env,
            cwd=monorepo_root,
        )

        assert result.returncode == 0, (
            f"schedule add with verify failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined_output = result.stdout + result.stderr
        assert app_name in combined_output, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Quick verify specifically: the verifier invokes the deployed function
        # with `--verify-mode quick` (logged by verification.py's "Invoking
        # deployed function to verify deployment" line, which echoes the joined
        # `modal run ... --verify-mode quick` command). Inside the container the
        # quick path destroys the agent and emits a result sentinel whose verify
        # block has status "destroyed" (cron_runner.py); that sentinel line is
        # streamed back verbatim to this subprocess's stdout.
        assert "--verify-mode quick" in combined_output, (
            f"Expected the verifier to run with --verify-mode quick\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert '"status": "destroyed"' in combined_output, (
            f"Expected quick verify to destroy the agent (sentinel status 'destroyed')\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # And it must NOT have taken the full-verify (poll-until-done) path,
        # which would emit a "finished" status instead.
        assert '"status": "finished"' not in combined_output, (
            f"Quick verify unexpectedly took the full-verify (poll) path\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        cleanup_modal_app(
            app_name,
            env,
            resolve_modal_environment(result.stderr if result is not None else ""),
            cwd=monorepo_root,
        )


@pytest.mark.release
@pytest.mark.timeout(500)
def test_schedule_add_with_full_verification(monorepo_root: Path) -> None:
    """Test that schedule add with full verification deploys and waits.

    Full-verify polls the agent's lifecycle state inside the cron runner's
    container until it reaches a terminal state (DONE/STOPPED). This is a
    different code path from quick-verify (which destroys the agent
    immediately): it exercises `_poll_until_done` and `mngr list` polling.

    The trigger creates an `echo` agent that finishes immediately, so the
    poll loop should exit on its first iteration with state == DONE.
    """
    trigger_name = "test-schedule-full-verify"
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
                "test-agent echo --no-connect --no-ensure-clean -- hello-full-verify",
                "--schedule",
                "0 3 * * *",
                "--provider",
                "modal",
                "--verify",
                "full",
                "--no-ensure-safe-commands",
                "--no-auto-merge",
            ],
            capture_output=True,
            text=True,
            timeout=500,
            env=env,
            cwd=monorepo_root,
        )

        assert result.returncode == 0, (
            f"schedule add with full verify failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined_output = result.stdout + result.stderr
        assert app_name in combined_output, (
            f"Expected app name '{app_name}' in output\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Full verify specifically: the verifier invokes the deployed function
        # with `--verify-mode full` (logged by verification.py's "Invoking
        # deployed function to verify deployment" line). Inside the container the
        # full path drives `_poll_until_done` (which shells `mngr list`) until the
        # agent reaches a terminal state, then emits a result sentinel whose
        # verify block has status "finished" with the terminal final_state
        # (cron_runner.py); that sentinel line is streamed back verbatim here.
        assert "--verify-mode full" in combined_output, (
            f"Expected the verifier to run with --verify-mode full\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert '"status": "finished"' in combined_output, (
            f"Expected full verify to poll the agent to a terminal state (sentinel status 'finished')\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # The echo agent finishes immediately, so the poll loop should record a
        # terminal-success state (DONE or STOPPED) rather than destroying the
        # agent like the quick path does.
        assert '"final_state": "DONE"' in combined_output or '"final_state": "STOPPED"' in combined_output, (
            f"Expected full verify to record a terminal-success final_state (DONE/STOPPED)\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert '"status": "destroyed"' not in combined_output, (
            f"Full verify unexpectedly took the quick (immediate-destroy) path\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
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
