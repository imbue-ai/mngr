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
import hashlib
import json
import os
import pwd
import shutil
import subprocess
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

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.config.data_types import WorkspacePaths
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
from imbue.minds.desktop_client.e2e_workspace_runner import _REPO_ROOT
from imbue.minds.desktop_client.e2e_workspace_runner import _send_message_and_await_reply
from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import create_workspace_via_electron
from imbue.minds.desktop_client.e2e_workspace_runner import destroy_agent_best_effort
from imbue.minds.desktop_client.e2e_workspace_runner import ensure_minds_env_defaults
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_default_workspace_template_path
from imbue.minds.desktop_client.recovery_probe import PROBE_SENTINEL
from imbue.minds.desktop_client.recovery_probe import build_probe_shell_command
from imbue.minds.desktop_client.restic_cli import ResticNotInstalledError
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.primitives import BackupProvider
from imbue.mngr.config.pre_readers import find_profile_dir_lightweight
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.testing import get_short_random_string

# The minds workspace container name prefix (mngr names docker hosts
# ``{mngr_prefix}-{host_name}`` and minds defaults to the ``minds-staging``
# prefix at snapshot time). The docker provider also keeps a singleton
# ``*docker-state*`` sidecar container per workspace; the workspace agent
# container is the one that is NOT the docker-state sidecar.
_WORKSPACE_CONTAINER_PREFIX: Final[str] = "minds-"
_DOCKER_STATE_MARKER: Final[str] = "docker-state"
# system_interface's in-container port. It is a core bootstrap-managed
# service with a fixed port (registered in runtime/applications.toml);
# kept as a constant so a drift shows up as a clear assertion failure.
_SYSTEM_INTERFACE_PORT: Final[int] = 8000
_MNGR_START_TIMEOUT_SECONDS: Final[int] = 300
_SYSTEM_INTERFACE_READY_TIMEOUT_SECONDS: Final[int] = 120
_PROBE_TIMEOUT_SECONDS: Final[int] = 120

# mngr lifecycle states that mean the agent's tmux window is alive (as opposed
# to STOPPED / DONE). The system-services agent's window-0 command is
# ``sleep infinity && claude`` -- claude is unreachable by design (see the
# minds README), so mngr observes a non-claude process occupying the window
# and reports REPLACED rather than RUNNING. Both indicate the agent is up.
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
    return _run_docker(["ps", "-aq", "--filter", f"name={_WORKSPACE_CONTAINER_PREFIX}"]).split()


def _running_workspace_container_name() -> str:
    """Return the running workspace agent container (not the docker-state sidecar)."""
    names = _run_docker(["ps", "--format", "{{.Names}}"]).splitlines()
    workspace_names = [
        name for name in names if name.startswith(_WORKSPACE_CONTAINER_PREFIX) and _DOCKER_STATE_MARKER not in name
    ]
    assert workspace_names, f"No running minds workspace container; running containers: {names!r}"
    return workspace_names[0]


def _start_all_minds_containers() -> None:
    container_ids = _all_minds_container_ids()
    assert container_ids, "No minds containers captured in the snapshot to start."
    # Start them in one call; docker start is idempotent for already-running ones.
    subprocess.run(["docker", "start", *container_ids], check=True, capture_output=True, text=True, timeout=120)


def _list_agents_in_container(container_name: str) -> list[dict[str, Any]]:
    """Return the agents mngr sees from inside the workspace container.

    Run from inside the host (the container), where mngr uses the local
    provider and the baked-in ``MNGR_HOST_DIR=/mngr`` -- no desktop-side
    provider fan-out, so an unrelated (uncredentialed) provider can't blank
    the listing. ``--on-error continue`` keeps any single provider failure
    from aborting the list.
    """
    result = _exec_in_container(container_name, "cd /code && mngr list --format json --on-error continue", timeout=60)
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


