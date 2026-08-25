"""Sanity tests for sandboxes booted from a minds-workspace snapshot.

Run via::

    just test-offload-minds-snapshot <snapshot-image-id>

where ``<snapshot-image-id>`` is the Modal image id printed by
``scripts/snapshot_minds_e2e_state.py``. That script captures a Modal
sandbox in which the DEFAULT_WORKSPACE_TEMPLATE workspace's ``system_interface`` UI has
rendered, then ``docker stop``s the workspace containers so the
filesystem snapshot represents a deterministic stopped state.

Every test here carries ``@pytest.mark.minds_snapshot_resume`` and
asserts something about that pre-baked state. The mark is excluded
from every other offload config (see ``offload-modal*.toml``) so a
``minds_snapshot_resume`` test only ever runs against the right kind
of sandbox.
"""

import bz2
import configparser
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import zipfile
from collections.abc import Iterable
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import httpx
import pytest
import tomlkit
from loguru import logger
from playwright.sync_api import Frame
from playwright.sync_api import Page

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_provisioning import configure_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import disable_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import reinject_canonical_env
from imbue.minds.desktop_client.backup_update import run_backup_restore_sequence
from imbue.minds.desktop_client.backup_verification import MINIMUM_BACKUP_SERVICE_TAG
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_APPLY_UPDATE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_CHECK_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_GATE_PROBE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import CHECK_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import GATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import OFFICIAL_REMOTE_URL
from imbue.minds.desktop_client.backup_workspace_scripts import UPDATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import build_workspace_script_command
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.desktop_client.e2e_workspace_runner import _DEFAULT_MINDS_ROOT_NAME
from imbue.minds.desktop_client.e2e_workspace_runner import _REPO_ROOT
from imbue.minds.desktop_client.e2e_workspace_runner import _send_message_and_await_reply
from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import create_workspace_via_electron
from imbue.minds.desktop_client.e2e_workspace_runner import destroy_agent_best_effort
from imbue.minds.desktop_client.e2e_workspace_runner import ensure_minds_env_defaults
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_default_workspace_template_path
from imbue.minds.desktop_client.restic_cli import ResticNotInstalledError
from imbue.minds.desktop_client.workspace_diagnostics import CONSOLE_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import TRANSCRIPT_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_COLLECTOR_PATH
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_LOGS_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WORKSPACE_ZIP_ATTACHMENT_KEY
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsOmissionReason
from imbue.minds.desktop_client.workspace_diagnostics import WorkspaceDiagnosticsResult
from imbue.minds.desktop_client.workspace_diagnostics import build_staged_diagnostics_filename
from imbue.minds.desktop_client.workspace_diagnostics import collect_workspace_diagnostics
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.primitives import BackupProvider
from imbue.mngr.config.pre_readers import find_profile_dir_lightweight
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.testing import get_short_random_string

# The docker provider keeps a singleton ``*docker-state*`` sidecar container per
# workspace; the workspace agent container is the one that is NOT the
# docker-state sidecar.
_DOCKER_STATE_MARKER: Final[str] = "docker-state"


def _workspace_container_prefix() -> str:
    """The container-name prefix of the minds env under test.

    mngr names docker hosts ``{mngr_prefix}-{host_name}`` and the sandbox's
    conftest pins ``MINDS_ROOT_NAME`` to the snapshot's root, so in CI this is
    the ``minds-ci-snapshot-`` prefix every baked container carries. Scoping by
    the env (rather than a bare ``minds-``) is what makes these tests runnable
    on a dev machine, where containers from many envs coexist: only the named
    env's containers are started or selected, never a colleague env's.
    """
    return f"{os.environ.get('MINDS_ROOT_NAME', _DEFAULT_MINDS_ROOT_NAME)}-"


# system_interface's in-container port. It is a core bootstrap-managed
# app with a fixed port (registered in the data/.state app registry);
# kept as a constant so a drift shows up as a clear assertion failure.
_SYSTEM_INTERFACE_PORT: Final[int] = 8000
_MNGR_START_TIMEOUT_SECONDS: Final[int] = 300
_SYSTEM_INTERFACE_READY_TIMEOUT_SECONDS: Final[int] = 120
_SERVICES_REGISTERED_TIMEOUT_SECONDS: Final[int] = 120

# The always-on core services that must re-register in the data/.state app registry
# after a resume. ``browser`` also registers but is required separately, with a
# memory-pressure shed exception.
_CORE_REGISTERED_SERVICES: Final[tuple[str, ...]] = ("system_interface", "terminal")
_BROWSER_SERVICE_NAME: Final[str] = "browser"

# earlyoom's shed ledger inside the container (written by its ``-N`` hook; path
# pinned by ``OOM_PRIORITY_RUNTIME_DIR`` in the template's ``.mngr/settings.toml``).
# Only human-facing corroboration in the shed evidence dump, not the decision.
_SHED_LEDGER_PATH: Final[str] = "/home/user/workspace/data/.state/oom_priority/events/shed.jsonl"
# supervisord's own log; source of the browser-shed signal.
_SUPERVISORD_LOG_PATH: Final[str] = "/var/log/supervisor/supervisord.log"

# mngr lifecycle states that mean the agent's tmux window is alive (as opposed
# to STOPPED / DONE). The system-services agent is a plain ``command``-type
# agent whose window-0 command is ``sleep infinity`` (see the minds README),
# which mngr reports as RUNNING. REPLACED covers workspaces from older template
# revisions, whose claude-typed services agent held its window with a non-claude
# process. All of these indicate the agent is up.
_ALIVE_AGENT_STATES: Final[frozenset[str]] = frozenset(
    {"RUNNING", "WAITING", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE"}
)

# HTTP status codes that mean system_interface is serving (as opposed to a
# connection refusal, which curl reports as ``000``): a 2xx, a redirect, or a
# 401 auth challenge. The shell poll loops in ``_wait_for_system_interface_up``
# mirror this set in a ``case`` statement (they run inside ``docker exec`` and
# cannot reference this constant).
_SERVED_HTTP_STATUS_CODES: Final[frozenset[str]] = frozenset({"200", "301", "302", "307", "401"})


class _ResumedWorkspace(FrozenModel):
    """The workspace container + its system-services agent id, post-resume."""

    container_name: str
    services_agent_id: str


def _run_docker(args: list[str], *, timeout: int = 30) -> str:
    """Run a ``docker`` command on the sandbox host and return stdout."""
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=timeout).stdout


