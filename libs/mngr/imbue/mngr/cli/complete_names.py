"""Resolve current agent and host names from the discovery event stream.

This is a standalone script that uses ONLY stdlib -- no mngr imports, no
third-party libraries. This is intentional: it runs on every TAB press and
must be as fast as possible.

It reads the discovery event stream JSONL file, finds the latest snapshot of
each provider (plus any legacy global snapshot), then replays incremental
events to determine which agents and hosts are currently active.

Usage:
    python -m imbue.mngr.cli.complete_names
    python -m imbue.mngr.cli.complete_names --hosts
    python -m imbue.mngr.cli.complete_names --both
"""

import json
import os
import sys
from pathlib import Path
from typing import Any


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


def _find_replay_start_idx(lines: list[str]) -> int:
    """Find the earliest line index whose replay still includes every provider's latest snapshot.

    Reverse-scans (for efficiency) for each provider's latest ``DISCOVERY_PROVIDER``
    line and returns the minimum of those indices. Returns 0 if no snapshot is found
    (the whole file is replayed). Replaying a little extra is harmless: per-provider
    snapshots reset only their own provider.

    Stops as soon as a legacy ``DISCOVERY_FULL`` is reached: that snapshot resets all
    state on replay, so any line below it is irrelevant and every per-provider snapshot
    already seen above it has a larger index, making the full snapshot's index the
    minimum. This keeps the legacy/mixed-log case cheap on this every-TAB hot path; a
    purely per-provider log (no full snapshot) is still scanned to the start to locate
    every provider's latest snapshot.
    """
    latest_idx_by_provider: dict[str, int] = {}
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        # Quick string check before parsing JSON.
        if '"DISCOVERY_FULL"' not in line and '"DISCOVERY_PROVIDER"' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = data.get("type")
        if event_type == "DISCOVERY_FULL":
            return idx
        elif event_type == "DISCOVERY_PROVIDER":
            provider_name = data.get("provider_name", "")
            # First time we see a provider in reverse order is its latest snapshot.
            if provider_name not in latest_idx_by_provider:
                latest_idx_by_provider[provider_name] = idx
        else:
            pass

    if not latest_idx_by_provider:
        return 0
    return min(latest_idx_by_provider.values())


def _drop_provider_state(
    provider_name: str,
    agent_name_by_id: dict[str, str],
    host_name_by_id: dict[str, str],
    provider_by_agent_id: dict[str, str],
    provider_by_host_id: dict[str, str],
) -> None:
    """Forget every agent/host attributed to one provider (its per-provider snapshot supersedes them)."""
    for agent_id in [aid for aid, prov in provider_by_agent_id.items() if prov == provider_name]:
        agent_name_by_id.pop(agent_id, None)
        provider_by_agent_id.pop(agent_id, None)
    for host_id in [hid for hid, prov in provider_by_host_id.items() if prov == provider_name]:
        host_name_by_id.pop(host_id, None)
        provider_by_host_id.pop(host_id, None)


def resolve_names_from_discovery_stream(
    events_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Read the discovery event stream and return current (agent_names, host_names).

    Replays from each provider's latest snapshot (and any legacy global snapshot),
    then folds all subsequent events to determine which agents and hosts are active.
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

    start_idx = _find_replay_start_idx(all_lines)
    lines_to_replay = all_lines[start_idx:]

    # Map from agent_id -> agent_name for currently active agents.
    agent_name_by_id: dict[str, str] = {}
    # Map from host_id -> host_name for currently active hosts.
    host_name_by_id: dict[str, str] = {}
    # Provider attribution, so a per-provider snapshot can reset only its own items.
    provider_by_agent_id: dict[str, str] = {}
    provider_by_host_id: dict[str, str] = {}

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
            # Legacy global snapshot: reset all state, then repopulate.
            agent_name_by_id.clear()
            host_name_by_id.clear()
            provider_by_agent_id.clear()
            provider_by_host_id.clear()
            for agent in data.get("agents", ()):
                _record_agent(agent, agent_name_by_id, provider_by_agent_id)
            for host in data.get("hosts", ()):
                _record_host(host, host_name_by_id, provider_by_host_id)

        elif event_type == "DISCOVERY_PROVIDER":
            # Per-provider snapshot: reset only this provider's items, then repopulate.
            provider_name = data.get("provider_name", "")
            _drop_provider_state(
                provider_name, agent_name_by_id, host_name_by_id, provider_by_agent_id, provider_by_host_id
            )
            for agent in data.get("agents", ()):
                _record_agent(agent, agent_name_by_id, provider_by_agent_id)
            for host in data.get("hosts", ()):
                _record_host(host, host_name_by_id, provider_by_host_id)

        elif event_type == "AGENT_DISCOVERED":
            _record_agent(data.get("agent", {}), agent_name_by_id, provider_by_agent_id)

        elif event_type == "HOST_DISCOVERED":
            _record_host(data.get("host", {}), host_name_by_id, provider_by_host_id)

        elif event_type == "AGENT_DESTROYED":
            agent_id = data.get("agent_id", "")
            if agent_id:
                agent_name_by_id.pop(agent_id, None)
                provider_by_agent_id.pop(agent_id, None)

        elif event_type == "HOST_DESTROYED":
            host_id = data.get("host_id", "")
            if host_id:
                host_name_by_id.pop(host_id, None)
                provider_by_host_id.pop(host_id, None)
                # Remove all agents belonging to this host.
                for agent_id in data.get("agent_ids", []):
                    agent_name_by_id.pop(agent_id, None)
                    provider_by_agent_id.pop(agent_id, None)

        else:
            pass

    agent_names = sorted(set(agent_name_by_id.values()))
    host_names = sorted(set(host_name_by_id.values()))
    return agent_names, host_names


def _record_agent(
    agent: dict[str, Any],
    agent_name_by_id: dict[str, str],
    provider_by_agent_id: dict[str, str],
) -> None:
    """Record one discovered agent dict into the name/provider maps (no-op if it lacks id/name)."""
    agent_id = agent.get("agent_id", "")
    agent_name = agent.get("agent_name", "")
    if agent_id and agent_name:
        agent_name_by_id[agent_id] = agent_name
        provider_by_agent_id[agent_id] = agent.get("provider_name", "")


def _record_host(
    host: dict[str, Any],
    host_name_by_id: dict[str, str],
    provider_by_host_id: dict[str, str],
) -> None:
    """Record one discovered host dict into the name/provider maps (no-op if it lacks id/name)."""
    host_id = host.get("host_id", "")
    host_name = host.get("host_name", "")
    if host_id and host_name:
        host_name_by_id[host_id] = host_name
        provider_by_host_id[host_id] = host.get("provider_name", "")


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
