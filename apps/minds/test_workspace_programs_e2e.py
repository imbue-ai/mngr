"""Workspace programs run inside a real workspace container built from the default workspace template.

``minds_snapshot_resume`` tests: they run in the sandbox booted from the minds
snapshot (see ``test_snapshot_resume.py``), against its baked workspace, with
the archive and command the app ships each program with (``docker exec``
stands in for ``mngr exec``). They prove what the stubbed unit tests in
``desktop_client/backup_workspace_programs_test.py`` cannot: that a program
runs under the workspace's own ``python3`` with only its archive's modules,
and reads the workspace's real tools correctly.
"""

import pytest

from imbue.minds.desktop_client.workspace_program_command import extract_marker_json
from imbue.minds.testing import RunningWorkspaceContainer
from imbue.minds.testing import run_workspace_program_in_container
from imbue.minds.workspace_programs import backup_gate_probe
from imbue.minds.workspace_programs.backup_protocol import GATE_RESULT_MARKER


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(600)
def test_gate_probe_reads_the_real_workspace_chats_and_backup_ticks(
    running_workspace: RunningWorkspaceContainer,
) -> None:
    probe = run_workspace_program_in_container(
        running_workspace, backup_gate_probe, ("--agent-id", running_workspace.services_agent_id), timeout=300
    )

    assert probe.returncode == 0, (probe.stdout, probe.stderr)
    payload = extract_marker_json(probe.stdout, GATE_RESULT_MARKER)
    assert payload is not None, (probe.stdout, probe.stderr)
    # No gate_error: the program ran the workspace's own `uv run mngr list` and
    # found its agents in the real output.
    assert "gate_error" not in payload, payload
    assert isinstance(payload["running_chats"], list), payload
    assert isinstance(payload["backup_tick_in_flight"], bool), payload
