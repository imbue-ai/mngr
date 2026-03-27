"""Resolve current agent and host names from the discovery event stream.

This is a standalone script that uses ONLY stdlib -- no mngr imports, no
third-party libraries. This is intentional: it runs on every TAB press and
must be as fast as possible.

It reads the discovery event stream JSONL file, finds the latest full
snapshot, then replays incremental events to determine which agents and
hosts are currently active.

Usage:
    python -m imbue.mngr.cli.complete_names
    python -m imbue.mngr.cli.complete_names --hosts
    python -m imbue.mngr.cli.complete_names --both
"""

import json
import os
import sys
from pathlib import Path


def _get_discovery_events_path() -> Path:
    """Return the path to the discovery events JSONL file.

    Mirrors the logic in host_dir.py and discovery_events.py without
    importing them.
    """
    env_host_dir = os.environ.get("MNGR_HOST_DIR")
    if env_host_dir:
        base_dir = Path(env_host_dir).expanduser()
    else:
        root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
        base_dir = Path(f"~/.{root_name}").expanduser()
    return base_dir / "events" / "mngr" / "discovery" / "events.jsonl"


def _find_last_full_snapshot_line_idx(lines: list[str]) -> int:
    """Find the index of the last DISCOVERY_FULL line in the given list.

    Reverse-scans for efficiency. Returns -1 if no full snapshot is found.
    """
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        # Quick string check before parsing JSON
        if '"DISCOVERY_FULL"' not in line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "DISCOVERY_FULL":
                return idx
        except json.JSONDecodeError:
            continue
    return -1


def resolve_names_from_discovery_stream(
    events_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Read the discovery event stream and return current (agent_names, host_names).

    Finds the latest DISCOVERY_FULL snapshot, then replays all subsequent
    events to determine which agents and hosts are currently active.
    """
    if events_path is None:
        events_path = _get_discovery_events_path()

    if not events_path.exists():
        return [], []

    try:
        all_lines = events_path.read_text().splitlines()
    except OSError:
        return [], []

    if not all_lines:
        return [], []

    # Find the last full snapshot and replay from there
    last_full_idx = _find_last_full_snapshot_line_idx(all_lines)
    start_idx = last_full_idx if last_full_idx >= 0 else 0
    lines_to_replay = all_lines[start_idx:]

    # Map from agent_id -> agent_name for currently active agents
    agent_name_by_id: dict[str, str] = {}
    # Map from host_id -> host_name for currently active hosts
    host_name_by_id: dict[str, str] = {}

    for line in lines_to_replay:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_type = data.get("type")
        if event_type == "DISCOVERY_FULL":
            # Reset state from the full snapshot
            agent_name_by_id.clear()
            host_name_by_id.clear()
            for agent in data.get("agents", ()):
                agent_id = agent.get("agent_id", "")
                agent_name = agent.get("agent_name", "")
                if agent_id and agent_name:
                    agent_name_by_id[agent_id] = agent_name
            for host in data.get("hosts", ()):
                host_id = host.get("host_id", "")
                host_name = host.get("host_name", "")
                if host_id and host_name:
                    host_name_by_id[host_id] = host_name

        elif event_type == "AGENT_DISCOVERED":
            agent = data.get("agent", {})
            agent_id = agent.get("agent_id", "")
            agent_name = agent.get("agent_name", "")
            if agent_id and agent_name:
                agent_name_by_id[agent_id] = agent_name

        elif event_type == "HOST_DISCOVERED":
            host = data.get("host", {})
            host_id = host.get("host_id", "")
            host_name = host.get("host_name", "")
            if host_id and host_name:
                host_name_by_id[host_id] = host_name

        elif event_type == "AGENT_DESTROYED":
            agent_id = data.get("agent_id", "")
            if agent_id:
                agent_name_by_id.pop(agent_id, None)

        elif event_type == "HOST_DESTROYED":
            host_id = data.get("host_id", "")
            if host_id:
                host_name_by_id.pop(host_id, None)
                # Remove all agents belonging to this host
                for agent_id in data.get("agent_ids", []):
                    agent_name_by_id.pop(agent_id, None)

        else:
            pass

    agent_names = sorted(set(agent_name_by_id.values()))
    host_names = sorted(set(host_name_by_id.values()))
    return agent_names, host_names


def main() -> None:
    """Print agent names (or host names with --hosts, or both with --both) to stdout."""
    args = sys.argv[1:]
    is_hosts = "--hosts" in args
    is_both = "--both" in args

    agent_names, host_names = resolve_names_from_discovery_stream()

    if is_both:
        for name in agent_names:
            sys.stdout.write(name + "\n")
        for name in host_names:
            sys.stdout.write(name + "\n")
    elif is_hosts:
        for name in host_names:
            sys.stdout.write(name + "\n")
    else:
        for name in agent_names:
            sys.stdout.write(name + "\n")


if __name__ == "__main__":
    main()
