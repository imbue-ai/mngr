#!/usr/bin/env python3
import json
import subprocess
import time

import click


def parse_agent_identifier(stdout: str, stderr: str) -> str:
    """Parse agent name or ID from mngr create output.

    Tries JSON stdout first (for --format json/jsonl), then falls back
    to scanning stderr for the "Agent name: " pattern (loguru output
    in background/human mode).
    """
    # Try parsing JSON from stdout (--format json outputs a single JSON object,
    # --format jsonl outputs one JSON object per line)
    stdout_lines = stdout.strip().splitlines()
    for line in stdout_lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict) and "agent_id" in data:
                return data["agent_id"]
        except json.JSONDecodeError:
            continue

    # Fallback: scan stderr for "Agent name: <name>" pattern (loguru output)
    all_lines = stdout_lines + stderr.strip().splitlines()
    for line in all_lines:
        if "Agent name:" in line:
            return line.split("Agent name:", 1)[-1].strip()

    raise click.ClickException(
        f"Could not parse agent identifier from command output.\nstdout: {stdout}\nstderr: {stderr}"
    )


def find_agent_state_in_jsonl(jsonl_output: str, agent_identifier: str) -> str | None:
    """Parse JSONL output from mngr list to find an agent's state.

    Returns the state string (e.g. "WAITING", "RUNNING") or None if not found.
    """
    for line in jsonl_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # Match on name or id
        if data.get("name") == agent_identifier or data.get("id") == agent_identifier:
            return data.get("state")

    return None


def get_agent_state(agent_identifier: str) -> str | None:
    """Get the lifecycle state of an agent by running mngr list.

    Returns the state string (e.g. "WAITING", "RUNNING") or None if not found.
    Logs a warning if mngr list fails.
    """
    result = subprocess.run(
        ["uv", "run", "mngr", "list", "--provider", "local", "--format", "jsonl"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        click.echo(
            f"Warning: mngr list failed (exit code {result.returncode}): {result.stderr.strip()}",
            err=True,
        )
        return None

    return find_agent_state_in_jsonl(result.stdout, agent_identifier)


def wait_for_agent_completion(
    agent_identifier: str,
    max_task_time: float,
    poll_interval: float,
) -> str | None:
    """Poll mngr list until agent reaches a terminal state or timeout.

    Returns the state when the agent is no longer RUNNING, or None if max_task_time is exceeded.
    """
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_task_time:
            return None

        state = get_agent_state(agent_identifier)

        # Any terminal state means the agent is no longer actively running
        if state != "RUNNING":
            return state

        time.sleep(poll_interval)


def stop_agent(agent_identifier: str) -> None:
    """Stop a timed-out agent via mngr stop.

    Raises click.ClickException if the stop command fails.
    """
    result = subprocess.run(
        ["uv", "run", "mngr", "stop", agent_identifier],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"Failed to stop agent {agent_identifier} (exit code {result.returncode}).\nstderr: {result.stderr}"
        )


@click.command()
@click.option(
    "--command-template",
    required=True,
    help="Command template with {idx} and {prev_idx} placeholders",
)
@click.option(
    "--max-task-count",
    type=int,
    required=True,
    help="Maximum number of tasks to run",
)
@click.option(
    "--max-total-time",
    type=float,
    required=True,
    help="Maximum total time in seconds",
)
@click.option(
    "--max-task-time",
    type=float,
    required=True,
    help="Maximum time per task in seconds",
)
@click.option(
    "--poll-interval",
    type=float,
    default=5.0,
    help="Polling interval in seconds for checking agent state",
)
def main(
    command_template: str,
    max_task_count: int,
    max_total_time: float,
    max_task_time: float,
    poll_interval: float,
) -> None:
    """Launch mngr agents repeatedly with incremented arguments.

    Each agent is given an index (idx) and previous index (prev_idx).
    The workflow polls mngr list to detect when each agent reaches the
    WAITING state. On timeout, the agent is stopped and the same task
    is retried. The workflow continues until max_task_count tasks have
    completed or max_total_time has elapsed.
    """
    start_time = time.time()
    idx = 1
    last_successful_idx = 0

    while idx <= max_task_count:
        elapsed_total = time.time() - start_time

        if elapsed_total >= max_total_time:
            click.echo(f"Reached max total time ({max_total_time}s), stopping")
            break

        prev_idx = last_successful_idx
        command = command_template.format(idx=idx, prev_idx=prev_idx)

        click.echo(f"Task {idx}: Launching command: {command}")

        # Launch command and capture output to parse agent identifier
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise click.ClickException(
                f"Command failed (exit code {result.returncode}): {command}\nstderr: {result.stderr}"
            )
        agent_identifier = parse_agent_identifier(result.stdout, result.stderr)

        click.echo(f"Task {idx}: Waiting for agent {agent_identifier} to complete")

        # Poll mngr list until WAITING or timeout
        final_state = wait_for_agent_completion(
            agent_identifier=agent_identifier,
            max_task_time=max_task_time,
            poll_interval=poll_interval,
        )

        if final_state in ("WAITING", "DONE"):
            click.echo(f"Task {idx}: Complete")
            last_successful_idx = idx
        elif final_state is None:
            click.echo(f"Task {idx}: Timed out, stopping agent and retrying")
            stop_agent(agent_identifier)
        else:
            click.echo(f"Task {idx}: Agent entered unexpected state '{final_state}', stopping agent and retrying")
            stop_agent(agent_identifier)

        # no matter what we increment the index
        idx += 1


if __name__ == "__main__":
    main()