def _run_minds_in_container_probe(container_name: str) -> dict[str, Any]:
    """Run minds' real in-container recovery probe and return its parsed payload.

    Ships the exact probe shell command minds' recovery flow runs (normally
    via ``mngr exec``) straight into the container via ``docker exec``, then
    parses the documented sentinel + single-JSON-line contract. The payload
    carries ``curl_status`` (system_interface health), ``inner_port``,
    ``system_interface_status``, etc.
    """
    result = _exec_in_container(
        container_name, f"cd /code && {build_probe_shell_command()}", timeout=_PROBE_TIMEOUT_SECONDS
    )
    assert PROBE_SENTINEL in result.stdout, (
        f"minds recovery probe sentinel missing; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    after_sentinel = result.stdout.split(PROBE_SENTINEL, 1)[1]
    for line in after_sentinel.splitlines():
        candidate = line.strip()
        if candidate:
            return json.loads(candidate)
    raise AssertionError(f"minds recovery probe emitted no JSON payload; stdout={result.stdout!r}")


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
        container_name, f"cd /code && mngr start {services_agent_id} --quiet", timeout=_MNGR_START_TIMEOUT_SECONDS
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
      ``minds-staging`` prefix at snapshot time)
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

    The services agent runs ``sleep infinity && claude`` (claude unreachable by
    design), so mngr reports it as ``REPLACED`` -- a non-claude process holding
    the window -- rather than ``RUNNING``. Both are "alive"; only STOPPED/DONE
    would mean the resume failed to bring the agent back.
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
def test_resumed_workspace_registered_expected_services(running_workspace: _ResumedWorkspace) -> None:
    """After resume, the bootstrap re-registered the core services in applications.toml.

    The app-watcher / bootstrap respawns the standard services on restart and
    each registers its port into ``runtime/applications.toml``; the always-on
    core services (``system_interface`` and ``terminal``) must be present.

    ``web`` was intentionally dropped: default-workspace-template removed the
    blank example web service (its ``[program:web]`` supervisord entry and the
    ``libs/web_server`` scaffold), so it no longer registers. ``browser`` does
    autostart now, but it is memory-heavy and expendable (earlyoom can shed it
    under pressure), so requiring it would make this test flaky -- we only
    assert the services guaranteed to survive a resume.
    """
    result = _exec_in_container(running_workspace.container_name, "cat /code/runtime/applications.toml", timeout=30)
    assert result.returncode == 0, f"Could not read runtime/applications.toml: {result.stderr}"
    for service_name in ("system_interface", "terminal"):
        assert service_name in result.stdout, (
            f"Service {service_name!r} not registered in applications.toml after resume:\n{result.stdout}"
        )


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(360)
def test_minds_recovery_restores_dead_system_interface() -> None:
    """minds' recovery flow brings back a workspace whose system_interface is dead.

    Drives the actual minds recovery building blocks against a deterministic
    break: stop the system-services agent (which takes system_interface down),
    confirm minds' real in-container recovery probe diagnoses it as unhealthy,
    then perform minds' surgical restart (``mngr stop`` + ``mngr start`` on
    the system-services agent, exactly what the desktop client's recovery
    endpoint runs) and confirm both the live HTTP check and minds' probe see
    system_interface healthy again.

    Self-contained (it establishes its own broken state) so it is robust to
    running in the same sandbox as the ``running_workspace`` fixture tests.
    """
    _start_all_minds_containers()
    container_name = _running_workspace_container_name()
    services_agent_id = _system_services_agent_id(container_name)

    # Break it: stopping the system-services agent tears down the bootstrap
    # and the services it manages, including system_interface.
    stop_result = _exec_in_container(
        container_name, f"cd /code && mngr stop {services_agent_id} --quiet", timeout=_MNGR_START_TIMEOUT_SECONDS
    )
    assert stop_result.returncode == 0, (
        f"Failed to stop system-services to set up the broken state: {stop_result.stderr}"
    )

    # Wait for the interface to actually go down before diagnosing, so the
    # broken-state assertion isn't a timing race against a lingering listener.
    assert _wait_for_system_interface_down(container_name), (
        "system_interface stayed up after stopping the system-services agent; "
        "the agent may not own the bootstrap/system_interface process tree."
    )
    # minds' real recovery probe should now see system_interface unhealthy.
    broken_probe = _run_minds_in_container_probe(container_name)
    assert broken_probe.get("curl_status") not in _SERVED_HTTP_STATUS_CODES, (
        f"Expected system_interface unhealthy after stopping system-services; probe={broken_probe!r}"
    )

    # Recover the way minds' desktop client does: surgical restart of the
    # system-services agent (stop is idempotent here; start respawns it).
    restart_result = _exec_in_container(
        container_name,
        f"cd /code && mngr stop {services_agent_id} --quiet; mngr start {services_agent_id} --quiet",
        timeout=_MNGR_START_TIMEOUT_SECONDS,
    )
    assert restart_result.returncode == 0, f"minds-style surgical restart failed: {restart_result.stderr}"

    assert _wait_for_system_interface_up(container_name), (
        "system_interface did not recover after minds' surgical restart of system-services."
    )
    recovered_probe = _run_minds_in_container_probe(container_name)
    assert recovered_probe.get("curl_status") in _SERVED_HTTP_STATUS_CODES, (
        f"minds recovery probe still reports system_interface unhealthy after restart; probe={recovered_probe!r}"
    )


