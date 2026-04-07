"""Release test for mngr schedule run, remove, and list with Modal deployment.

This test requires Modal credentials and network access. It is marked
with @pytest.mark.release and @pytest.mark.timeout(900).

End-to-end flow:
1. Create a long-running agent on Modal (sleep 300) as a test host
2. Deploy a trigger that execs a marker file onto that agent
3. List triggers and verify the deployed trigger appears
4. Run the trigger via schedule run
5. Verify the trigger actually executed by checking for the marker file
6. Remove the trigger via schedule remove
7. Verify the trigger is gone from the list
8. Cleanup: destroy the agent
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

_MARKER_PATH = "/tmp/schedule-test-marker"


@pytest.mark.release
@pytest.mark.timeout(900)
def test_schedule_run_and_remove_modal_trigger() -> None:
    """Test the full schedule lifecycle: add, list, run, verify, remove."""
    trigger_name = "test-schedule-run"
    agent_name = "test-schedule-host"
    env = build_subprocess_env()
    disable_args = build_disable_plugin_args(_ENABLED_PLUGINS)

    try:
        # Step 1: Create a long-running agent on Modal as a test host.
        # The trigger will exec onto this agent to write a marker file.
        create_result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "create",
                agent_name,
                "sleep",
                "--no-connect",
                "--no-ensure-clean",
                "--context",
                "/tmp",
                "--provider",
                "modal",
                "--headless",
                *disable_args,
                "--",
                "300",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        assert create_result.returncode == 0, (
            f"mngr create failed\nstdout: {create_result.stdout}\nstderr: {create_result.stderr}"
        )

        # Step 2: Deploy a trigger that execs onto the test host.
        # The trigger writes a marker file so we can verify it ran.
        add_result = deploy_test_trigger(
            trigger_name,
            env,
            _ENABLED_PLUGINS,
            command="exec",
            args=f"{agent_name} -- touch {_MARKER_PATH}",
        )
        assert add_result.returncode == 0, (
            f"schedule add failed\nstdout: {add_result.stdout}\nstderr: {add_result.stderr}"
        )

        # Step 3: Verify the trigger appears in schedule list
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

        # Step 4: Run the trigger immediately via schedule run
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

        # Step 5: Verify the trigger actually executed by checking
        # for the marker file on the test host.
        verify_result = subprocess.run(
            ["uv", "run", "mngr", "exec", agent_name, "--", "cat", _MARKER_PATH, *disable_args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert verify_result.returncode == 0, (
            f"Marker file not found at {_MARKER_PATH} on agent {agent_name}. "
            f"The trigger may not have actually executed.\n"
            f"stdout: {verify_result.stdout}\nstderr: {verify_result.stderr}"
        )

        # Step 6: Remove the trigger
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

        # Step 7: Verify the trigger is gone from schedule list
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
        # Best-effort cleanup
        remove_test_trigger(trigger_name, env, _ENABLED_PLUGINS)
        subprocess.run(
            ["uv", "run", "mngr", "destroy", "--force", agent_name, *disable_args],
            capture_output=True,
            timeout=60,
            env=env,
        )
