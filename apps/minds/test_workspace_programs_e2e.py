"""Workspace programs run inside a real workspace container built from the default workspace template.

``minds_snapshot_resume`` tests: they run in the sandbox booted from the minds
snapshot (see ``test_snapshot_resume.py``), against its baked workspace, with
the archive and command the app ships each program with (``docker exec``
stands in for ``mngr exec``). They prove what the stubbed unit tests in
``desktop_client/backup_workspace_programs_test.py`` and
``desktop_client/update_*_test.py`` cannot: that a program runs under the
workspace's own ``python3`` with only its archive's modules, and reads and
writes the workspace's real tools and files correctly.
"""

import hashlib
import json
import shlex

import pytest

from imbue.minds.desktop_client.testing import armed_update_schedule_file
from imbue.minds.desktop_client.update_workspace_command import UpdateWorkspaceSettings
from imbue.minds.desktop_client.update_workspace_command import UtcUpdateWindow
from imbue.minds.desktop_client.update_workspace_command import build_read_arguments
from imbue.minds.desktop_client.update_workspace_command import build_update_workspace_command
from imbue.minds.desktop_client.update_workspace_command import parse_update_workspace_facts
from imbue.minds.desktop_client.workspace_program_command import build_workspace_program_archive
from imbue.minds.desktop_client.workspace_program_command import extract_marker_json
from imbue.minds.testing import RunningWorkspaceContainer
from imbue.minds.testing import WORKSPACE_CONTAINER_ROOT
from imbue.minds.testing import exec_in_workspace_container
from imbue.minds.testing import run_workspace_program_in_container
from imbue.minds.workspace_programs import backup_gate_probe
from imbue.minds.workspace_programs import update_workspace
from imbue.minds.workspace_programs.backup_protocol import GATE_RESULT_MARKER
from imbue.minds.workspace_programs.update_dispatcher import DISPATCHER_ARCHIVE_PATH
from imbue.minds.workspace_programs.update_dispatcher import DISPATCHER_LOG_PATH
from imbue.minds.workspace_programs.update_protocol import DISPATCHER_CRON_NAME
from imbue.minds.workspace_programs.update_protocol import LIVE_CRON_DIRECTORY
from imbue.minds.workspace_programs.update_protocol import SCHEDULE_FILE_PATH
from imbue.minds.workspace_programs.update_protocol import UPDATE_COMMAND_RESULT_MARKER

_LIVE_CRON_PATH = f"{LIVE_CRON_DIRECTORY}/{DISPATCHER_CRON_NAME}"
_DURABLE_CRON_PATH = f"data/.state/cron.d/{DISPATCHER_CRON_NAME}"


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


def _run_in_workspace_root(workspace: RunningWorkspaceContainer, command: str) -> str:
    completed = exec_in_workspace_container(
        workspace.container_name, f"cd {WORKSPACE_CONTAINER_ROOT} && {command}", timeout=120
    )
    assert completed.returncode == 0, (command, completed.stdout, completed.stderr)
    return completed.stdout


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(600)
def test_update_program_installs_its_own_archive_as_the_dispatcher_cron_runs(
    running_workspace: RunningWorkspaceContainer,
) -> None:
    """A refreshing ``read`` copies the program's archive in and writes the cron line, and the tick runs from it.

    The schedule is written as an arm leaves it rather than armed, since an arm also needs an AI
    account signed in to the workspace, which the snapshot's need not have; its window is empty, so
    the tick dispatches nothing.
    """
    armed_schedule = armed_update_schedule_file(
        ceiling_ref="minds-v0.0.1", window_start_minute_utc=0, window_length_minutes=0
    )
    settings = UpdateWorkspaceSettings(
        ceiling_ref="minds-v0.0.2",
        window=UtcUpdateWindow(start_minute_utc=60, length_minutes=0),
        is_backup_configured=True,
    )
    _run_in_workspace_root(
        running_workspace,
        f"mkdir -p {shlex.quote(SCHEDULE_FILE_PATH.rsplit('/', 1)[0])} && "
        f"printf %s {shlex.quote(json.dumps(armed_schedule))} > {shlex.quote(SCHEDULE_FILE_PATH)}",
    )
    try:
        read = exec_in_workspace_container(
            running_workspace.container_name,
            f"cd {WORKSPACE_CONTAINER_ROOT} && "
            + build_update_workspace_command("read", build_read_arguments(settings, is_sign_in_checked=False)),
            timeout=300,
        )

        assert read.returncode == 0, (read.stdout, read.stderr)
        payload = extract_marker_json(read.stdout, UPDATE_COMMAND_RESULT_MARKER)
        assert payload is not None, (read.stdout, read.stderr)
        facts = parse_update_workspace_facts(payload)
        assert facts.schedule is not None
        assert (facts.schedule.ceiling_ref, facts.schedule.window_start_minute_utc) == ("minds-v0.0.2", 60)
        installed_digest = _run_in_workspace_root(running_workspace, f"sha256sum {DISPATCHER_ARCHIVE_PATH}").split()[0]
        assert installed_digest == hashlib.sha256(build_workspace_program_archive(update_workspace)).hexdigest()
        tick_command = f"&& python3 {DISPATCHER_ARCHIVE_PATH} tick >> {DISPATCHER_LOG_PATH} 2>&1"
        assert tick_command in _run_in_workspace_root(running_workspace, f"cat {_LIVE_CRON_PATH}")
        assert tick_command in _run_in_workspace_root(running_workspace, f"cat {_DURABLE_CRON_PATH}")

        tick = exec_in_workspace_container(
            running_workspace.container_name,
            f"cd {WORKSPACE_CONTAINER_ROOT} && python3 {DISPATCHER_ARCHIVE_PATH} tick",
            timeout=300,
        )

        assert tick.returncode == 0, (tick.stdout, tick.stderr)
        assert json.loads(_run_in_workspace_root(running_workspace, f"cat {SCHEDULE_FILE_PATH}"))["ceiling"] == (
            "minds-v0.0.2"
        )
    finally:
        _run_in_workspace_root(
            running_workspace,
            f"rm -f {SCHEDULE_FILE_PATH} {DISPATCHER_ARCHIVE_PATH} {DISPATCHER_LOG_PATH} {_LIVE_CRON_PATH} "
            f"{_DURABLE_CRON_PATH}",
        )
