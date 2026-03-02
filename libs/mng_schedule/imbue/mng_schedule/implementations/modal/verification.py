# Post-deploy verification for mng schedule.
#
# Verifies that a deployed schedule actually works by invoking it once
# via `modal run`, streaming output, detecting agent creation, and
# optionally destroying the verification agent.
#
# This module is excluded from unit test coverage because it requires
# real Modal and mng infrastructure to execute (similar to cron_runner.py).
# It is exercised by the acceptance test in test_schedule_add.py.
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.mng.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mng.api.list import load_all_agents_grouped_by_host
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng_schedule.errors import ScheduleDeployError

VERIFICATION_TIMEOUT_SECONDS: Final[float] = 900.0

# How long to wait for the agent to finish running after modal run completes.
_AGENT_FINISH_TIMEOUT_SECONDS: Final[float] = 3600.0

# How often to poll the agent's lifecycle state.
_AGENT_POLL_INTERVAL_SECONDS: Final[float] = 10.0

# How often to capture and log the agent's tmux pane content.
_SCREEN_CAPTURE_INTERVAL_SECONDS: Final[float] = 30.0

# Agent lifecycle states that indicate the agent is still actively running.
_RUNNING_STATES: Final[frozenset[AgentLifecycleState]] = frozenset(
    {AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING, AgentLifecycleState.REPLACED}
)


@pure
def build_modal_run_command(cron_runner_path: Path, modal_env_name: str) -> list[str]:
    """Build the modal run CLI command for invoking the deployed function once."""
    return ["uv", "run", "modal", "run", "--env", modal_env_name, str(cron_runner_path)]


# Regex to extract agent name from mng output.
# The mng create command logs a line like: "Starting agent <agent-name> ..."
_AGENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"Starting agent\s+(\S+)")


def _destroy_agent(agent_name: str) -> None:
    """Destroy an agent by name (no-op if it doesn't exist, since --force is used)."""
    logger.info("Destroying verification agent '{}'", agent_name)
    with ConcurrencyGroup(name="mng-destroy") as cg:
        result = cg.run_process_to_completion(
            ["uv", "run", "mng", "destroy", "--force", agent_name],
            is_checked_after=False,
            timeout=300.0,
        )
    if result.returncode != 0:
        logger.warning("mng destroy failed (exit {}): {}", result.returncode, result.stderr)


def _resolve_agent(agent_name: str, mng_ctx: MngContext) -> AgentInterface:
    """Resolve an agent by name to an AgentInterface object.

    Queries all providers to find the agent and returns a handle to it.
    The host is started if necessary, but the agent's lifecycle state is
    not checked (since we expect the agent to be running and want to poll
    its state ourselves).

    Raises UserInputError if the agent cannot be found.
    """
    agents_by_host, _ = load_all_agents_grouped_by_host(mng_ctx)
    agent, _host = find_and_maybe_start_agent_by_name_or_id(
        agent_name,
        agents_by_host,
        mng_ctx,
        "schedule-verify",
        is_start_desired=True,
        skip_agent_state_check=True,
    )
    return agent


def _wait_for_agent_to_finish(
    agent: AgentInterface,
    timeout_seconds: float = _AGENT_FINISH_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _AGENT_POLL_INTERVAL_SECONDS,
    screen_capture_interval_seconds: float = _SCREEN_CAPTURE_INTERVAL_SECONDS,
) -> None:
    """Wait for an agent to finish running, periodically capturing its screen.

    Polls the agent's lifecycle state until it is no longer in a running state
    (RUNNING, WAITING, or REPLACED). Periodically captures and logs the agent's
    tmux pane content via capture_pane_content() so the deployment progress can
    be monitored.

    Raises ScheduleDeployError if the timeout is exceeded.
    """
    logger.info("Waiting for agent '{}' to finish (timeout: {:.0f}s)", agent.name, timeout_seconds)

    start_time = time.monotonic()
    # Set to a value that triggers an immediate capture on the first iteration.
    last_capture_time = start_time - screen_capture_interval_seconds

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed > timeout_seconds:
            raise ScheduleDeployError(
                f"Timed out waiting for agent '{agent.name}' to finish after {timeout_seconds:.0f}s"
            )

        state = agent.get_lifecycle_state()
        if state not in _RUNNING_STATES:
            logger.info(
                "Agent '{}' finished with state: {} (after {:.0f}s)",
                agent.name,
                state,
                elapsed,
            )
            return

        # FIXME: when polling the agent here, we should *also* print a little "and this is how you can connect" message at the bottom, in case the user wants to connect and debug, eg, if the agent gets stuck

        # Capture and log the agent's screen periodically.
        now = time.monotonic()
        if now - last_capture_time >= screen_capture_interval_seconds:
            screen = agent.capture_pane_content()
            if screen is not None:
                logger.info("Agent '{}' screen capture:\n{}", agent.name, screen)
            else:
                logger.debug("Could not capture screen for agent '{}'", agent.name)
            last_capture_time = now

        time.sleep(poll_interval_seconds)


