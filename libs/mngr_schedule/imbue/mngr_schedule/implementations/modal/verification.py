# Post-deploy verification for mngr schedule.
#
# After `modal deploy` uploads the cron function, the deploy-side verifier
# invokes it once via `modal run` to make sure it actually works. All of the
# real verification work -- creating the agent, inspecting its lifecycle
# state, destroying it if needed -- happens inside the Modal container (see
# cron_runner.py). That way whichever provider hosts the agent also owns the
# verification; the deploying machine does not have to peek across provider
# boundaries.
#
# This module just:
# 1. Builds the `modal run` command (including --verify-mode)
# 2. Streams stdout/stderr to the terminal so users see live progress
# 3. Parses the sentinel line the runner emits at the end ("__MNGR_SCHEDULE_VERIFY__ {...}")
# 4. Translates the structured result into success or ScheduleDeployError
#
# This module is excluded from unit test coverage because it requires real
# Modal and mngr infrastructure to execute (similar to cron_runner.py).
# It is exercised by the acceptance test in test_schedule_add.py.
import json
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_schedule.data_types import VerifyMode
from imbue.mngr_schedule.errors import ScheduleDeployError
from imbue.mngr_schedule.implementations.modal.cron_runner_constants import AGENT_MISSING_STATE

# Three timers stack for full-verify:
#   1. cron_runner._AGENT_FINISH_TIMEOUT_SECONDS (~3000s) -- inner poll.
#      Kept small enough that the poll + destroy + sentinel-emit path fits
#      inside (2) even after `mngr create` has already consumed part of the
#      function's budget.
#   2. cron_runner @app.function(timeout=3600) -- Modal-side wall clock.
#   3. VERIFICATION_TIMEOUT_SECONDS -- local `modal run` subprocess wall
#      clock, which MUST exceed (2) with enough headroom for CLI spin-up,
#      container cold start, and final sentinel flush. Otherwise a late
#      successful verify gets killed by the client before we read the
#      sentinel line. We mirror the Modal function timeout here and add
#      a generous headroom so the client never fires first.
_MODAL_FUNCTION_TIMEOUT_SECONDS: Final[float] = 3600.0
_CLIENT_HEADROOM_SECONDS: Final[float] = 300.0
VERIFICATION_TIMEOUT_SECONDS: Final[float] = _MODAL_FUNCTION_TIMEOUT_SECONDS + _CLIENT_HEADROOM_SECONDS

# Must match cron_runner._RESULT_SENTINEL exactly.
_RESULT_SENTINEL: Final[str] = "__MNGR_SCHEDULE_VERIFY__"

# Terminal states we accept as a successful full-verify outcome. Built from
# the AgentLifecycleState enum so that adding a new state (or renaming one)
# is caught at import time rather than silently treating the new state as a
# failure.
_TERMINAL_SUCCESS_STATES: Final[frozenset[str]] = frozenset(
    {AgentLifecycleState.DONE.value, AgentLifecycleState.STOPPED.value}
)

# Matches the sentinel anywhere on a line (so Modal-side log prefixes such as
# container ids or timestamps don't defeat detection) and captures the JSON
# payload that follows it. Greedy match up to end-of-line so that payloads
# containing braces / quotes are captured in full.
_SENTINEL_PATTERN: Final[re.Pattern[str]] = re.compile(re.escape(_RESULT_SENTINEL) + r"\s+(\{.*\})\s*$")


@pure
def build_modal_run_command(
    cron_runner_path: Path,
    modal_env_name: str,
    verify_mode: VerifyMode,
) -> list[str]:
    """Build the `modal run` CLI command for invoking the deployed function once.

    The --verify-mode flag is passed through to run_scheduled_trigger so it
    knows whether to skip verify, destroy the created agent, or wait for it
    to finish.
    """
    return [
        "uv",
        "run",
        "modal",
        "run",
        "--env",
        modal_env_name,
        f"{cron_runner_path}::run_scheduled_trigger",
        "--verify-mode",
        verify_mode.value.lower(),
    ]


def _stream_and_capture(
    process: subprocess.Popen[str],
    error_event: threading.Event,
    error_lines: list[str],
    sentinel_holder: list[dict[str, Any]],
) -> None:
    """Stream subprocess stdout to the console, capturing errors and the sentinel line."""
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()

        stripped = line.rstrip()

        # Detect the structured result sentinel first. If this line is the
        # sentinel, skip the generic error-keyword scan: the sentinel's JSON
        # payload may legitimately contain substrings like "exception" (e.g.
        # inside a captured destroy_stderr) and those should not be
        # mis-interpreted as an in-container failure.
        if not sentinel_holder:
            match = _SENTINEL_PATTERN.search(stripped)
            if match is not None:
                payload = match.group(1)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    logger.warning("Could not parse verify sentinel payload: {}", payload)
                    continue
                if isinstance(parsed, dict):
                    sentinel_holder.append(parsed)
                else:
                    logger.warning("Verify sentinel payload was not a JSON object: {}", payload)
                continue

        lower = stripped.lower()
        if "traceback" in lower or "exception" in lower:
            error_lines.append(stripped)
            error_event.set()