# -- Electron-driven create + chat (a second workspace) -----------------------
#
# The snapshot image bakes a warm Electron/Playwright/Xvfb/Docker toolchain (the
# snapshot *build* drives that same toolchain to create the first workspace).
# This test reuses that warm toolchain to drive the real Electron app and create
# a SECOND workspace -- this time via the manual ``api_key`` AI-provider option
# -- then sends a chat message to its ``system_interface`` and asserts the agent
# replies. It runs in the same offload snapshot stage (carries
# minds_snapshot_resume), so all the "drive Electron" coverage lives in one place
# instead of a separate cold-install CI job. It does NOT use the baked first
# workspace (it creates its own), so it is independent of the
# ``running_workspace`` fixture.


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


def _prepare_electron_workspace_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Set the minds env + provider overrides and materialize the throwaway DEFAULT_WORKSPACE_TEMPLATE + host config.

    Returns ``(default_workspace_template_path, host_config_root)`` for ``create_workspace_via_electron``.
    """
    configure_logging()
    # Route env-var defaults through monkeypatch so injected MINDS_ROOT_NAME /
    # MINDS_CLIENT_CONFIG_PATH revert between tests; defaults to the committed
    # minds-staging tier.
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


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_create_apikey_workspace_and_chat_via_electron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    xvfb_display: str,
) -> None:
    """Drive Electron to create a manual-API-key Docker workspace, then chat with it.

    The product-level round-trip: pick the ``api_key`` AI provider, type a raw
    Anthropic key, create a local Docker workspace from DEFAULT_WORKSPACE_TEMPLATE, and assert the agent
    in the workspace's ``system_interface`` answers a chat message (echoes a
    unique token) -- end-to-end through the real Electron app and the desktop
    client proxy.

    Runs in the snapshot offload sandbox, reusing the warm Electron toolchain
    baked into the image (the ``xvfb_display`` fixture supplies the display the
    sandbox lacks). Needs a real Anthropic key: the ``API_KEY`` path talks to the
    official Anthropic API directly (no LiteLLM proxy), so the agent only replies
    if the key works. The key is read from ``ANTHROPIC_API_KEY`` (forwarded into
    this stage from Vault) and typed into the create form -- the Electron child
    env scrubs that var, so the key reaches the agent only via the form,
    exercising the real manual-key UX. Skips if the key is absent.
    """
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY is required for the manual-key workspace chat round-trip")

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
            anthropic_api_key=anthropic_api_key,
            on_workspace_ready=lambda page: _send_message_and_await_reply(page, token),
        )
    finally:
        destroy_agent_best_effort(workspace_name, config_project_dir=host_config_root / ".mngr")


# -- Backup-update chat gate against a live, LLM-authenticated chat agent -----
#
# The backup update's chat gate must (a) classify a claude agent that is
# actively generating as a running chat, (b) block the mutating update on it,
# and (c) stop it for real when the "Stop all chats and retry" flow passes
# --stop-chats. The unit tests in backup_workspace_scripts_test.py drive these
# paths with a stubbed `mngr list`; this test drives them against the resumed
# snapshot workspace's real chat agent, which has working LLM credentials
# baked in -- asking it for a long story keeps it RUNNING long enough for the
# gate to observe it.


def _find_chat_agent(container_name: str) -> dict[str, Any]:
    """Return the workspace's (claude-type) chat agent record from mngr list."""
    agents = _list_agents_in_container(container_name)
    chats = [agent for agent in agents if agent.get("type") == "claude"]
    assert chats, f"No claude chat agent among {[a.get('name') for a in agents]!r}"
    return chats[0]