def _stream_process_output(
    process: subprocess.Popen[str],
    error_event: threading.Event,
    error_lines: list[str],
    # Mutable list to capture extracted agent name (thread-safe single write).
    agent_name_holder: list[str],
    trigger_name: str,
) -> None:
    """Read process stdout line by line, forwarding to console and extracting metadata."""
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()

        stripped = line.rstrip()
        lower = stripped.lower()

        # Detect errors (tracebacks/exceptions)
        if "traceback" in lower or "exception" in lower:
            error_lines.append(stripped)
            error_event.set()

        # Extract agent name from mng create output
        if not agent_name_holder:
            match = _AGENT_NAME_PATTERN.search(stripped)
            if match is not None:
                candidate = match.group(1)
                agent_name_holder.append(candidate)
                logger.debug("Extracted agent name from output: {}", candidate)


def verify_schedule_deployment(
    trigger_name: str,
    modal_env_name: str,
    is_finish_initial_run: bool,
    env: Mapping[str, str],
    cron_runner_path: Path,
    mng_ctx: MngContext,
    process_timeout_seconds: float = VERIFICATION_TIMEOUT_SECONDS,
) -> None:
    """Verify deployment by invoking the deployed function and waiting for it to exit.

    After modal deploy, this function:
    1. Runs `modal run` to invoke the deployed cron function once
    2. Streams output and monitors for errors
    3. Waits for the process to exit
    4. If is_finish_initial_run is False, destroys the agent after it starts
    5. If is_finish_initial_run is True, resolves the agent via the mng Python API,
       then polls its lifecycle state until it finishes, periodically capturing and
       logging the agent's tmux screen via capture_pane_content()
    6. Raises ScheduleDeployError on timeout, non-zero exit, or detected errors
    """
    cmd = build_modal_run_command(cron_runner_path, modal_env_name)
    logger.info("Invoking deployed function to verify deployment: {}", " ".join(cmd))

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=dict(env),
    )

    error_event = threading.Event()
    error_lines: list[str] = []
    agent_name_holder: list[str] = []

    log_thread = threading.Thread(
        target=_stream_process_output,
        args=(process, error_event, error_lines, agent_name_holder, trigger_name),
        daemon=True,
    )
    log_thread.start()

    try:
        exit_code = process.wait(timeout=process_timeout_seconds)

        # Wait for the log thread to finish processing remaining buffered output
        # so that agent_name_holder is fully populated before we read it.
        log_thread.join(timeout=5.0)

        extracted_agent_name = agent_name_holder[0] if agent_name_holder else None

        if error_event.is_set():
            if extracted_agent_name is not None:
                _destroy_agent(extracted_agent_name)
            error_detail = "\n".join(error_lines) if error_lines else "See output above"
            raise ScheduleDeployError(
                f"Error detected during deployment verification of schedule '{trigger_name}':\n{error_detail}"
            )

        if exit_code != 0:
            if extracted_agent_name is not None:
                _destroy_agent(extracted_agent_name)
            raise ScheduleDeployError(
                f"Deployment verification of schedule '{trigger_name}' failed "
                f"(modal run exited with code {exit_code}). See output above for details."
            )

        logger.info("modal run completed successfully for schedule '{}'", trigger_name)

        if is_finish_initial_run:
            if extracted_agent_name is not None:
                agent = _resolve_agent(extracted_agent_name, mng_ctx)
                _wait_for_agent_to_finish(agent)
            else:
                logger.warning(
                    "Could not extract agent name from output -- cannot wait for agent to finish. "
                    "The agent may still be running."
                )
        else:
            if extracted_agent_name is not None:
                _destroy_agent(extracted_agent_name)
            else:
                logger.warning(
                    "Could not extract agent name from output -- skipping cleanup. "
                    "The agent may still be running and will need manual cleanup."
                )

        logger.info("Deployment verification complete for schedule '{}'", trigger_name)

    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        log_thread.join(timeout=5.0)
        extracted_agent_name = agent_name_holder[0] if agent_name_holder else None
        if extracted_agent_name is not None:
            _destroy_agent(extracted_agent_name)
        raise ScheduleDeployError(
            f"Deployment verification of schedule '{trigger_name}' timed out after "
            f"{process_timeout_seconds}s. The modal run process was killed."
        ) from None

    except Exception:
        # Ensure process is cleaned up on any unexpected error
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