def verify_schedule_deployment(
    trigger_name: str,
    modal_env_name: str,
    verify_mode: VerifyMode,
    env: Mapping[str, str],
    cron_runner_path: Path,
    process_timeout_seconds: float = VERIFICATION_TIMEOUT_SECONDS,
) -> None:
    """Invoke the deployed cron function once to verify deployment.

    Runs `modal run ::run_scheduled_trigger --verify-mode <mode>` and reads
    the structured result the runner emits on a single sentinel line. All
    agent-side work (extraction, destroy, lifecycle polling) happens inside
    the container, so this function only needs to interpret the result.

    Raises ScheduleDeployError on timeout, non-zero exit, detected errors,
    missing sentinel, or a verify result that indicates failure (e.g. full
    verify finishing in a non-terminal-success state, or verify unable to
    extract an agent name from mngr create output).

    The caller should only invoke this when verify_mode != VerifyMode.NONE.
    """
    assert verify_mode != VerifyMode.NONE, "verify_schedule_deployment called with NONE"

    cmd = build_modal_run_command(cron_runner_path, modal_env_name, verify_mode)
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
    sentinel_holder: list[dict[str, Any]] = []

    log_thread = threading.Thread(
        target=_stream_and_capture,
        args=(process, error_event, error_lines, sentinel_holder),
        daemon=True,
    )
    log_thread.start()

    try:
        exit_code = process.wait(timeout=process_timeout_seconds)
        # Let the streaming thread drain any final buffered output so the
        # sentinel is captured before we inspect it.
        log_thread.join(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        log_thread.join(timeout=5.0)
        raise ScheduleDeployError(
            f"Deployment verification of schedule '{trigger_name}' timed out after "
            f"{process_timeout_seconds}s. The modal run process was killed."
        ) from None

    # Precedence: the sentinel is the authoritative structured result emitted
    # by the runner. If we have a sentinel AND the process exited cleanly,
    # trust the sentinel and ignore the error-keyword heuristic -- the runner's
    # own diagnostic prints (e.g. stderr tails from a transient `mngr list`
    # failure in the poll loop) may legitimately contain "exception" or
    # "traceback" substrings, which would otherwise cause a spurious
    # ScheduleDeployError despite the runner reporting success.
    #
    # If there is no sentinel, the runner crashed or was killed before emitting
    # one; fall back to the heuristic: prefer the collected error lines if any
    # were seen, otherwise report "no sentinel".
    if exit_code != 0:
        raise ScheduleDeployError(
            f"Deployment verification of schedule '{trigger_name}' failed "
            f"(modal run exited with code {exit_code}). See output above for details."
        )

    if not sentinel_holder:
        if error_event.is_set():
            error_detail = "\n".join(error_lines) if error_lines else "See output above"
            raise ScheduleDeployError(
                f"Error detected during deployment verification of schedule '{trigger_name}':\n{error_detail}"
            )
        raise ScheduleDeployError(
            f"Deployment verification of schedule '{trigger_name}' did not emit a result sentinel. "
            "The cron function may have exited before the verify step completed."
        )

    result = sentinel_holder[0]
    verify_block = result.get("verify")
    if verify_block is None:
        # Runner ran successfully but skipped verify (e.g. non-create command).
        # Treat as success.
        logger.info("Deployment verification complete for schedule '{}'", trigger_name)
        return

    status = verify_block.get("status")
    if status == "no_agent_name":
        raise ScheduleDeployError(
            f"Deployment verification of schedule '{trigger_name}' could not extract the "
            "created agent name from mngr output, so the agent could not be "
            "destroyed or polled. It may still be running and need manual cleanup."
        )

    if status == "destroyed":
        destroy_exit_code = verify_block.get("destroy_exit_code")
        if destroy_exit_code not in (0, None):
            logger.warning(
                "Quick verification: `mngr destroy` exited {} for agent '{}':\n{}",
                destroy_exit_code,
                result.get("agent_name"),
                verify_block.get("destroy_stderr", ""),
            )
        logger.info(
            "Deployment verification complete for schedule '{}' (agent '{}' destroyed)",
            trigger_name,
            result.get("agent_name"),
        )
        return

    if status == "finished":
        final_state = verify_block.get("final_state")
        if final_state in _TERMINAL_SUCCESS_STATES:
            logger.info(
                "Deployment verification complete for schedule '{}' (agent '{}' finished with state {})",
                trigger_name,
                result.get("agent_name"),
                final_state,
            )
            return
        if final_state == AGENT_MISSING_STATE:
            raise ScheduleDeployError(
                f"Full verification of schedule '{trigger_name}' could not find agent "
                f"{result.get('agent_name')!r} in `mngr list` output before it reached a terminal "
                f"state. The agent may have been destroyed externally or never registered with the "
                f"local provider inside the container."
            )
        raise ScheduleDeployError(
            f"Full verification of schedule '{trigger_name}' finished with non-terminal-success "
            f"state {final_state!r} for agent {result.get('agent_name')!r}."
        )

    if status == "timeout":
        timeout_message = verify_block.get("timeout_message", "")
        destroy_exit_code = verify_block.get("destroy_exit_code")
        destroy_stderr = verify_block.get("destroy_stderr", "")
        raise ScheduleDeployError(
            f"Full verification of schedule '{trigger_name}' timed out waiting for agent "
            f"{result.get('agent_name')!r} to finish: {timeout_message}. "
            f"Best-effort destroy exited {destroy_exit_code!r}"
            f"{f' with stderr: {destroy_stderr}' if destroy_stderr else ''}."
        )

    raise ScheduleDeployError(
        f"Deployment verification of schedule '{trigger_name}' returned unexpected verify status: {status!r}"
    )
