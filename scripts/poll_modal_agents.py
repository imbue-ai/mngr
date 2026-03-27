#!/usr/bin/env python3
"""Polls modal agents and notifies when any agent leaves the RUNNING state."""

import json
import subprocess
import time

import click


def parse_all_agents_from_jsonl(jsonl_output: str) -> dict[str, str]:
    """Parse JSONL output from mngr list to extract all agents and their states.

    Returns a mapping from agent name to state string.
    """
    state_by_name: dict[str, str] = {}
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
        name = data.get("name")
        state = data.get("state")
        if name and state:
            state_by_name[name] = state
    return state_by_name


def fetch_modal_agent_states() -> dict[str, str] | None:
    """Fetch current states of all modal agents by running mngr list.

    Returns a mapping from agent name to state, or None if the command fails.
    """
    result = subprocess.run(
        ["uv", "run", "mngr", "list", "--provider", "modal", "--format", "jsonl"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        click.echo(
            f"Warning: mngr list failed (exit code {result.returncode}): {result.stderr.strip()}",
            err=True,
        )
        return None

    return parse_all_agents_from_jsonl(result.stdout)


def notify_user(message: str) -> None:
    """Send a notification to the user via notify_user command."""
    try:
        subprocess.run(
            ["notify_user", message],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        click.echo(f"Warning: notify_user command not found. Message: {message}", err=True)


def detect_state_transitions(
    previous_state_by_name: dict[str, str],
    current_state_by_name: dict[str, str],
    known_agent_names: set[str],
) -> list[tuple[str, str, str]]:
    """Detect agents that have transitioned away from RUNNING.

    Returns a list of (agent_name, old_state, new_state) tuples for:
    - Agents that were previously RUNNING but are now in a different state
    - New agents that appear already in a non-RUNNING state (finished between polls)
    """
    transitions: list[tuple[str, str, str]] = []

    # Check agents that were RUNNING in the previous poll
    for name, old_state in previous_state_by_name.items():
        if old_state != "RUNNING":
            continue
        new_state = current_state_by_name.get(name)
        if new_state is None:
            transitions.append((name, old_state, "GONE"))
        elif new_state != "RUNNING":
            transitions.append((name, old_state, new_state))

    # Check for new agents that appeared already in a non-RUNNING state
    # (they ran and finished between two polls, so we never saw them as RUNNING)
    for name, state in current_state_by_name.items():
        if name not in known_agent_names and state != "RUNNING":
            transitions.append((name, "RUNNING", state))

    return transitions


@click.command()
@click.option(
    "--poll-interval",
    type=float,
    default=30.0,
    help="Polling interval in seconds",
)
def main(poll_interval: float) -> None:
    """Poll modal agents and notify when any agent finishes.

    Periodically checks the status of all agents on the modal provider.
    When an agent transitions out of the RUNNING state, sends a notification
    via the notify_user command.
    """
    click.echo(f"Polling modal agents every {poll_interval}s (Ctrl+C to stop)")

    # Initial fetch to establish baseline
    previous_state_by_name = fetch_modal_agent_states()
    if previous_state_by_name is None:
        previous_state_by_name = {}

    # Track all agents we've ever seen so we can detect new agents that
    # appear already finished (ran and completed between two polls)
    known_agent_names: set[str] = set(previous_state_by_name.keys())

    running_count = sum(1 for state in previous_state_by_name.values() if state == "RUNNING")
    click.echo(f"Initial state: {len(previous_state_by_name)} agents, {running_count} running")

    try:
        while True:
            time.sleep(poll_interval)

            current_state_by_name = fetch_modal_agent_states()
            if current_state_by_name is None:
                continue

            # Detect transitions away from RUNNING (including new agents that finished between polls)
            transitions = detect_state_transitions(previous_state_by_name, current_state_by_name, known_agent_names)

            for agent_name, old_state, new_state in transitions:
                message = f"Agent '{agent_name}' finished ({old_state} -> {new_state})"
                click.echo(message)
                notify_user(message)

            known_agent_names.update(current_state_by_name.keys())
            previous_state_by_name = current_state_by_name

    except KeyboardInterrupt:
        click.echo("\nStopped polling")


if __name__ == "__main__":
    main()