def _wait_for_agent_state(container_name: str, agent_id: str, expected_state: str, *, attempts: int = 40) -> bool:
    """Poll (shell-side, no python sleeps) until the agent reports the state, or time out."""
    read_state = (
        "cd /code && mngr list --format json --on-error continue | "
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
    """Run one of the minds backup workspace scripts inside the container at /code."""
    command = build_workspace_script_command(script, args)
    return _exec_in_container(container_name, f"cd /code && {command}", timeout=timeout)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(900)
def test_backup_update_gate_blocks_on_live_chat_and_stop_chats_clears_it(
    running_workspace: _ResumedWorkspace,
) -> None:
    """The chat gate blocks on a genuinely generating claude agent and --stop-chats stops it."""
    container_name = running_workspace.container_name
    chat = _find_chat_agent(container_name)
    chat_id = str(chat["id"])
    chat_name = str(chat["name"])

    # Wake the chat and give it work that takes a while: a real LLM generation
    # (the baked workspace carries working credentials). `mngr message`
    # delivers the keystrokes and returns; claude then reads as RUNNING while
    # it generates.
    started = _exec_in_container(container_name, f"cd /code && mngr start {chat_id} --quiet", timeout=180)
    assert started.returncode == 0, f"`mngr start` failed for the chat agent: {started.stderr}"
    story_marker = get_short_random_string()
    messaged = _exec_in_container(
        container_name,
        f'cd /code && mngr message {chat_id} -m "Please tell me a really long story about {story_marker}. '
        'Make it as long and detailed as you possibly can."',
        timeout=120,
    )
    assert messaged.returncode == 0, f"`mngr message` failed for the chat agent: {messaged.stderr}"
    assert _wait_for_agent_state(container_name, chat_id, "RUNNING"), (
        "The chat agent never reached RUNNING after being asked for a long story."
    )

    # The gate probe classifies the generating chat as a running chat.
    probe = _run_backup_script_in_container(
        container_name,
        BACKUP_GATE_PROBE_SCRIPT,
        ("--agent-id", running_workspace.services_agent_id),
        timeout=300,
    )
    probe_payload = extract_marker_json(probe.stdout, GATE_RESULT_MARKER)
    assert probe_payload is not None, (probe.stdout, probe.stderr)
    probe_chats = probe_payload["running_chats"]
    assert isinstance(probe_chats, list) and chat_name in probe_chats, probe_payload

    # The mutating update refuses to run while the chat is generating.
    blocked = _run_backup_script_in_container(
        container_name,
        BACKUP_APPLY_UPDATE_SCRIPT,
        ("--minds-version", "0.0.0-snapshot-test", "--agent-id", running_workspace.services_agent_id),
        timeout=600,
    )
    blocked_payload = extract_marker_json(blocked.stdout, UPDATE_RESULT_MARKER)
    assert blocked_payload is not None, (blocked.stdout, blocked.stderr)
    assert blocked_payload["status"] == "blocked", blocked_payload
    blocked_chats = blocked_payload["running_chats"]
    assert isinstance(blocked_chats, list) and chat_name in blocked_chats, blocked_payload

    # "Stop all chats and retry": the script stops the live chat itself and
    # proceeds past the gate. Whether the rest of the update succeeds depends
    # on the baked repo's tags; the contract under test is that the outcome is
    # anything but blocked and the chat agent is genuinely stopped.
    retried = _run_backup_script_in_container(
        container_name,
        BACKUP_APPLY_UPDATE_SCRIPT,
        ("--minds-version", "0.0.0-snapshot-test", "--agent-id", running_workspace.services_agent_id, "--stop-chats"),
        timeout=600,
    )
    retried_payload = extract_marker_json(retried.stdout, UPDATE_RESULT_MARKER)
    assert retried_payload is not None, (retried.stdout, retried.stderr)
    assert retried_payload["status"] != "blocked", retried_payload
    assert _wait_for_agent_state(container_name, chat_id, "STOPPED"), (
        "The chat agent was not stopped by the --stop-chats gate."
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
    return _exec_in_container(container_name, f"cd /code && git {args}", timeout=60)


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
    `mngr exec` injection/rotation of `runtime/secrets/restic.env` inside the
    resumed workspace container.
    """
    _ensure_restic_on_sandbox_host(tmp_path, monkeypatch)
    # The provisioning path shells out to `mngr exec <agent>` from this
    # process. Give that mngr the snapshot's real container prefix AND the
    # snapshot's real desktop-side host dir: the docker provider reaches its
    # host records through a state container named after the profile's user id
    # under MNGR_HOST_DIR, so the autouse fixture's throwaway host dir (fresh
    # profile, different user id) would make the baked workspace invisible
    # ("Agent not found"). The baked host dir lives under the *real* home --
    # the autouse fixture monkeypatches HOME to a temp dir, so resolve it via
    # /etc/passwd (same trick as deployment_tests/conftest.py) using the
    # mngr_host_dir_for layout. Its baked profile settings get the pytest
    # config-guard opt-in (throwaway sandbox state, per the helper's contract),
    # and the project config is an isolated pytest-opted-in copy (the repo's
    # own .mngr would fail the config guard). Use a neutral cwd and silence
    # providers that would need cloud credentials during discovery.
    root_name = os.environ.get("MINDS_ROOT_NAME", "minds-staging")
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

    container_name = running_workspace.container_name
    agent_id = AgentId(running_workspace.services_agent_id)

    data_dir = tmp_path / "minds-data"
    data_dir.mkdir()
    paths = WorkspacePaths(data_dir=data_dir)
    repo_one = tmp_path / "restic-repo-1"
    repo_two = tmp_path / "restic-repo-2"

    def read_workspace_env() -> str:
        result = _exec_in_container(container_name, "cat /code/runtime/secrets/restic.env", timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    # Enable backups on the (configure-later) workspace: real restic init +
    # random per-workspace password + injection into the real container.
    configure_backups_for_host(
        agent_id=agent_id,
        host_id="host-snapshot-test",
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
        container_name, "printf 'RESTIC_REPOSITORY=garbage\n' > /code/runtime/secrets/restic.env", timeout=30
    )
    assert corrupted.returncode == 0, corrupted.stderr
    reinject_canonical_env(agent_id=agent_id, paths=paths)
    assert read_workspace_env() == canonical_one
    rotated = _exec_in_container(container_name, "grep -l garbage /code/runtime/secrets/restic.env.*", timeout=30)
    assert rotated.returncode == 0 and rotated.stdout.strip(), (rotated.stdout, rotated.stderr)

    # Destination change: fresh provisioning against repo two; the old
    # canonical env is archived minds-side and the workspace copy replaced.
    change_backup_destination_for_host(
        agent_id=agent_id,
        host_id="host-snapshot-test",
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
    gone = _exec_in_container(container_name, "test -f /code/runtime/secrets/restic.env", timeout=30)
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
        host_id="host-snapshot-test",
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
    return "set -a; . /code/runtime/secrets/restic.env; set +a; "


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.timeout(900)
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
    root_name = os.environ.get("MINDS_ROOT_NAME", "minds-staging")
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

    container_name = running_workspace.container_name
    agent_id = AgentId(running_workspace.services_agent_id)
    data_dir = tmp_path / "minds-data"
    data_dir.mkdir()
    paths = WorkspacePaths(data_dir=data_dir)
    # The repository path must be identical on the sandbox host and inside the
    # container (the same RESTIC_REPOSITORY string is read by both), so it
    # lives under /tmp rather than the per-test tmp_path.
    repository = Path(f"/tmp/restore-e2e-repo-{get_short_random_string()}")

    # Enable backups for real (restic init on the host + env injection into
    # the container), then hand the initialized repository to the container.
    configure_backups_for_host(
        agent_id=agent_id,
        host_id="host-restore-e2e",
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
    sentinel = "/code/restore-e2e-sentinel.txt"
    written = _exec_in_container(container_name, f"printf 'version-one\\n' > {sentinel}", timeout=30)
    assert written.returncode == 0, written.stderr
    source_backup = _exec_in_container(
        container_name,
        _restic_env_prefix() + "restic backup /mngr --tag e2e-source --exclude '**/.venv' --exclude '**/node_modules' "
        "--exclude '**/__pycache__' --exclude '**/.cache'",
        timeout=600,
    )
    assert source_backup.returncode == 0, (source_backup.stdout, source_backup.stderr)

    # Work done after the snapshot: the restore must undo both of these.
    mutated = _exec_in_container(
        container_name,
        f"printf 'version-two\\n' > {sentinel} && printf 'after\\n' > /code/restore-e2e-extra.txt",
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
    extra_after = _exec_in_container(container_name, "test -f /code/restore-e2e-extra.txt", timeout=30)
    assert extra_after.returncode != 0, "the post-snapshot file should have been deleted by the restore"

    # The injected credentials survived the restore (the snapshot predates
    # them only logically -- the script writes the current env back).
    env_after = _exec_in_container(container_name, "cat /code/runtime/secrets/restic.env", timeout=30)
    assert env_after.returncode == 0, env_after.stderr
    assert f"RESTIC_REPOSITORY={repository}" in env_after.stdout

    # The script upgraded the workspace's restic (the distro build predates
    # restore --delete) and persisted the pinned version.
    version_after = _exec_in_container(container_name, "restic version", timeout=30)
    assert version_after.returncode == 0, version_after.stderr
    assert "restic 0.1" in version_after.stdout
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
