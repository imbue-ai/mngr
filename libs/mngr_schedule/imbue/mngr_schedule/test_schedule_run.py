"""Release test for mngr schedule run, remove, and list with Modal deployment.

This test requires Modal credentials and network access. It is marked
with @pytest.mark.release and @pytest.mark.timeout(900).

End-to-end flow:
1. Deploy a trigger that runs echo inside the container
2. List triggers and verify the deployed trigger appears
3. Run the trigger via schedule run
4. Verify the echo output is returned (proves the command actually ran)
5. Remove the trigger via schedule remove
6. Verify the trigger is gone from the list
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
    """Test the full schedule lifecycle: add, list, run, verify, remove."""
    trigger_name = "test-schedule-run"
    env = build_subprocess_env()
    disable_args = build_disable_plugin_args(_ENABLED_PLUGINS)

    try:
        # Step 1: Deploy a trigger that creates an echo agent.
        # The echo agent runs /bin/echo with the passthrough args, producing
        # output that run_scheduled_trigger captures and returns via fn.remote().
        # --verify none because the schedule run call IS the test (different
        # code path from --verify quick which uses `modal run` CLI).
        add_result = deploy_test_trigger(
            trigger_name,
            env,
            _ENABLED_PLUGINS,
            command="create",
            args="test-agent echo --no-connect --no-ensure-clean --context /tmp --branch :run-{DATE} -- hello-from-schedule-run",
        )
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

        # Step 3: Run the trigger immediately via schedule run.
        # run_scheduled_trigger() returns the full command output, which
        # invoke_modal_trigger_function() returns via fn.remote(), and the
        # CLI prints to stdout.
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

        # Step 4: Verify the echo output proves the command ran.
        # The echo agent passes "hello-from-schedule-run" through /bin/echo,
        # which appears in the captured output returned by fn.remote().
        assert "hello-from-schedule-run" in run_result.stdout, (
            f"Expected echo output 'hello-from-schedule-run' in stdout. "
            f"The trigger may not have actually executed the command.\n"
            f"stdout: {run_result.stdout}\nstderr: {run_result.stderr}"
        )

        # Step 5: Remove the trigger
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

        # Step 6: Verify the trigger is gone from schedule list
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
