"""Release test for mngr schedule run with Modal deployment.

This test requires Modal credentials and network access. It is marked
with @pytest.mark.release and @pytest.mark.timeout(900).

End-to-end flow:
1. Deploy a trigger via schedule add (with --verify none for speed)
2. Run it immediately via schedule run --provider modal
3. Verify it completed successfully
4. Cleanup: stop/delete the deployed Modal app
"""

import subprocess

import pytest

from imbue.mngr_schedule.implementations.modal.deploy import get_modal_app_name
from imbue.mngr_schedule.testing import build_disable_plugin_args
from imbue.mngr_schedule.testing import build_subprocess_env
from imbue.mngr_schedule.testing import cleanup_modal_app
from imbue.mngr_schedule.testing import deploy_test_trigger

# Only the schedule and modal plugins are needed for this test.
# All other plugins are disabled to avoid needing their credentials
# (e.g. ANTHROPIC_API_KEY for the claude plugin).
_ENABLED_PLUGINS = frozenset({"schedule", "modal"})


@pytest.mark.release
@pytest.mark.timeout(900)
def test_schedule_run_invokes_modal_trigger() -> None:
    """Test that schedule run invokes a deployed trigger on Modal.

    Deploys a trigger, then immediately runs it. The trigger uses a
    simple echo agent that exits quickly, so the run should complete
    within the timeout.
    """
    trigger_name = "test-schedule-run"
    app_name = get_modal_app_name(trigger_name)
    env = build_subprocess_env()

    try:
        # Step 1: Deploy the trigger (--verify none because the schedule run
        # call below IS the test -- it exercises invoke_modal_trigger_function,
        # a completely different code path from --verify quick which uses
        # `modal run` CLI instead of the SDK's Function.from_name().remote())
        add_result = deploy_test_trigger(trigger_name, env, _ENABLED_PLUGINS)
        assert add_result.returncode == 0, (
            f"schedule add failed\nstdout: {add_result.stdout}\nstderr: {add_result.stderr}"
        )

        # Step 2: Run the trigger immediately
        run_result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "schedule",
                "run",
                trigger_name,
                "--provider",
                "modal",
                *build_disable_plugin_args(_ENABLED_PLUGINS),
            ],
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
    finally:
        cleanup_modal_app(app_name, env)
