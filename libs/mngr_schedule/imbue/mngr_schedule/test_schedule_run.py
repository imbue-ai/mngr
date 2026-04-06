"""Release test for mngr schedule run and schedule remove with Modal deployment.

This test requires Modal credentials and network access. It is marked
with @pytest.mark.release and @pytest.mark.timeout(900).

End-to-end flow:
1. Deploy a trigger via schedule add (with --verify none for speed)
2. Run it immediately via schedule run --provider modal
3. Verify it completed successfully
4. Remove it via schedule remove --provider modal --force
5. Verify the trigger is no longer listed
"""

import json
import subprocess

import pytest

from imbue.mngr_schedule.testing import build_disable_plugin_args
from imbue.mngr_schedule.testing import build_subprocess_env
from imbue.mngr_schedule.testing import deploy_test_trigger
from imbue.mngr_schedule.testing import remove_test_trigger

# Only the schedule and modal plugins are needed for this test.
# All other plugins are disabled to avoid needing their credentials
# (e.g. ANTHROPIC_API_KEY for the claude plugin).
_ENABLED_PLUGINS = frozenset({"schedule", "modal"})


@pytest.mark.release
@pytest.mark.timeout(900)
def test_schedule_run_and_remove_modal_trigger() -> None:
    """Test schedule run and schedule remove against a deployed Modal trigger.

    Deploys a trigger, runs it, removes it, and verifies each step.
    """
    trigger_name = "test-schedule-run"
    env = build_subprocess_env()
    disable_args = build_disable_plugin_args(_ENABLED_PLUGINS)

    try:
        # Step 1: Deploy the trigger (--verify none because the schedule run
        # call below IS the test -- it exercises invoke_modal_trigger_function,
        # a completely different code path from --verify quick which uses
        # `modal run` CLI instead of the SDK's Function.from_name().remote())
        add_result = deploy_test_trigger(trigger_name, env, _ENABLED_PLUGINS)
        assert add_result.returncode == 0, (
            f"schedule add failed\nstdout: {add_result.stdout}\nstderr: {add_result.stderr}"
        )

        # Step 2: Verify the trigger appears in schedule list
        list_result = subprocess.run(
            ["uv", "run", "mngr", "schedule", "list", "--provider", "modal", "--format=json", *disable_args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert list_result.returncode == 0, (
            f"schedule list failed\nstdout: {list_result.stdout}\nstderr: {list_result.stderr}"
        )
        list_data = json.loads(list_result.stdout)
        trigger_names = [s["trigger"]["name"] for s in list_data.get("schedules", [])]
        assert trigger_name in trigger_names, (
            f"Deployed trigger '{trigger_name}' not found in schedule list: {trigger_names}"
        )

        # Step 3: Run the trigger immediately
        run_result = subprocess.run(
            ["uv", "run", "mngr", "schedule", "run", trigger_name, "--provider", "modal", *disable_args],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        assert run_result.returncode == 0, (
            f"schedule run failed\nstdout: {run_result.stdout}\nstderr: {run_result.stderr}"
        )

        # Verify the deployed function actually executed by checking for
        # output from cron_runner.py's run_scheduled_trigger(), which prints
        # "Running: mngr create ..." before invoking the command.
        combined_output = run_result.stdout + run_result.stderr
        assert "Running:" in combined_output and "mngr create" in combined_output, (
            f"schedule run exited 0 but the trigger function does not appear to have "
            f"executed (expected 'Running: mngr create ...' in output)\n"
            f"stdout: {run_result.stdout}\nstderr: {run_result.stderr}"
        )

        # Verify the echo agent actually ran by checking for the passthrough
        # message in the output.
        assert "hello-from-schedule-run" in combined_output, (
            f"The echo agent's passthrough message was not found in the output. "
            f"The trigger may have started but the agent may not have executed.\n"
            f"stdout: {run_result.stdout}\nstderr: {run_result.stderr}"
        )

        # Step 4: Remove the trigger
        remove_result = subprocess.run(
            ["uv", "run", "mngr", "schedule", "remove", trigger_name, "--provider", "modal", "--force", *disable_args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert remove_result.returncode == 0, (
            f"schedule remove failed\nstdout: {remove_result.stdout}\nstderr: {remove_result.stderr}"
        )

        # Step 5: Verify the trigger is gone from schedule list
        list_after_result = subprocess.run(
            ["uv", "run", "mngr", "schedule", "list", "--provider", "modal", "--format=json", *disable_args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert list_after_result.returncode == 0, (
            f"schedule list after remove failed\nstdout: {list_after_result.stdout}\nstderr: {list_after_result.stderr}"
        )
        list_after_data = json.loads(list_after_result.stdout)
        remaining_names = [s["trigger"]["name"] for s in list_after_data.get("schedules", [])]
        assert trigger_name not in remaining_names, (
            f"Trigger '{trigger_name}' still appears in schedule list after removal: {remaining_names}"
        )

    finally:
        # Best-effort cleanup in case a step failed before remove
        remove_test_trigger(trigger_name, env, _ENABLED_PLUGINS)