def _exec_in_container(container_name: str, command: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside ``container_name`` via ``docker exec``."""
    return subprocess.run(
        ["docker", "exec", container_name, "bash", "-lc", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _all_minds_container_ids() -> list[str]:
    return _run_docker(["ps", "-aq", "--filter", f"name={_workspace_container_prefix()}"]).split()


def _running_workspace_container_name() -> str:
    """Return the running workspace agent container (not the docker-state sidecar)."""
    prefix = _workspace_container_prefix()
    names = _run_docker(["ps", "--format", "{{.Names}}"]).splitlines()
    workspace_names = [name for name in names if name.startswith(prefix) and _DOCKER_STATE_MARKER not in name]
    assert workspace_names, f"No running {prefix}* workspace container; running containers: {names!r}"
    return workspace_names[0]


def _start_all_minds_containers() -> None:
    container_ids = _all_minds_container_ids()
    assert container_ids, "No minds containers captured in the snapshot to start."
    # Start them in one call; docker start is idempotent for already-running ones.
    subprocess.run(["docker", "start", *container_ids], check=True, capture_output=True, text=True, timeout=120)


def _list_agents_in_container(container_name: str) -> list[dict[str, Any]]:
    """Return the agents mngr sees from inside the workspace container.

    Run from inside the host (the container), where mngr uses the local
    provider and the baked-in ``MNGR_HOST_DIR=/home/user/.mngr`` -- no desktop-side
    provider fan-out, so an unrelated (uncredentialed) provider can't blank
    the listing. ``--on-error continue`` keeps any single provider failure
    from aborting the list.
    """
    result = _exec_in_container(
        container_name, "cd /home/user/workspace && mngr list --format json --on-error continue", timeout=60
    )
    assert result.returncode == 0, f"`mngr list` failed inside {container_name}: {result.stderr}"
    return json.loads(result.stdout)["agents"]


def _system_services_agent_id(container_name: str) -> str:
    """Return the id of the primary system-services agent (runs the bootstrap)."""
    agents = _list_agents_in_container(container_name)
    for agent in agents:
        if agent.get("labels", {}).get("is_primary") == "true" or agent.get("name") == "system-services":
            return agent["id"]
    raise AssertionError(f"No primary system-services agent among {[a.get('name') for a in agents]!r}")


def _wait_for_system_interface_up(container_name: str) -> bool:
    """Poll system_interface from inside the container until it answers, or time out.

    The poll loop (and its sleeps) run in shell inside ``docker exec`` rather
    than Python so the test never calls ``time.sleep``. Any 2xx/3xx/401 means
    the interface is serving (it may redirect to auth); a connection refused
    surfaces as ``000``.
    """
    poll = (
        "for i in $(seq 1 40); do "
        f"code=$(curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{_SYSTEM_INTERFACE_PORT}/ 2>/dev/null); "
        'case "$code" in 200|301|302|307|401) exit 0;; esac; '
        "sleep 3; done; exit 1"
    )
    return (
        _exec_in_container(container_name, poll, timeout=_SYSTEM_INTERFACE_READY_TIMEOUT_SECONDS + 30).returncode == 0
    )


def _wait_for_services_registered(container_name: str, service_names: tuple[str, ...]) -> str:
    """Poll the data/.state app registry inside the container until every expected service appears.

    After resume, services re-register into the app registry asynchronously, so a
    single read can race a service that registers a moment later. Poll (in shell inside
    ``docker exec``, so the test never calls ``time.sleep``) until all expected
    names are present or the deadline passes, then return the final file
    contents so the caller can assert with a useful message either way.
    """
    presence_checks = " && ".join(f'grep -q {name} "$f"' for name in service_names)
    poll = (
        "f=/home/user/workspace/data/.state/apps.toml; "
        "for i in $(seq 1 40); do "
        f'if [ -f "$f" ] && {presence_checks}; then break; fi; '
        "sleep 3; done; "
        'cat "$f" 2>/dev/null'
    )
    return _exec_in_container(container_name, poll, timeout=_SERVICES_REGISTERED_TIMEOUT_SECONDS + 30).stdout


def _gather_browser_shed_diagnostics(container_name: str) -> tuple[bool, str]:
    """Return ``(was_shed, diagnostics)`` for an absent ``browser`` registration.

    The ``browser`` supervisord program registers into the app registry *before*
    it launches the memory-heavy browser-service/Chromium, and nothing ever
    removes an entry once written, so an absent ``browser`` means its program
    never finished that registration.
    Two states produce that absence:

    - a memory-pressure shed (tolerated) -- earlyoom (or the kernel) killed the
      program with a *signal* while it was starting; supervisord records this as
      ``terminated by SIGKILL``/``SIGTERM`` or, when the killed child is
      ``forward_port.py`` and its bash wrapper propagates the status, ``exit
      status 137``/``143``. Nothing in the pre-launch registration step signals
      itself, so a signal kill there is an external OOM kill -- the browser is
      the single most OOM-expendable service (oom_score_adj=1000).
    - a genuine regression (a real failure) -- the program exits with an
      ordinary code or never spawns at all, so no signal kill is recorded.

    ``was_shed`` is True iff supervisord recorded the browser program dying on an
    OOM signal under the current (post-resume) supervisord instance:
    supervisord.log is append-only and survives the snapshot, so the ``awk``
    pass resets its match on every ``supervisord started with pid`` marker,
    ignoring kills logged by the snapshot's own build or shutdown. A missing
    marker yields "not shed", making an absent browser a hard failure. The
    ``diagnostics`` text is always returned so the caller can surface it either
    way.
    """
    diagnostic = (
        f"log={_SUPERVISORD_LOG_PATH}; "
        "awk '/supervisord started with pid/{seen=1;killed=0} "
        "seen&&/exited: browser/&&/terminated by SIGKILL|terminated by SIGTERM|exit status 137|exit status 143/{killed=1} "
        'END{print (killed?"BROWSER_SIGNAL_KILLED=yes":"BROWSER_SIGNAL_KILLED=no")}\' "$log" 2>/dev/null; '
        "echo '=== supervisorctl status browser ==='; supervisorctl status browser 2>&1 | head -5; "
        "echo '=== browser lines in supervisord.log (tail) ==='; grep -aE browser \"$log\" 2>/dev/null | tail -30; "
        "echo '=== earlyoom shed ledger ==='; "
        f"tail -50 {_SHED_LEDGER_PATH} 2>/dev/null; "
        "echo '=== earlyoom service log tail ==='; "
        "tail -20 /var/log/supervisor/earlyoom-stderr.log 2>/dev/null"
    )
    diagnostics = _exec_in_container(container_name, diagnostic, timeout=30).stdout
    return "BROWSER_SIGNAL_KILLED=yes" in diagnostics, diagnostics


def _wait_for_system_interface_down(container_name: str) -> bool:
    """Poll until system_interface stops answering (connection refused), or time out.

    Shell-side poll loop (no Python ``time.sleep``). ``000`` is curl's code
    for a failed connection, i.e. the listener is gone.
    """
    poll = (
        "for i in $(seq 1 20); do "
        f"code=$(curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{_SYSTEM_INTERFACE_PORT}/ 2>/dev/null); "
        '[ "$code" = "000" ] && exit 0; '
        "sleep 3; done; exit 1"
    )
    return _exec_in_container(container_name, poll, timeout=90).returncode == 0


@pytest.fixture(scope="session")
def running_workspace() -> Iterator[_ResumedWorkspace]:
    """Resume the snapshot's workspace and yield it once system_interface serves.

    The captured container is stopped, so a sandbox booted from the snapshot
    must (1) ``docker start`` it and (2) restart the system-services agent so
    the bootstrap respawns system_interface. This is the mngr-level building
    block behind minds' own recovery flow.
    """
    _start_all_minds_containers()
    container_name = _running_workspace_container_name()
    services_agent_id = _system_services_agent_id(container_name)
    start_result = _exec_in_container(
        container_name,
        f"cd /home/user/workspace && mngr start {services_agent_id} --quiet",
        timeout=_MNGR_START_TIMEOUT_SECONDS,
    )
    assert start_result.returncode == 0, f"`mngr start` failed for system-services: {start_result.stderr}"
    assert _wait_for_system_interface_up(container_name), (
        "system_interface never answered after resuming the system-services agent."
    )
    yield _ResumedWorkspace(container_name=container_name, services_agent_id=services_agent_id)


@pytest.fixture(scope="session", autouse=True)
def _ensure_dockerd_after_snapshot_resume(snapshot_sandbox_dockerd: None) -> None:
    """Every test in this module needs the snapshot sandbox's dockerd back up.

    The actual bring-up lives in the shared ``snapshot_sandbox_dockerd``
    session fixture in ``conftest.py`` (also used by ``test_sync_e2e.py``);
    this module-autouse wrapper preserves the original apply-to-all behavior
    for the resume sanity tests.
    """


# @pytest.mark.docker tells the host-side pytest resource guard that this
# test invokes the `docker` CLI -- so the guard's PATH wrapper expects
# (and tolerates) the call. The guard isn't actually installed inside
# the snapshot-resumed offload sandbox where this test runs, but the
# mark also satisfies the `test_prevent_hardcoded_guarded_binary`
# ratchet, which inspects this test file's source from the local host
# regardless of where the test ultimately executes.
@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(60)
def test_workspace_docker_container_is_present_and_stopped() -> None:
    """The snapshot captured a stopped DEFAULT_WORKSPACE_TEMPLATE workspace Docker container.

    Asserts:
    - dockerd sees at least one container (``docker ps -a`` non-empty)
    - at least one of those is a minds workspace container (name prefix
      ``minds-`` -- mngr_modal names workspace containers
      ``{mngr_prefix}-{host_name}`` and minds defaults to the
      ``minds-ci-snapshot`` prefix at snapshot time)
    - every minds workspace container is in the ``exited`` state (the
      snapshot script's clean-shutdown step ``docker stop``ped them
      before ``snapshot_filesystem``)
    """
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    rows = [line.split("\t", maxsplit=1) for line in result.stdout.strip().splitlines() if line]
    assert rows, (
        "`docker ps -a` returned no containers; /var/lib/docker did not survive the snapshot "
        "or dockerd is reading from the wrong root."
    )

    workspace_rows = [(name, state) for name, state in rows if name.startswith("minds-")]
    assert workspace_rows, (
        "No `minds-*` workspace containers in the snapshot. All containers seen: "
        f"{rows!r}. The snapshot script's docker-stop pass must have run against "
        "the wrong container set, or the snapshot was taken before mngr_modal "
        "created the workspace container."
    )

    not_stopped = [(name, state) for name, state in workspace_rows if state != "exited"]
    assert not not_stopped, (
        "Expected every minds workspace container to be in the `exited` state after "
        f"snapshot resume; got states: {dict(workspace_rows)!r}. The snapshot script "
        "should have `docker stop`ped them before calling snapshot_filesystem."
    )


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(300)
def test_resumed_workspace_serves_system_interface(running_workspace: _ResumedWorkspace) -> None:
    """After resume, the workspace's system_interface answers HTTP.

    A fresh probe (independent of the fixture's readiness wait) must get a
    served response (2xx, a redirect, or a 401 auth challenge -- anything but
    a connection refusal) from system_interface inside the container.
    """
    result = _exec_in_container(
        running_workspace.container_name,
        f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{_SYSTEM_INTERFACE_PORT}/",
        timeout=30,
    )
    assert result.stdout.strip() in _SERVED_HTTP_STATUS_CODES, (
        f"system_interface returned {result.stdout.strip()!r} after resume (expected a served response)."
    )


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(300)
def test_resumed_workspace_system_services_agent_is_alive(running_workspace: _ResumedWorkspace) -> None:
    """After resume, the primary system-services agent's tmux window is alive.

    The services agent is a plain ``command``-type agent running
    ``sleep infinity``, which mngr reports as ``RUNNING`` (workspaces from
    older template revisions read as ``REPLACED`` instead). Both are "alive";
    only STOPPED/DONE would mean the resume failed to bring the agent back.
    """
    agents = _list_agents_in_container(running_workspace.container_name)
    services_agents = [agent for agent in agents if agent["id"] == running_workspace.services_agent_id]
    assert services_agents, (
        f"system-services agent {running_workspace.services_agent_id} vanished from `mngr list` after resume."
    )
    state = services_agents[0]["state"]
    assert state in _ALIVE_AGENT_STATES, (
        f"Expected the system-services agent alive after resume (one of {sorted(_ALIVE_AGENT_STATES)}); got {state!r}."
    )


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(300)
def test_resumed_workspace_registered_expected_apps(running_workspace: _ResumedWorkspace) -> None:
    """After resume, the bootstrap re-registered the expected apps in the app registry.

    After resume, services re-register into the app registry asynchronously, so we
    poll until the expected names appear rather than reading once -- a single read
    races a service that registers a moment later.

    ``system_interface`` and ``terminal`` are always-on core services and must
    be present. ``web`` was intentionally dropped: default-workspace-template
    removed the blank example web service (its ``[program:web]`` supervisord
    entry and the ``libs/web_server`` scaffold), so it no longer registers.

    ``browser`` also autostarts and registers before it launches the memory-heavy
    browser-service, so it is expected too -- but it self-tags as the single most
    OOM-expendable process (oom_score_adj=1000), so under memory pressure earlyoom
    can shed it. We therefore require ``browser`` UNLESS there is positive evidence
    it was shed (supervisord recorded its program dying on an OOM signal). A bare
    "never re-registered", with no shed signal, is a real regression and fails.
    """
    expected_services = (*_CORE_REGISTERED_SERVICES, _BROWSER_SERVICE_NAME)
    app_registry = _wait_for_services_registered(running_workspace.container_name, expected_services)

    for service_name in _CORE_REGISTERED_SERVICES:
        assert service_name in app_registry, (
            f"Core service {service_name!r} not registered in the app registry after resume:\n{app_registry}"
        )

    if _BROWSER_SERVICE_NAME in app_registry:
        return
    was_shed, diagnostics = _gather_browser_shed_diagnostics(running_workspace.container_name)
    assert was_shed, (
        "browser did not re-register in the app registry after resume, and there is no evidence it was "
        "shed under memory pressure (supervisord shows no OOM-signal kill of the browser program) -- so it "
        f"genuinely failed to re-register.\napp registry:\n{app_registry}\ndiagnostics:\n{diagnostics}"
    )
    logger.info(
        "browser did not re-register after resume but was shed under memory pressure "
        "(expected: it is the most OOM-expendable service); diagnostics:\n{}",
        diagnostics,
    )


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(360)
def test_minds_recovery_restores_dead_system_interface() -> None:
    """A dead system_interface is revived by an agent bounce.

    Drives the actual minds recovery building blocks against a deterministic
    break: stop the system-services agent (which takes system_interface down),
    confirm the interface really goes down, then bounce the system-services
    agent (``mngr stop`` + ``mngr start`` inside the container -- the
    in-container analogue of the desktop client's host restart, which cannot
    ``--stop-host`` from within the container it would stop) and confirm the
    live HTTP check sees system_interface serving again.

    Self-contained (it establishes its own broken state) so it is robust to
    running in the same sandbox as the ``running_workspace`` fixture tests.
    """
    _start_all_minds_containers()
    container_name = _running_workspace_container_name()
    services_agent_id = _system_services_agent_id(container_name)

    # Break it: stopping the system-services agent tears down the bootstrap
    # and the services it manages, including system_interface.
    stop_result = _exec_in_container(
        container_name,
        f"cd /home/user/workspace && mngr stop {services_agent_id} --quiet",
        timeout=_MNGR_START_TIMEOUT_SECONDS,
    )
    assert stop_result.returncode == 0, (
        f"Failed to stop system-services to set up the broken state: {stop_result.stderr}"
    )

    assert _wait_for_system_interface_down(container_name), (
        "system_interface stayed up after stopping the system-services agent; "
        "the agent may not own the bootstrap/system_interface process tree."
    )

    # Recover by bouncing the system-services agent (stop is idempotent here;
    # start respawns it, and the bootstrap brings system_interface back up).
    restart_result = _exec_in_container(
        container_name,
        f"cd /home/user/workspace && mngr stop {services_agent_id} --quiet; mngr start {services_agent_id} --quiet",
        timeout=_MNGR_START_TIMEOUT_SECONDS,
    )
    assert restart_result.returncode == 0, f"system-services agent restart failed: {restart_result.stderr}"

    assert _wait_for_system_interface_up(container_name), (
        "system_interface did not recover after restarting the system-services agent."
    )


# -- Electron-driven create + chat (a second workspace) -----------------------
#
# The snapshot image bakes a warm Electron/Playwright/Xvfb/Docker toolchain (the
# snapshot *build* drives that same toolchain to create the first workspace).
# This test reuses that warm toolchain to drive the real Electron app and create
# a SECOND workspace -- which boots unauthenticated (the create flow injects no
# AI credentials anymore), signs in through the workspace's own Claude sign-in
# modal with a raw API key (the modal auto-appears on the fresh workspace, the
# designed first-boot step), then sends a chat message to its
# ``system_interface`` and asserts the agent replies. It runs in the same
# offload snapshot stage (carries minds_snapshot_resume), so all the "drive
# Electron" coverage lives in one place instead of a separate cold-install CI
# job. It does NOT use the baked first workspace (it creates its own), so it is
# independent of the ``running_workspace`` fixture.


def _opt_into_pytest_config_guard(settings_path: Path) -> None:
    """Set ``is_allowed_in_pytest = true`` in a throwaway ``settings.toml``.

    mngr's config guard refuses to run under ``PYTEST_CURRENT_TEST`` unless every
    config file it loads opts in. This writes the file in place with no restore,
    so ``settings_path`` must live under a throwaway tree (``tmp_path`` or a DEFAULT_WORKSPACE_TEMPLATE
    clone) -- never a real checkout.
    """
    doc = tomlkit.parse(settings_path.read_text()) if settings_path.exists() else tomlkit.document()
    doc["is_allowed_in_pytest"] = True
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(tomlkit.dumps(doc))


def _isolated_host_config_root(scratch_dir: Path) -> Path:
    """Build a throwaway git repo holding an opted-in copy of the repo's mngr config.

    The Electron app runs from the returned directory (passed as
    ``create_workspace_via_electron``'s ``host_config_dir``), so the host-side
    ``mngr`` it spawns resolves its project config here instead of the real repo
    ``.mngr/``. We copy the repo's ``settings.toml`` verbatim and add the pytest
    opt-in, deliberately omitting any ``settings.local.toml``. ``git init`` makes
    this the worktree root mngr's project-config walk stops at.
    """
    root = scratch_dir / "mngr_host_config"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True, text=True, timeout=30)
    settings_path = root / ".mngr" / "settings.toml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text((_REPO_ROOT / ".mngr" / "settings.toml").read_text())
    _opt_into_pytest_config_guard(settings_path)
    return root


def _point_desktop_mngr_at_the_baked_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Wire this process's ``mngr`` at the snapshot's desktop-side state; return its host dir.

    Anything that shells out to ``mngr exec <agent>`` from the sandbox host needs
    the snapshot's real container prefix AND its real desktop-side host dir: the
    docker provider reaches its host records through a state container named
    after the profile's user id under MNGR_HOST_DIR, so the autouse fixture's
    throwaway host dir (fresh profile, different user id) would make the baked
    workspace invisible ("Agent not found"). The baked host dir lives under the
    *real* home -- the autouse fixture monkeypatches HOME to a temp dir, so it is
    resolved via /etc/passwd (same trick as deployment_tests/conftest.py) using
    the mngr_host_dir_for layout. Its baked profile settings get the pytest
    config-guard opt-in (throwaway sandbox state, per that helper's contract),
    and the project config is an isolated pytest-opted-in copy (the repo's own
    .mngr would fail the config guard). The cwd is left neutral and the providers
    that would need cloud credentials during discovery are silenced.
    """
    root_name = os.environ.get("MINDS_ROOT_NAME", _DEFAULT_MINDS_ROOT_NAME)
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    baked_mngr_host_dir = real_home / f".{root_name}" / "mngr"
    assert baked_mngr_host_dir.is_dir(), f"No baked desktop-side mngr host dir at {baked_mngr_host_dir}"
    baked_profile_dir = find_profile_dir_lightweight(baked_mngr_host_dir)
    assert baked_profile_dir is not None, f"No mngr profile under {baked_mngr_host_dir} in the snapshot"
    _opt_into_pytest_config_guard(baked_profile_dir / "settings.toml")
    monkeypatch.setenv("MNGR_HOST_DIR", str(baked_mngr_host_dir))
    monkeypatch.setenv("MNGR_PREFIX", mngr_prefix_for(root_name))
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(_isolated_host_config_root(tmp_path) / ".mngr"))
    monkeypatch.setenv("MNGR__PROVIDERS__MODAL__IS_ENABLED", "false")
    monkeypatch.setenv("MNGR__PROVIDERS__AWS__IS_ENABLED", "false")
    monkeypatch.chdir(tmp_path)
    return baked_mngr_host_dir


def _prepare_electron_workspace_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Set the minds env + provider overrides and materialize the throwaway DEFAULT_WORKSPACE_TEMPLATE + host config.

    Returns ``(default_workspace_template_path, host_config_root)`` for ``create_workspace_via_electron``.
    """
    configure_logging()
    # Route env-var defaults through monkeypatch so injected MINDS_ROOT_NAME /
    # MINDS_CLIENT_CONFIG_PATH revert between tests; defaults to the committed
    # ci-snapshot tier.
    ensure_minds_env_defaults(setenv=monkeypatch.setenv)
    # No Modal creds here, so silence the Electron-spawned mngr's Modal discovery.
    monkeypatch.setenv("MNGR__PROVIDERS__MODAL__IS_ENABLED", "false")
    # Pin the local-docker workspace to runc; gVisor (runsc) is absent in CI /
    # the sandbox. MINDS_DOCKER_RUNTIME_DEFAULT pins the create form / API default
    # to runc so minds never stacks the `docker_runsc` create-template -- the only
    # way runsc gets selected, now that the pinned DEFAULT_WORKSPACE_TEMPLATE `docker` template already
    # defaults to runc. (A provider-config env var like
    # MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME cannot help: an explicitly stacked
    # template's docker_runtime outranks it.)
    monkeypatch.setenv("MINDS_DOCKER_RUNTIME_DEFAULT", "RUNC")
    # The Electron-spawned mngr loads two project-config trees under
    # PYTEST_CURRENT_TEST: the host-side config (a throwaway opted-in copy built
    # here) and the DEFAULT_WORKSPACE_TEMPLATE worktree. The DEFAULT_WORKSPACE_TEMPLATE worktree is materialized ahead of time
    # by ``materialize_paired_default_workspace_template_worktree`` (baked into the snapshot image in
    # CI, or created by the local test recipe) with its pytest opt-in already
    # committed, so this only resolves it -- and errors loudly if the materialize
    # step never ran.
    default_workspace_template_path = resolve_default_workspace_template_path()
    host_config_root = _isolated_host_config_root(tmp_path)
    return default_workspace_template_path, host_config_root


def _sign_in_with_api_key_via_modal(page: Page | Frame, api_key: str) -> None:
    """Drive the workspace's Claude sign-in modal through the API-key path.

    A freshly created workspace has no AI credentials, so the modal opens on
    its own (the load-time status check) -- the designed first-boot step.
    Signing in writes the key into the shared Claude settings env block and
    restarts the workspace's claude agents, so the success state can take a
    couple of minutes to appear.
    """
    logger.info("Waiting for the Claude sign-in modal to auto-appear")
    page.wait_for_selector(".claude-login-modal", timeout=120_000)
    page.click(".claude-login-alts-toggle")
    page.click('button.claude-login-alt:has-text("Use an API key")')
    page.wait_for_selector("#claude-login-api-key-input", timeout=10_000)
    page.fill("#claude-login-api-key-input", api_key)
    logger.info("Submitting the API key through the modal")
    page.click('button:has-text("Save & finish")')
    # Applying credentials restarts the claude agents before reporting success.
    page.wait_for_selector(".claude-login-status-icon--success", timeout=300_000)
    page.click('button:has-text("Done")')
    page.wait_for_selector(".claude-login-overlay", state="detached", timeout=10_000)
    logger.info("Signed in via the modal")


def _sign_in_and_chat(page: Page | Frame, api_key: str, token: str) -> None:
    _sign_in_with_api_key_via_modal(page, api_key)
    _send_message_and_await_reply(page, token)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_create_workspace_and_sign_in_via_modal_then_chat_via_electron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    xvfb_display: str,
) -> None:
    """Create an unauthenticated Docker workspace, sign in via the modal, chat.

    The product-level first-boot round-trip: the create flow injects no AI
    credentials, so the workspace boots unauthenticated and its Claude
    sign-in modal auto-appears; the test fills the API-key path in the real
    modal UI, waits for the settings write + agent restart, then asserts the
    agent answers a chat message (echoes a unique token) -- end-to-end
    through the real Electron app and the desktop client proxy.

    Runs in the snapshot offload sandbox, reusing the warm Electron toolchain
    baked into the image (the ``xvfb_display`` fixture supplies the display the
    sandbox lacks). Needs a real Anthropic key: the agent only replies if the
    key works. The key is read from ``ANTHROPIC_API_KEY`` (forwarded into
    this stage from Vault) and typed into the modal -- the Electron child
    env scrubs that var, so the key reaches the agent only via the modal,
    exercising the real sign-in UX. Skips if the key is absent.
    """
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY is required for the modal sign-in workspace chat round-trip")

    default_workspace_template_path, host_config_root = _prepare_electron_workspace_inputs(tmp_path, monkeypatch)

    workspace_name = f"forever-{get_short_random_string()}"
    token = get_short_random_string()
    debug_port = find_free_port()
    logger.info(
        "Workspace name: {}; chat token: {}; CDP debug port: {}; DISPLAY={}",
        workspace_name,
        token,
        debug_port,
        xvfb_display,
    )

    try:
        create_workspace_via_electron(
            default_workspace_template_path,
            workspace_name,
            debug_port,
            host_config_dir=host_config_root,
            on_workspace_ready=lambda page: _sign_in_and_chat(page, anthropic_api_key, token),
        )
    finally:
        destroy_agent_best_effort(workspace_name, config_project_dir=host_config_root / ".mngr")


# -- Backup-update chat gate against a deterministically RUNNING agent --------
#
# The backup update's chat gate must (a) classify a non-main agent that is
# RUNNING as a running chat, (b) block the mutating update on it, and (c) stop
# it for real when the "Stop all chats and retry" flow passes --stop-chats. The
# unit tests in backup_workspace_scripts_test.py drive these paths with a
# stubbed `mngr list`; this test drives them against the resumed snapshot
# workspace with a controllable `command` agent held RUNNING via the `active`
# marker, so the gate has a deterministic RUNNING window to observe.


def _find_agent_by_name(container_name: str, name: str) -> dict[str, Any]:
    """Return the agent record whose name matches ``name`` from mngr list."""
    agents = _list_agents_in_container(container_name)
    matching = [agent for agent in agents if agent.get("name") == name]
    assert matching, f"No agent named {name!r} among {[agent.get('name') for agent in agents]!r}"
    return matching[0]


def _wait_for_agent_state(container_name: str, agent_id: str, expected_state: str, *, attempts: int = 40) -> bool:
    """Poll (shell-side, no python sleeps) until the agent reports the state, or time out."""
    read_state = (
        "cd /home/user/workspace && mngr list --format json --on-error continue | "
        f'python3 -c \'import json,sys; print(next((a["state"] for a in json.load(sys.stdin)["agents"] '
        f'if a["id"] == "{agent_id}"), ""))\''
    )
    poll = (
        f"for i in $(seq 1 {attempts}); do "
        f'state=$({read_state}); [ "$state" = "{expected_state}" ] && exit 0; '
        "sleep 3; done; exit 1"
    )
    return _exec_in_container(container_name, poll, timeout=attempts * 3 + 120).returncode == 0


def _run_backup_script_in_container(
    container_name: str, script: str, args: tuple[str, ...], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run one of the minds backup workspace scripts inside the container at /home/user/workspace."""
    command = build_workspace_script_command(script, args)
    return _exec_in_container(container_name, f"cd /home/user/workspace && {command}", timeout=timeout)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(900)
def test_backup_update_gate_blocks_on_live_chat_and_stop_chats_clears_it(
    running_workspace: _ResumedWorkspace,
) -> None:
    """The backup-update gate blocks while an agent is RUNNING and --stop-chats clears it.

    The gate keys on ``state == "RUNNING"`` for any non-``main`` agent, so a
    controllable ``command`` agent that blocks in ``sleep`` and is marked active
    through ``mngr exec`` (the same ``active`` marker real agent integrations
    write) stands in for a live chat. A real claude chat's RUNNING window lasts
    only as long as its current turn, so the gate probe and apply-update scripts
    could miss it between their slow ``mngr list`` round-trips; the sleep-backed
    marker holds RUNNING until the ``--stop-chats`` gate itself clears it.
    """
    container_name = running_workspace.container_name
    services_agent_id = running_workspace.services_agent_id
    blocker_name = "gate-blocker"
    try:
        # We want an agent that holds RUNNING for the whole test and leaves it
        # only when the gate stops it, so the gate catches it between its slow
        # `mngr list` round-trips. A command agent running a long `sleep` is the
        # live process; the `active` marker (which real agents get from their
        # hooks) is what mngr reads as RUNNING rather than WAITING. `--transfer
        # none` keeps it in the repo root, the only work_dir the gate looks at.
        created = _exec_in_container(
            container_name,
            f"cd /home/user/workspace && mngr create {blocker_name} --type command "
            "--transfer none --no-ensure-clean --no-connect -- sleep 100196",
            timeout=180,
        )
        assert created.returncode == 0, f"`mngr create` failed for the blocker agent: {created.stderr}"
        marked = _exec_in_container(
            container_name,
            f"cd /home/user/workspace && mngr exec {blocker_name} 'touch \"$MNGR_AGENT_STATE_DIR/active\"'",
            timeout=120,
        )
        assert marked.returncode == 0, f"marking the blocker agent active failed: {marked.stderr}"
        blocker_id = str(_find_agent_by_name(container_name, blocker_name)["id"])
        assert _wait_for_agent_state(container_name, blocker_id, "RUNNING"), (
            "The blocker agent never reached RUNNING after its active marker was written."
        )

        # The gate probe classifies the running blocker as a running chat.
        probe = _run_backup_script_in_container(
            container_name,
            BACKUP_GATE_PROBE_SCRIPT,
            ("--agent-id", services_agent_id),
            timeout=300,
        )
        probe_payload = extract_marker_json(probe.stdout, GATE_RESULT_MARKER)
        assert probe_payload is not None, (probe.stdout, probe.stderr)
        probe_chats = probe_payload["running_chats"]
        assert isinstance(probe_chats, list) and blocker_name in probe_chats, probe_payload

        # The mutating update refuses to run while the blocker is RUNNING.
        blocked = _run_backup_script_in_container(
            container_name,
            BACKUP_APPLY_UPDATE_SCRIPT,
            ("--minds-version", "0.0.0-snapshot-test", "--agent-id", services_agent_id),
            timeout=600,
        )
        blocked_payload = extract_marker_json(blocked.stdout, UPDATE_RESULT_MARKER)
        assert blocked_payload is not None, (blocked.stdout, blocked.stderr)
        assert blocked_payload["status"] == "blocked", blocked_payload
        blocked_chats = blocked_payload["running_chats"]
        assert isinstance(blocked_chats, list) and blocker_name in blocked_chats, blocked_payload

        # "Stop all chats and retry": the script stops the running agent itself
        # and proceeds past the gate. Whether the rest of the update succeeds
        # depends on the baked repo's tags; the contract under test is that the
        # outcome is anything but blocked and the agent is genuinely stopped.
        retried = _run_backup_script_in_container(
            container_name,
            BACKUP_APPLY_UPDATE_SCRIPT,
            ("--minds-version", "0.0.0-snapshot-test", "--agent-id", services_agent_id, "--stop-chats"),
            timeout=600,
        )
        retried_payload = extract_marker_json(retried.stdout, UPDATE_RESULT_MARKER)
        assert retried_payload is not None, (retried.stdout, retried.stderr)
        assert retried_payload["status"] != "blocked", retried_payload
        assert _wait_for_agent_state(container_name, blocker_id, "STOPPED"), (
            "The blocker agent was not stopped by the --stop-chats gate."
        )
    finally:
        # The workspace is a session-scoped shared container; never leak a
        # RUNNING agent that would block a sibling test's own gate.
        _exec_in_container(
            container_name, f"cd /home/user/workspace && mngr destroy {blocker_name} --force", timeout=180
        )


# -- Backup service: check / update / converge against the resumed workspace --
#
# These replace the old test_backup_service_release.py release tests (which
# ran against a fake default-workspace-template-shaped repo with a stub supervisorctl). Here
# everything is real: the baked workspace's git history (shared with the
# official template repo on GitHub, so the check's `official`-remote tag
# fetch runs for real), the actual supervisord + host-backup program inside
# the container, real `uv sync`, and real restic provisioning from the
# sandbox host into the container.


def _git_in_workspace(container_name: str, args: str) -> subprocess.CompletedProcess[str]:
    return _exec_in_container(container_name, f"cd /home/user/workspace && git {args}", timeout=60)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(900)
def test_backup_service_check_update_and_force_update_converge(running_workspace: _ResumedWorkspace) -> None:
    """Check against the shipped minimum tag, update to it, verify convergence + force-update idempotence.

    The check fetches the minimum tag from the official GitHub remote when it
    is missing locally (exactly the production path) and reads the real
    supervisord state of the real host-backup program; the update does the
    real git converge + `uv sync` + `supervisorctl restart` inside the
    container, and a second (force) update at the same version must be an ok
    no-op commit-wise -- the idempotent "reset the backup service" action.
    """
    container_name = running_workspace.container_name
    agent_id = running_workspace.services_agent_id
    minimum_tag = MINIMUM_BACKUP_SERVICE_TAG
    minimum_version = minimum_tag.removeprefix("minds-v")

    # 1. The check runs end to end: official remote ensured (and pointed at
    # the canonical URL), tags fetched from GitHub when missing, real
    # supervisord state reported.
    check = _run_backup_script_in_container(
        container_name, BACKUP_CHECK_SCRIPT, ("--minimum-tag", minimum_tag, "--agent-id", agent_id), timeout=600
    )
    check_payload = extract_marker_json(check.stdout, CHECK_RESULT_MARKER)
    assert check_payload is not None, (check.stdout, check.stderr)
    assert check_payload["target_tag"] == minimum_tag, check_payload
    assert check_payload["code_state"] in ("matches", "newer", "outdated"), check_payload
    assert check_payload["service_state"] == "running", check_payload
    remote_url = _git_in_workspace(container_name, "remote get-url official")
    assert remote_url.stdout.strip() == OFFICIAL_REMOTE_URL, remote_url

    # 2. Update (converge) to the minimum tag's content. Whether a commit
    # lands depends on how far the baked template has moved past the tag;
    # either way the script must succeed and restart the service.
    update = _run_backup_script_in_container(
        container_name,
        BACKUP_APPLY_UPDATE_SCRIPT,
        ("--minds-version", minimum_version, "--agent-id", agent_id),
        timeout=800,
    )
    update_payload = extract_marker_json(update.stdout, UPDATE_RESULT_MARKER)
    assert update_payload is not None, (update.stdout, update.stderr)
    assert update_payload["status"] == "ok", update_payload
    assert update_payload["tag"] == minimum_tag, update_payload
    if update_payload["committed"]:
        subject = _git_in_workspace(container_name, "log -1 --format=%s").stdout.strip()
        assert subject == f"backup-update: {minimum_tag}", subject

    # 3. Re-check: the code now matches the minimum tag exactly and the
    # service came back RUNNING.
    recheck = _run_backup_script_in_container(
        container_name, BACKUP_CHECK_SCRIPT, ("--minimum-tag", minimum_tag, "--agent-id", agent_id), timeout=600
    )
    recheck_payload = extract_marker_json(recheck.stdout, CHECK_RESULT_MARKER)
    assert recheck_payload is not None, (recheck.stdout, recheck.stderr)
    assert recheck_payload["code_state"] == "matches", recheck_payload
    assert recheck_payload["service_state"] == "running", recheck_payload

    # 4. Force update at the already-converged version: ok, nothing to
    # commit, service restarted/verified again.
    forced = _run_backup_script_in_container(
        container_name,
        BACKUP_APPLY_UPDATE_SCRIPT,
        ("--minds-version", minimum_version, "--agent-id", agent_id),
        timeout=800,
    )
    forced_payload = extract_marker_json(forced.stdout, UPDATE_RESULT_MARKER)
    assert forced_payload is not None, (forced.stdout, forced.stderr)
    assert forced_payload["status"] == "ok", forced_payload
    assert forced_payload["committed"] is False, forced_payload


# -- Backup enable / env repair / destination change (minds-side, real exec) --

# Pinned restic download for sandboxes whose snapshot image predates the
# bundled binary; must track scripts/download-binaries.js.
_RESTIC_DOWNLOAD_URL: Final[str] = (
    "https://github.com/restic/restic/releases/download/v0.18.1/restic_0.18.1_linux_amd64.bz2"
)
_RESTIC_DOWNLOAD_SHA256: Final[str] = "680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d"


def _ensure_restic_on_sandbox_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point MINDS_RESTIC_BINARY at a usable restic, downloading one when absent.

    The snapshot image only carries the bundled binary when its build ran the
    download step; rather than skipping (silently losing coverage), fetch the
    pinned release and verify its published checksum.
    """
    try:
        restic_cli.ensure_restic_available()
        return
    except ResticNotInstalledError:
        logger.info("No restic on the sandbox host; downloading the pinned release for this test")
    response = httpx.get(_RESTIC_DOWNLOAD_URL, follow_redirects=True, timeout=180.0)
    response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    assert digest == _RESTIC_DOWNLOAD_SHA256, f"restic download checksum mismatch: {digest}"
    binary_path = tmp_path / "restic"
    binary_path.write_bytes(bz2.decompress(response.content))
    binary_path.chmod(0o755)
    monkeypatch.setenv("MINDS_RESTIC_BINARY", str(binary_path))


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(900)
def test_backup_enable_repair_and_destination_change_on_resumed_workspace(
    running_workspace: _ResumedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enable backups, repair a corrupted env, and change the destination -- minds-side, for real.

    Drives the actual provisioning entry points from the sandbox host: real
    `restic init` against local repositories (keyed by the per-workspace
    password), and real
    `mngr exec` injection/rotation of `data/.secrets/restic.env` inside the
    resumed workspace container.
    """
    _ensure_restic_on_sandbox_host(tmp_path, monkeypatch)
    # The provisioning path shells out to `mngr exec <agent>` from this process.
    _point_desktop_mngr_at_the_baked_workspace(tmp_path, monkeypatch)

    container_name = running_workspace.container_name
    agent_id = AgentId(running_workspace.services_agent_id)

    data_dir = tmp_path / "minds-data"
    data_dir.mkdir()
    paths = InstallationPaths(data_dir=data_dir)
    repo_one = tmp_path / "restic-repo-1"
    repo_two = tmp_path / "restic-repo-2"

    def read_workspace_env() -> str:
        result = _exec_in_container(container_name, "cat /home/user/workspace/data/.secrets/restic.env", timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    # Enable backups on the (configure-later) workspace: real restic init +
    # random per-workspace password + injection into the real container.
    configure_backups_for_host(
        agent_id=agent_id,
        request=BackupSetupRequest(
            backup_provider=BackupProvider.API_KEY, api_key_env_text=f"RESTIC_REPOSITORY={repo_one}"
        ),
        imbue_cloud_cli=None,
        paths=paths,
    )
    canonical_one = read_canonical_env(paths, agent_id)
    assert canonical_one is not None
    assert f"RESTIC_REPOSITORY={repo_one}" in canonical_one
    assert "RESTIC_PASSWORD=" in canonical_one
    assert (repo_one / "config").is_file()
    assert read_workspace_env() == canonical_one

    # Repair: corrupt the workspace copy, re-inject, and confirm the drifted
    # copy was rotated aside inside the container rather than lost.
    corrupted = _exec_in_container(
        container_name,
        "printf 'RESTIC_REPOSITORY=garbage\n' > /home/user/workspace/data/.secrets/restic.env",
        timeout=30,
    )
    assert corrupted.returncode == 0, corrupted.stderr
    reinject_canonical_env(agent_id=agent_id, paths=paths)
    assert read_workspace_env() == canonical_one
    rotated = _exec_in_container(
        container_name, "grep -l garbage /home/user/workspace/data/.secrets/restic.env.*", timeout=30
    )
    assert rotated.returncode == 0 and rotated.stdout.strip(), (rotated.stdout, rotated.stderr)

    # Destination change: fresh provisioning against repo two; the old
    # canonical env is archived minds-side and the workspace copy replaced.
    change_backup_destination_for_host(
        agent_id=agent_id,
        request=BackupSetupRequest(
            backup_provider=BackupProvider.API_KEY, api_key_env_text=f"RESTIC_REPOSITORY={repo_two}"
        ),
        imbue_cloud_cli=None,
        paths=paths,
    )
    canonical_two = read_canonical_env(paths, agent_id)
    assert canonical_two is not None
    assert f"RESTIC_REPOSITORY={repo_two}" in canonical_two
    assert canonical_two != canonical_one
    assert (repo_two / "config").is_file()
    assert read_workspace_env() == canonical_two
    archived = list((data_dir / "backup_envs").glob(f"{agent_id}.env.*"))
    assert len(archived) == 1
    assert archived[0].read_text() == canonical_one
    # The old repository is untouched and still reachable via the archive.
    assert (repo_one / "config").is_file()

    # Disable: the canonical env is archived and the workspace copy rotated
    # aside, so the backup service reads "not configured" again.
    disable_backups_for_host(agent_id=agent_id, paths=paths)
    assert read_canonical_env(paths, agent_id) is None
    gone = _exec_in_container(container_name, "test -f /home/user/workspace/data/.secrets/restic.env", timeout=30)
    assert gone.returncode != 0, "the workspace restic.env should be rotated aside after disabling"
    archived_after_disable = list((data_dir / "backup_envs").glob(f"{agent_id}.env.*"))
    assert len(archived_after_disable) == 2
    # Disabling again is an idempotent no-op.
    disable_backups_for_host(agent_id=agent_id, paths=paths)

    # Re-enable after the disable: fresh provisioning works again (the
    # disable/enable loop is the intended way to reset a workspace's backups).
    repo_three = tmp_path / "restic-repo-3"
    configure_backups_for_host(
        agent_id=agent_id,
        request=BackupSetupRequest(
            backup_provider=BackupProvider.API_KEY, api_key_env_text=f"RESTIC_REPOSITORY={repo_three}"
        ),
        imbue_cloud_cli=None,
        paths=paths,
    )
    canonical_three = read_canonical_env(paths, agent_id)
    assert canonical_three is not None
    assert f"RESTIC_REPOSITORY={repo_three}" in canonical_three
    assert (repo_three / "config").is_file()
    assert read_workspace_env() == canonical_three


# -- In-place restore (the real worker + script, against the resumed workspace) --


def _cp_repo_host_to_container(container_name: str, repository: Path) -> None:
    _run_docker(["cp", str(repository), f"{container_name}:{repository.parent}"], timeout=120)


def _cp_repo_container_to_host(container_name: str, repository: Path) -> None:
    if repository.exists():
        shutil.rmtree(repository)
    _run_docker(["cp", f"{container_name}:{repository}", str(repository.parent)], timeout=120)


def _restic_env_prefix() -> str:
    """Shell prefix exporting the injected restic.env for an in-container restic call."""
    return "set -a; . /home/user/workspace/data/.secrets/restic.env; set +a; "


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(900)
# Restarting the workspace's services at the end of the restore intermittently fails with
# `xvfb: ERROR (spawn error)`, which fails the whole operation and so the test. The restore
# itself completes; only bringing one service back races. Retried while the underlying spawn
# race is investigated -- the marker is what routes this into the retrying offload group.
@pytest.mark.flaky
def test_backup_restore_rewinds_the_resumed_workspace_in_place(
    running_workspace: _ResumedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore the real workspace to a real snapshot via the real product path.

    Drives ``run_backup_restore_sequence`` (the actual desktop worker) end to
    end: minds-side snapshot + subpath resolution, canonical env reinjection,
    the gate probe, and the in-workspace restore script -- which must notice
    the workspace's distro restic predates ``restore --delete``, install the
    pinned build, take the safety snapshot, sync-restore the host dir in
    place, and bring every service back.

    The restic repository must be reachable from both sides (in production it
    is remote object storage): the same absolute path is used on the sandbox
    host and in the container, and the repository directory is copied across
    with ``docker cp`` at the two hand-off points (host->container after
    provisioning initializes it; container->host after the in-container
    source snapshot exists, so minds-side resolution can see it).
    """
    _ensure_restic_on_sandbox_host(tmp_path, monkeypatch)
    # Same desktop-side mngr wiring as the enable/repair test above: the baked
    # host dir + prefix so `mngr exec` can reach the resumed container.
    _point_desktop_mngr_at_the_baked_workspace(tmp_path, monkeypatch)

    container_name = running_workspace.container_name
    agent_id = AgentId(running_workspace.services_agent_id)
    data_dir = tmp_path / "minds-data"
    data_dir.mkdir()
    paths = InstallationPaths(data_dir=data_dir)
    # The repository path must be identical on the sandbox host and inside the
    # container (the same RESTIC_REPOSITORY string is read by both), so it
    # lives at a fixed absolute path rather than the per-test tmp_path. It must
    # NOT live under /tmp: the workspace container mounts /tmp as a tmpfs, and
    # `docker cp` cannot copy into a tmpfs mount.
    repository = Path(f"/var/tmp/restore-e2e-repo-{get_short_random_string()}")

    # Enable backups for real (restic init on the host + env injection into
    # the container), then hand the initialized repository to the container.
    configure_backups_for_host(
        agent_id=agent_id,
        request=BackupSetupRequest(
            backup_provider=BackupProvider.API_KEY, api_key_env_text=f"RESTIC_REPOSITORY={repository}"
        ),
        imbue_cloud_cli=None,
        paths=paths,
    )
    assert (repository / "config").is_file()
    _cp_repo_host_to_container(container_name, repository)

    # A sentinel captures "the state worth restoring"; the source snapshot is
    # taken from inside the container (like the hourly service would), with
    # the workspace's own restic -- the distro 0.14 is fine for backup, which
    # is exactly why the restore script must upgrade for restore --delete.
    sentinel = "/home/user/workspace/restore-e2e-sentinel.txt"
    written = _exec_in_container(container_name, f"printf 'version-one\\n' > {sentinel}", timeout=30)
    assert written.returncode == 0, written.stderr
    source_backup = _exec_in_container(
        container_name,
        _restic_env_prefix()
        + "restic backup /home/user --tag e2e-source --exclude '**/.venv' --exclude '**/node_modules' "
        "--exclude '**/__pycache__' --exclude '**/.cache'",
        timeout=600,
    )
    assert source_backup.returncode == 0, (source_backup.stdout, source_backup.stderr)

    # Work done after the snapshot: the restore must undo both of these.
    mutated = _exec_in_container(
        container_name,
        f"printf 'version-two\\n' > {sentinel} && printf 'after\\n' > /home/user/workspace/restore-e2e-extra.txt",
        timeout=30,
    )
    assert mutated.returncode == 0, mutated.stderr

    # Hand the repository (now carrying the source snapshot) back to the host
    # so minds-side resolution can read it, and find the snapshot to restore.
    _cp_repo_container_to_host(container_name, repository)
    snapshots = backup_status.list_workspace_snapshots(paths, agent_id, parent_cg=None, timeout_seconds=120.0)
    source_snapshots = [snapshot for snapshot in snapshots if "e2e-source" in snapshot.tags]
    assert len(source_snapshots) == 1, [snapshot.tags for snapshot in snapshots]
    snapshot_id = source_snapshots[0].snapshot_id

    # Dispatch the real worker, exactly as the API route does (registered
    # operation, then the worker run synchronously in this thread). The
    # chained update is exercised by its own converge test above; keeping it
    # off here keeps this test focused on the restore path.
    registry = InMemoryWorkspaceOperationRegistry()
    assert registry.start_if_idle(
        agent_id, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc), snapshot_id
    )
    run_backup_restore_sequence(
        agent_id=agent_id,
        paths=paths,
        resolver=StaticBackendResolver(url_by_agent_and_service={}),
        registry=registry,
        parent_cg=None,
        snapshot_id=snapshot_id,
        is_stop_chats=False,
        is_update_after=False,
        is_skip_safety_snapshot=False,
        is_skip_chat_gate=False,
    )

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.DONE, (record.status, record.error)
    assert record.warning is None
    # The streamed script output landed in the operation log (the live
    # details panel feed), proving exec streaming worked end to end.
    log_chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=1.0)
    assert log_chunk is not None
    log_text = "\n".join(log_chunk.lines)
    assert "Restoring the selected backup into place..." in log_text

    # The workspace content was rewound: the sentinel is back at version-one
    # and the post-snapshot file is gone.
    sentinel_after = _exec_in_container(container_name, f"cat {sentinel}", timeout=30)
    assert sentinel_after.returncode == 0, sentinel_after.stderr
    assert sentinel_after.stdout.strip() == "version-one"
    extra_after = _exec_in_container(container_name, "test -f /home/user/workspace/restore-e2e-extra.txt", timeout=30)
    assert extra_after.returncode != 0, "the post-snapshot file should have been deleted by the restore"

    # The injected credentials survived the restore (the snapshot predates
    # them only logically -- the script writes the current env back).
    env_after = _exec_in_container(container_name, "cat /home/user/workspace/data/.secrets/restic.env", timeout=30)
    assert env_after.returncode == 0, env_after.stderr
    assert f"RESTIC_REPOSITORY={repository}" in env_after.stdout

    # The script upgraded the workspace's restic (the distro build predates
    # restore --delete) and persisted the pinned version.
    version_after = _exec_in_container(container_name, "restic version", timeout=30)
    assert version_after.returncode == 0, version_after.stderr
    # The pinned minor (the script pins 0.18.x); update alongside a pin bump.
    assert "restic 0.18" in version_after.stdout, version_after.stdout
    assert "restic 0.14" not in version_after.stdout, version_after.stdout

    # The repository timeline tells the story: the source snapshot, the
    # pre-restore safety snapshot, and the restored state tagged with its
    # lineage.
    timeline = _exec_in_container(container_name, _restic_env_prefix() + "restic snapshots --json", timeout=120)
    assert timeline.returncode == 0, (timeline.stdout, timeline.stderr)
    entries = json.loads(timeline.stdout)
    tags_by_snapshot = [tuple(entry.get("tags") or ()) for entry in entries]
    assert any("pre-restore" in tags for tags in tags_by_snapshot), tags_by_snapshot
    assert any("restored" in tags for tags in tags_by_snapshot), tags_by_snapshot

    # Every supervisord service came back onto the restored tree; the script
    # verified host-backup itself, and the system interface serving again
    # proves the broader workspace is alive.
    assert _wait_for_system_interface_up(container_name)


# -- Bug-report diagnostics against the resumed workspace ---------------------
#
# A bug report can carry the workspace's own logs and its recent chat
# transcripts, gathered by the RESIDENT collector the workspace template ships
# (`system/scripts/collect_bug_report_diagnostics.py`): one small `mngr exec`
# probes for it and runs it, the collector secret-scans everything as plaintext
# with the template's own scan gate, and everything that survives comes back as
# ONE base64 zip the desktop app stages as the report's workspace archive. Both
# halves of that exchange are unit-tested against fixture trees and a fake
# mngr; what only a real workspace can show is that the collector's assumptions
# about the workspace still hold -- that it is installed at the contract path,
# where supervisord writes its logs, how the workspace marks a user-created
# app, that the baked scan gate runs to completion in there against the real
# collected text, and that the whole round trip fits the collection budget. The
# tests below therefore drive the real `collect_workspace_diagnostics` from the
# sandbox host, exactly as the desktop client does.

_SUPERVISORD_CONF_PATH: Final[str] = "/home/user/workspace/system/supervisord.conf"
_SUPERVISOR_LOG_DIR: Final[str] = "/var/log/supervisor"
_SUPERVISORCTL_STATUS_HEADING: Final[str] = "=== supervisorctl status ==="
_SYSTEM_INTERFACE_PROGRAM_NAME: Final[str] = "system_interface"
# The workspace wraps every service it starts in ``oom_tag_service.py <band>``
# and marks a user-created app with the literal band ``user``. That argument is
# what the collector reads to tell diagnostics apart from workspace content, so
# the expected set is derived from the container's own config below rather than
# from a list of service names that would rot as the template changes.
_USER_BAND_COMMAND_PATTERN: Final[re.Pattern[str]] = re.compile(r"oom_tag_service\.py\s+user(\s|$)")
# Every common-transcript event names the event source it was converted from, as
# ``<source>/common_transcript``. ``logs`` is the converter's own stdout stream,
# which sits alongside the real harness transcripts and is written *after* each
# conversion -- so it is newer than the transcript it just wrote and would win
# the collector's newest-wins race if it were eligible. Attaching it would send
# a record that a conversion happened instead of a record of what was said.
_CONVERTER_LOG_EVENT_SOURCE: Final[str] = "logs"
# The collector stages each payload as plaintext into its own ``mktemp -d``
# (``bug-report-scan-*``) inside the workspace so the scan gate can read it, and
# removes that dir on every exit path -- a report must never leave the user's
# own collected logs or chats sitting in their workspace. These staged payload
# files are the ones carrying that content.
_IN_CONTAINER_SCRATCH_GLOB: Final[str] = "/tmp/bug-report-scan-*/bug-report-*.staged"
# Where every agent's transcripts live inside the workspace (the collector's own
# first MNGR_HOME_CANDIDATES entry).
_IN_CONTAINER_MNGR_AGENTS_DIR: Final[str] = "/home/user/.mngr/agents"
# Stands in for a collection's slug while a staged filename's fixed halves are
# read back off the production builder; no real slug (a uuid4 hex) contains it.
_STAGED_SLUG_PLACEHOLDER: Final[str] = "COLLECTION-SLUG"
# The members of the staged workspace archive, as the collection contract names
# them: the logs member, and one ``chats/<agent-id>-<harness>.jsonl`` per chat.
_METADATA_MEMBER_NAME: Final[str] = "metadata.json"
_LOG_MEMBER_DIR_PREFIX: Final[str] = "logs/"
_CHAT_MEMBER_DIR_PREFIX: Final[str] = "chats/"
# Where the collector-missing test parks the real collector while it runs.
_COLLECTOR_MOVED_ASIDE_SUFFIX: Final[str] = ".moved-aside-by-test"
# The collection budget these tests run under. Production's budget is policy
# about user machines; this shared 4-core sandbox (dockerd plus the workspace's
# nested docker-state daemon on cold page cache) exceeded the original 10s
# production budget on 8 of 8 attempts in the first CI runs, warm-up included.
# What these tests exist to prove is the collection pipeline -- what is
# gathered, excluded, scanned, and staged -- so they run it under a budget the
# sandbox can meet, while the production default stays pinned by
# workspace_diagnostics_test.
#
# Raised from 60s once the collector began asking the workspace's own mngr for
# its agents and their transcripts: measured in a running workspace, `mngr list`
# alone costs ~66s and each `mngr event` ~85s, so a single-chat collection needs
# ~160s. At 60s every attachment came back `scanner_unavailable` -- the scan's
# share of the budget was gone before the scan started -- which is what failed
# this tier rather than anything the tests assert.
_SANDBOX_COLLECTION_TIMEOUT_SECONDS: Final[float] = 300.0


def _fake_anthropic_api_key() -> str:
    """A rule-matching fake key, assembled at runtime so no literal key sits in this file.

    Matches the scan gate's betterleaks anthropic rule (sk-ant-[A-Za-z0-9_-]{24,}).
    Mixed-case rather than a single repeated character: the real scanners apply
    entropy filtering on top of the regex, and an all-"x" candidate passes them
    -- this exact shape is the one proven to trip them in a live report.
    """
    return "sk-ant-" + "api03-" + "FAKEFAKEFAKEfakefakefake" + "0" * 46 + "FAKE"


def _chat_agents_in_container(container_name: str) -> list[tuple[str, str]]:
    """``(agent id, agent name)`` for each chat the workspace's own mngr reports.

    Both halves are needed: an agent's state directory is keyed by its ID, while
    the archive member the collector writes is named for the agent's NAME, so a
    fixture plants by one and asserts on the other.

    The collector asks mngr what its chats are, so a fixture has to plant into an
    agent mngr already knows about: a hand-made agent directory is invisible to
    ``mngr list``, and its transcript is therefore never fetched however
    convincing the files look.
    """
    listed = _exec_in_container(
        container_name,
        "export PATH=/root/.local/bin:$PATH; "
        'mngr list --format "{id}|{name}|{labels.is_primary}|{labels.agent_created}" 2>/dev/null',
        timeout=180,
    )
    agents = []
    for line in listed.stdout.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 4 or not parts[0].startswith("agent-") or not parts[1]:
            continue
        if parts[2].lower() == "true" or parts[3].lower() == "true":
            continue
        agents.append((parts[0], parts[1]))
    return agents


def _plant_transcript_pair_in_container(container_name: str, agent_dir: str, chat_line: str) -> None:
    """Write a harness transcript plus a NEWER converter log into an agent mngr lists.

    The baked workspace is created without a chat ever being opened, so without
    this the transcript half of a collection legitimately answers
    ``no_chat_transcript`` and its assertions never run. The planted pair also
    plants the trap collection must not fall into: the converter's own log
    (``events/logs/...``) sits beside every real transcript, newer than what it
    just converted and holding a record that a conversion happened rather than
    what was said.

    ``agent_dir`` must belong to an agent ``mngr list`` reports (see
    ``_chat_agent_ids_in_container``). An invented agent directory used to work,
    when the collector globbed the agent tree itself; it cannot now, because the
    collector asks mngr which agents are chats and mngr has no record of a
    directory someone made behind its back.
    """
    converter_line = json.dumps(
        {
            "type": "common_transcript",
            "source": f"{_CONVERTER_LOG_EVENT_SOURCE}/common_transcript",
            "message": "Converted 1 new event(s)",
        }
    )
    plant = (
        f"mkdir -p {agent_dir}/events/claude/common_transcript {agent_dir}/events/logs/common_transcript"
        f" && printf '%s\\n' '{chat_line}' > {agent_dir}/events/claude/common_transcript/events.jsonl"
        f" && printf '%s\\n' '{converter_line}' > {agent_dir}/events/logs/common_transcript/events.jsonl"
        f" && touch -d '+1 hour' {agent_dir}/events/claude/common_transcript/events.jsonl"
        f" && touch -d '+2 hours' {agent_dir}/events/logs/common_transcript/events.jsonl"
    )
    result = _exec_in_container(container_name, plant, timeout=30)
    assert result.returncode == 0, f"could not plant the transcript fixtures: {result.stderr}"


def _user_program_names_in_container(container_name: str) -> frozenset[str]:
    """The supervisord programs the container's own config marks as user-created."""
    result = _exec_in_container(container_name, f"cat {_SUPERVISORD_CONF_PATH}", timeout=30)
    assert result.returncode == 0, f"could not read {_SUPERVISORD_CONF_PATH}: {result.stderr}"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(result.stdout)
    return frozenset(
        section.removeprefix("program:")
        for section in parser.sections()
        if section.startswith("program:")
        and _USER_BAND_COMMAND_PATTERN.search(parser.get(section, "command", fallback=""))
    )


def _collected_log_program_names(member_names: Iterable[str]) -> frozenset[str]:
    """The supervisord programs the archive carries a log member for.

    Read off the member names rather than parsed out of section headings: each
    service log is its own ``logs/<program>.log`` member now, so the names ARE
    the answer.
    """
    return frozenset(
        name[len(_LOG_MEMBER_DIR_PREFIX) : -len(".log")]
        for name in member_names
        if name.startswith(_LOG_MEMBER_DIR_PREFIX) and name.endswith(".log")
    )


def _staged_filename_bounds(staged_file_key: str) -> tuple[str, str]:
    """The fixed text on either side of one staged file's per-collection slug.

    Read back off the production builder rather than reassembled from its
    constants, so a file staging under a different suffix (the workspace
    archive's ``.zip``) cannot leave these helpers matching an older shape.
    """
    before, after = build_staged_diagnostics_filename(staged_file_key, _STAGED_SLUG_PLACEHOLDER).split(
        _STAGED_SLUG_PLACEHOLDER
    )
    return before, after


def _matches_staged_filename(path: Path, staged_file_key: str) -> bool:
    """Whether ``path`` is named the way one collection stages that file.

    The slug in the middle is minted per collection, so only the shape around it
    can be predicted. A name with no slug at all is rejected: that is the shared
    literal these names moved away from, under which two concurrent reports
    would stage over each other.
    """
    before, after = _staged_filename_bounds(staged_file_key)
    return path.name.startswith(before) and path.name.endswith(after) and len(path.name) > len(before) + len(after)


def _staged_files_in(logs_dir: Path, staged_file_key: str) -> list[Path]:
    """Every staged file in ``logs_dir`` for one staged-file key, from any collection."""
    return sorted(path for path in logs_dir.iterdir() if _matches_staged_filename(path, staged_file_key))


def _read_zip_members(staged_zip: Path) -> dict[str, str]:
    """Every member of a staged workspace archive, DECODED, keyed by member name.

    Content assertions must read decompressed member text: an assertion against
    the staged file's raw bytes would leave the absence checks passing
    vacuously, because compressed bytes contain no readable substring.
    """
    with zipfile.ZipFile(staged_zip) as archive:
        assert archive.testzip() is None, f"the staged workspace archive is not a readable zip: {staged_zip}"
        return {info.filename: archive.read(info).decode("utf-8") for info in archive.infolist()}


def _leftover_collection_scratch_in_container(container_name: str) -> list[str]:
    """Payload files the collector staged for scanning inside the workspace and should have removed."""
    result = _exec_in_container(container_name, f"ls -1 {_IN_CONTAINER_SCRATCH_GLOB} 2>/dev/null || true", timeout=30)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _warm_host_side_mngr() -> None:
    """Run one throwaway ``mngr`` command so collection is not the cold start.

    Collection spends the production budget end to end, and in production it
    is never the host's first mngr invocation -- the desktop client has run
    discovery and lifecycle commands long before anyone files a report. In the
    sandbox it *would* be the first, and the cold start (interpreter boot plus
    the docker provider's first state-container round trip) repeatedly eats the
    whole budget. Warming here makes the test start from production's
    preconditions instead of relaxing the budget it exists to respect. Failure
    is ignored: a broken mngr surfaces through the collection assertions, with
    better diagnostics than this warm-up could give.
    """
    subprocess.run([MNGR_BINARY, "list", "--quiet"], capture_output=True, text=True, timeout=120, check=False)


def _collect_diagnostics_from_workspace(
    running_workspace: _ResumedWorkspace,
    *,
    include_transcript: bool,
    logs_dir: Path,
    baked_mngr_host_dir: Path,
    concurrency_group: ConcurrencyGroup,
    console_text: str | None = None,
) -> WorkspaceDiagnosticsResult:
    """Run the desktop client's real collection against the resumed workspace."""
    _warm_host_side_mngr()
    return collect_workspace_diagnostics(
        AgentId(running_workspace.services_agent_id),
        include_logs=True,
        include_transcript=include_transcript,
        logs_dir=logs_dir,
        host_state=None,
        mngr_binary=MNGR_BINARY,
        mngr_host_dir=baked_mngr_host_dir,
        concurrency_group=concurrency_group,
        timeout_seconds=_SANDBOX_COLLECTION_TIMEOUT_SECONDS,
        console_text=console_text,
    )


@pytest.mark.release
@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(600)
# Collection runs under the sandbox budget (see
# _SANDBOX_COLLECTION_TIMEOUT_SECONDS): the original 10s production budget proved
# unmeetable on this shared sandbox on every attempt, warm-up included. The
# flaky mark stays for residual load variance on the shared 4-core box.
@pytest.mark.flaky
def test_bug_report_diagnostics_collect_the_workspace_logs_and_transcript(
    running_workspace: _ResumedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
) -> None:
    """A report filed against the resumed workspace stages its real logs and transcript.

    Everything the workspace returns arrives as ONE staged archive. Its logs
    member must carry what the bug report is for -- the workspace's current
    service status and the tail of each *system* service's log -- and must not
    carry a user-created app's logs, which are workspace content rather than
    diagnostics. Both the collected set and the excluded set are read back out
    of the container (its supervisord config and the logs member's own section
    headers) so the assertions survive the template gaining or renaming
    services.

    The transcript half runs against a planted pair of transcript files (see
    ``_plant_transcript_pair_in_container``): the archive must hold the chat as
    a ``chats/*.jsonl`` member named for its agent, and never the converter's
    own newer log. Every content assertion reads DECODED member text -- the
    absence assertions are worthless against the archive's raw bytes, which
    match no substring.

    The console half INVERTS what the retired design pinned here: the console
    now attaches UNSCANNED, staged app-side, so a rule-matching fake key fed
    into the tail must ARRIVE in the staged console file. That is the user's
    explicit decision -- the console matches ``electron.log`` and ``minds.log``,
    which already upload unscanned on every event -- and this assertion is what
    stops anyone silently re-adding a scan-or-drop for it. The in-workspace
    scan still guards the workspace content; the chat-side proof lives in
    ``test_bug_report_diagnostics_withhold_every_chat_when_one_carries_a_secret``.
    """
    baked_mngr_host_dir = _point_desktop_mngr_at_the_baked_workspace(tmp_path, monkeypatch)
    logs_dir = tmp_path / "minds-logs"
    logs_dir.mkdir()
    # Dated far ahead so the planted chat outranks any real chat the workspace
    # holds: selection prefers the transcript whose USER last spoke (the file
    # mtimes only break ties and cover transcripts with no user message).
    chat_line = json.dumps(
        {
            "type": "user_message",
            "timestamp": "2030-01-01T00:00:00.000000000Z",
            "source": "claude/common_transcript",
            "message": f"planted-chat-{get_short_random_string()}",
        }
    )
    # mngr is the collector's source of truth for what a chat is, so the plant
    # goes into an agent it already reports rather than an invented directory.
    chat_agents = _chat_agents_in_container(running_workspace.container_name)
    assert chat_agents, "the workspace reports no chat agents to plant a transcript into"
    planted_agent_id, planted_agent_name = chat_agents[0]
    planted_agent_dir = f"{_IN_CONTAINER_MNGR_AGENTS_DIR}/{planted_agent_id}"
    _plant_transcript_pair_in_container(running_workspace.container_name, planted_agent_dir, chat_line)

    fake_api_key = _fake_anthropic_api_key()
    console_text = "\n".join(
        [
            f"renderer ready {get_short_random_string()}",
            f"leaked into the console: {fake_api_key}",
            "shutting down cleanly",
        ]
    )

    try:
        result = _collect_diagnostics_from_workspace(
            running_workspace,
            include_transcript=True,
            logs_dir=logs_dir,
            baked_mngr_host_dir=baked_mngr_host_dir,
            concurrency_group=cg,
            console_text=console_text,
        )
    finally:
        # Only the planted event streams: the directory belongs to a real agent
        # now, so removing it would take the workspace's own state with it.
        _exec_in_container(
            running_workspace.container_name,
            f"rm -rf {planted_agent_dir}/events/claude/common_transcript"
            f" {planted_agent_dir}/events/logs/common_transcript",
            timeout=30,
        )

    assert WORKSPACE_LOGS_ATTACHMENT_KEY not in result.attachment_omissions, dict(result.attachment_omissions)
    assert TRANSCRIPT_ATTACHMENT_KEY not in result.attachment_omissions, dict(result.attachment_omissions)
    staged_zip = result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    assert staged_zip.parent == logs_dir
    assert _matches_staged_filename(staged_zip, WORKSPACE_ZIP_ATTACHMENT_KEY), staged_zip.name
    # The suffix is the whole contract with the upload path, which reads it to
    # know this attachment is already compressed and must not be gzipped again.
    assert staged_zip.suffix == ".zip", staged_zip.name
    member_text_by_name = _read_zip_members(staged_zip)

    metadata_text = member_text_by_name.get(_METADATA_MEMBER_NAME)
    assert metadata_text is not None, sorted(member_text_by_name)
    metadata = json.loads(metadata_text)
    # The structured context: what this workspace is and how it was doing.
    assert metadata["services"]["status"].strip(), metadata["services"]
    assert metadata["workspace"]["commit"].strip(), metadata["workspace"]
    assert metadata["host_health"]["disk"].strip(), metadata["host_health"]

    collected_programs = _collected_log_program_names(member_text_by_name)
    assert _SYSTEM_INTERFACE_PROGRAM_NAME in collected_programs, sorted(collected_programs)
    user_programs = _user_program_names_in_container(running_workspace.container_name)
    leaked = collected_programs & user_programs
    assert not leaked, f"user-created app logs are workspace content and must not be collected: {sorted(leaked)}"
    logger.info(
        "Collected log sections for {}; the workspace marks {} as user-created",
        sorted(collected_programs),
        sorted(user_programs) or "nothing",
    )

    # The planted pair makes the transcript assertions unconditional: a chat
    # exists, so the archive owes that chat's content under a member named for
    # its agent (other chats recently written to may legitimately ride along as
    # further members), and must never carry the converter's log -- the
    # regression the pair is for.
    chat_member_names = [name for name in member_text_by_name if name.startswith(_CHAT_MEMBER_DIR_PREFIX)]
    assert chat_member_names, sorted(member_text_by_name)
    assert all(name.endswith(".jsonl") for name in chat_member_names), chat_member_names
    assert any(chat_line in member_text_by_name[name] for name in chat_member_names), (
        f"the planted chat is missing from the attached archive: {chat_member_names}"
    )
    # Conversations have to stay tellable apart, and the member name is the only
    # thing saying which is which: nothing is injected into the transcript text.
    # Members are named for the agent's NAME, which is what mngr reports and what
    # the collector builds the member from -- not the id its directory is keyed by.
    assert any(planted_agent_name in name for name in chat_member_names), (
        f"no member names the agent whose chat was planted ({planted_agent_name}): {chat_member_names}"
    )
    assert not any("Converted 1 new event(s)" in text for text in member_text_by_name.values()), (
        "the converter's own log rode along in the workspace archive"
    )

    # INVERTED from the retired design: the console attaches unscanned, so the
    # rule-matching key fed into the tail must arrive verbatim in the staged
    # console file. Under the old scan-or-drop this exact tail cost the report
    # its console (secrets_found); a reappearing omission here means someone
    # re-added a scan the user explicitly decided against.
    assert CONSOLE_ATTACHMENT_KEY not in result.attachment_omissions, dict(result.attachment_omissions)
    staged_console = result.staged_paths[CONSOLE_ATTACHMENT_KEY]
    assert staged_console.parent == logs_dir
    assert _matches_staged_filename(staged_console, CONSOLE_ATTACHMENT_KEY), staged_console.name
    assert fake_api_key in staged_console.read_text(encoding="utf-8"), (
        "the console tail did not arrive intact in the staged console file"
    )
    # ...while the unscanned tail must not have leaked into the scanned archive.
    assert not any(fake_api_key in text for text in member_text_by_name.values()), (
        "the console tail leaked into the workspace archive"
    )


@pytest.mark.release
@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(600)
# Runs the real in-container scan under the sandbox budget; the flaky mark
# covers residual load variance on the shared 4-core box (same rationale as the
# full-collection test above).
@pytest.mark.flaky
def test_bug_report_diagnostics_withhold_every_chat_when_one_carries_a_secret(
    running_workspace: _ResumedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
) -> None:
    """One secret in one chat withholds ALL chats, while the clean logs still attach.

    The ``secrets_found`` is the collector's own verdict, produced by the
    template's real scan gate against a planted rule-matching key -- the live
    proof that a leaked key in a conversation never reaches Sentry. A partial
    set of conversations would be indistinguishable from a complete one, so the
    clean chat is withheld along with the poisoned one, and neither may appear
    in the staged archive -- whose members are read DECODED for exactly that
    assertion. The finding names a chat, not the logs, so the logs member still
    attaches: one secret costs the chats, not the whole report.
    """
    baked_mngr_host_dir = _point_desktop_mngr_at_the_baked_workspace(tmp_path, monkeypatch)
    logs_dir = tmp_path / "minds-logs"
    logs_dir.mkdir()
    fake_api_key = _fake_anthropic_api_key()
    clean_marker = f"planted-clean-chat-{get_short_random_string()}"
    clean_chat_line = json.dumps(
        {
            "type": "user_message",
            "timestamp": "2030-01-01T00:00:00.000000000Z",
            "source": "claude/common_transcript",
            "message": clean_marker,
        }
    )
    poisoned_chat_line = json.dumps(
        {
            "type": "user_message",
            "timestamp": "2030-01-01T00:00:01.000000000Z",
            "source": "claude/common_transcript",
            "message": f"here is my key: {fake_api_key}",
        }
    )
    # Two distinct chats are the whole point here -- the clean one is the proof
    # that a finding in the other withholds it too -- and each has to belong to
    # an agent mngr lists, since that is what the collector asks.
    chat_agents = _chat_agents_in_container(running_workspace.container_name)
    assert len(chat_agents) >= 2, f"this test needs two chat agents to plant into; the workspace reports {chat_agents}"
    clean_agent_dir = f"{_IN_CONTAINER_MNGR_AGENTS_DIR}/{chat_agents[0][0]}"
    poisoned_agent_dir = f"{_IN_CONTAINER_MNGR_AGENTS_DIR}/{chat_agents[1][0]}"
    _plant_transcript_pair_in_container(running_workspace.container_name, clean_agent_dir, clean_chat_line)
    _plant_transcript_pair_in_container(running_workspace.container_name, poisoned_agent_dir, poisoned_chat_line)

    try:
        result = _collect_diagnostics_from_workspace(
            running_workspace,
            include_transcript=True,
            logs_dir=logs_dir,
            baked_mngr_host_dir=baked_mngr_host_dir,
            concurrency_group=cg,
        )
    finally:
        _exec_in_container(
            running_workspace.container_name,
            # Only the planted event streams -- these are real agents' directories.
            f"rm -rf {clean_agent_dir}/events/claude/common_transcript"
            f" {clean_agent_dir}/events/logs/common_transcript"
            f" {poisoned_agent_dir}/events/claude/common_transcript"
            f" {poisoned_agent_dir}/events/logs/common_transcript",
            timeout=30,
        )

    assert result.attachment_omissions.get(TRANSCRIPT_ATTACHMENT_KEY) == (
        WorkspaceDiagnosticsOmissionReason.SECRETS_FOUND
    ), dict(result.attachment_omissions)
    assert WORKSPACE_LOGS_ATTACHMENT_KEY not in result.attachment_omissions, dict(result.attachment_omissions)
    staged_zip = result.staged_paths[WORKSPACE_ZIP_ATTACHMENT_KEY]
    member_text_by_name = _read_zip_members(staged_zip)
    assert _METADATA_MEMBER_NAME in member_text_by_name, sorted(member_text_by_name)
    assert not any(name.startswith(_CHAT_MEMBER_DIR_PREFIX) for name in member_text_by_name), (
        f"a chat member survived a secret finding in another chat: {sorted(member_text_by_name)}"
    )
    # Decoded members: neither the poisoned chat's key nor the clean chat's
    # content may be anywhere in what was staged.
    assert not any(fake_api_key in text for text in member_text_by_name.values()), (
        "the planted key reached the staged archive"
    )
    assert not any(clean_marker in text for text in member_text_by_name.values()), (
        "the clean chat shipped despite the withheld set"
    )


@pytest.mark.release
@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(600)
def test_bug_report_diagnostics_report_collector_unavailable_when_the_workspace_predates_it(
    running_workspace: _ResumedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
) -> None:
    """A workspace without the resident collector answers ``collector_unavailable``.

    Workspaces built from templates that predate the collection contract have
    no script at the contract path, and the probe's MISSING sentinel is how a
    report tells that apart from broken exec plumbing. Moving the real
    collector aside turns this live workspace into exactly one of them. The
    workspace content reports ``collector_unavailable`` -- while the console,
    which never depends on the workspace, still stages.
    """
    baked_mngr_host_dir = _point_desktop_mngr_at_the_baked_workspace(tmp_path, monkeypatch)
    logs_dir = tmp_path / "minds-logs"
    logs_dir.mkdir()
    console_text = f"console captured by the shell {get_short_random_string()}\n"
    moved_aside_path = WORKSPACE_COLLECTOR_PATH + _COLLECTOR_MOVED_ASIDE_SUFFIX
    moved = _exec_in_container(
        running_workspace.container_name, f"mv {WORKSPACE_COLLECTOR_PATH} {moved_aside_path}", timeout=30
    )
    assert moved.returncode == 0, f"could not move the collector aside: {moved.stderr}"
    try:
        result = _collect_diagnostics_from_workspace(
            running_workspace,
            include_transcript=True,
            logs_dir=logs_dir,
            baked_mngr_host_dir=baked_mngr_host_dir,
            concurrency_group=cg,
            console_text=console_text,
        )
    finally:
        restored = _exec_in_container(
            running_workspace.container_name, f"mv {moved_aside_path} {WORKSPACE_COLLECTOR_PATH}", timeout=30
        )
        assert restored.returncode == 0, (
            f"could not restore the collector; later collections against this workspace "
            f"would wrongly read collector-less: {restored.stderr}"
        )

    assert result.attachment_omissions == {
        WORKSPACE_LOGS_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.COLLECTOR_UNAVAILABLE,
        TRANSCRIPT_ATTACHMENT_KEY: WorkspaceDiagnosticsOmissionReason.COLLECTOR_UNAVAILABLE,
    }, dict(result.attachment_omissions)
    assert set(result.staged_paths) == {CONSOLE_ATTACHMENT_KEY}, dict(result.staged_paths)
    assert result.staged_paths[CONSOLE_ATTACHMENT_KEY].read_text(encoding="utf-8") == console_text


@pytest.mark.release
@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(300)
def test_bug_report_diagnostics_stage_beside_an_earlier_reports_files(
    running_workspace: _ResumedWorkspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cg: ConcurrencyGroup,
) -> None:
    """A collection stages under its own name and leaves nothing behind in the workspace.

    Reports are submitted immediately and their attachments uploaded in the
    background, so two collections can overlap and an earlier report's staged
    files may still be being read. Each collection therefore stages under its own
    slug: the earlier report's files must survive untouched -- clobbering or
    deleting them would corrupt a report still uploading -- while this
    collection's own archive is a different path holding freshly collected
    content. Neither can ride along on the other regardless, because attachments
    are named one by one, by exact path. The other half of the property is
    inside the workspace: the collector stages each payload as plaintext into a
    temp dir there so its scan gate can read it, and a report must not leave the
    user's own logs sitting in their workspace.

    The tolerated ways for the real round trip to go wrong here are the load
    outcomes -- the sandbox collection budget expiring (``exec_timeout``) or the
    in-container scan not finishing its share of it (``scanner_unavailable``) --
    and both were observed on the shared sandbox. Anything else (``exec_failed``)
    means the exec plumbing is broken and must FAIL rather than silently
    exercising only half the property. The success path itself is covered by
    ``test_bug_report_diagnostics_collect_the_workspace_logs_and_transcript``.
    """
    baked_mngr_host_dir = _point_desktop_mngr_at_the_baked_workspace(tmp_path, monkeypatch)
    logs_dir = tmp_path / "minds-logs"
    logs_dir.mkdir()
    earlier_marker = f"from-an-earlier-report-{get_short_random_string()}"
    earlier_slug = get_short_random_string()
    earlier_zip = logs_dir / build_staged_diagnostics_filename(WORKSPACE_ZIP_ATTACHMENT_KEY, earlier_slug)
    earlier_console = logs_dir / build_staged_diagnostics_filename(CONSOLE_ATTACHMENT_KEY, earlier_slug)
    for earlier_path in (earlier_zip, earlier_console):
        earlier_path.write_text(earlier_marker, encoding="utf-8")

    result = _collect_diagnostics_from_workspace(
        running_workspace,
        include_transcript=False,
        logs_dir=logs_dir,
        baked_mngr_host_dir=baked_mngr_host_dir,
        concurrency_group=cg,
    )

    # An unticked box collects nothing, and the earlier report's files are
    # neither attached to this collection nor taken away from it.
    assert result.attachment_omissions.get(TRANSCRIPT_ATTACHMENT_KEY) == (
        WorkspaceDiagnosticsOmissionReason.NOT_REQUESTED
    ), dict(result.attachment_omissions)
    assert earlier_console.read_text(encoding="utf-8") == earlier_marker

    # Read unconditionally, so this test exercises docker (and honors its mark)
    # on either outcome. Only a collection that finished owes the workspace-side
    # half of the property: one killed by the budget never reaches its own
    # cleanup, and leftovers are then the timeout's doing rather than a defect.
    leftover_scratch = _leftover_collection_scratch_in_container(running_workspace.container_name)

    staged_zip = result.staged_paths.get(WORKSPACE_ZIP_ATTACHMENT_KEY)
    if staged_zip is not None:
        assert staged_zip != earlier_zip
        assert earlier_zip.read_text(encoding="utf-8") == earlier_marker
        member_text_by_name = _read_zip_members(staged_zip)
        # The unticked transcript contributed nothing to the archive either:
        # metadata plus service logs, and no chats/ member at all.
        assert _METADATA_MEMBER_NAME in member_text_by_name, sorted(member_text_by_name)
        assert not any(name.startswith(_CHAT_MEMBER_DIR_PREFIX) for name in member_text_by_name), (
            f"an unticked transcript reached the archive: {sorted(member_text_by_name)}"
        )
        # Decoded member text, not the archive's raw bytes, for the same
        # vacuous-absence reason as everywhere else in this section.
        assert not any(earlier_marker in text for text in member_text_by_name.values())
        assert json.loads(member_text_by_name[_METADATA_MEMBER_NAME])["services"]["status"].strip()
        assert not leftover_scratch, (
            f"collection left the workspace holding its own scratch copies: {leftover_scratch}"
        )
    else:
        assert result.attachment_omissions.get(WORKSPACE_LOGS_ATTACHMENT_KEY) in (
            WorkspaceDiagnosticsOmissionReason.EXEC_TIMEOUT,
            WorkspaceDiagnosticsOmissionReason.SCANNER_UNAVAILABLE,
        ), (
            "collection degraded for a reason other than sandbox load, "
            f"which means the exec plumbing is broken, not slow: {dict(result.attachment_omissions)}"
        )
        assert _staged_files_in(logs_dir, WORKSPACE_ZIP_ATTACHMENT_KEY) == [earlier_zip], (
            f"collection staged no archive ({dict(result.attachment_omissions)}) but wrote a file for it anyway"
        )
