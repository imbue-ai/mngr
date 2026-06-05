"""Shared non-fixture test helpers for desktop_client tests."""

import os
import subprocess
from pathlib import Path

from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.primitives import CreationId


def seed_agent_creator_creation(
    agent_creator: AgentCreator,
    creation_id: CreationId,
    status: AgentCreationStatus,
    host_name: str,
) -> None:
    """Register an in-flight creation directly in ``agent_creator`` for a test.

    ``AgentCreator`` has no public method to put a creation into a given
    status without spawning a real background ``mngr create`` thread, so
    tests that need a creation already in some state seed it here. This
    helper is the single place that reaches into the creator's private
    per-creation maps, so the private storage layout is encapsulated in
    one location rather than poked inline by each test.
    """
    with agent_creator._lock:
        agent_creator._statuses[str(creation_id)] = status
        agent_creator._host_names[str(creation_id)] = host_name


def restic_backup_a_file(repository: str, password: str, source: Path) -> None:
    """Create one snapshot in ``repository`` from ``source`` using plain restic."""
    env = dict(os.environ)
    env.update({"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": password})
    result = subprocess.run(
        ["restic", "backup", str(source)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120.0,
    )
    assert result.returncode == 0, result.stderr
