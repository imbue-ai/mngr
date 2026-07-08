"""Sanity tests for sandboxes booted from a minds-workspace snapshot.

Run via::

    just test-offload-minds-snapshot <snapshot-image-id>

where ``<snapshot-image-id>`` is the Modal image id printed by
``scripts/snapshot_minds_e2e_state.py``. That script captures a Modal
sandbox in which the FCT workspace's ``system_interface`` UI has
rendered, then ``docker stop``s the workspace containers so the
filesystem snapshot represents a deterministic stopped state.

Every test here carries ``@pytest.mark.minds_snapshot_resume`` and
asserts something about that pre-baked state. The mark is excluded
from every other offload config (see ``offload-modal*.toml``) so a
``minds_snapshot_resume`` test only ever runs against the right kind
of sandbox.
"""

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final

import pytest
import tomlkit
from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.e2e_workspace_runner import _REPO_ROOT
from imbue.minds.desktop_client.e2e_workspace_runner import _send_message_and_await_reply
from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import create_workspace_via_electron
from imbue.minds.desktop_client.e2e_workspace_runner import destroy_agent_best_effort
from imbue.minds.desktop_client.e2e_workspace_runner import ensure_minds_env_defaults
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_fct_path
from imbue.minds.desktop_client.recovery_probe import PROBE_SENTINEL
from imbue.minds.desktop_client.recovery_probe import build_probe_shell_command
from imbue.mngr.utils.testing import get_short_random_string

_START_DOCKERD_SCRIPT: Final[Path] = Path("/code/mngr/libs/mngr/imbue/mngr/resources/start-dockerd.sh")
_DOCKERD_STARTUP_TIMEOUT_SECONDS: Final[int] = 180

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
def _ensure_dockerd_after_snapshot_resume() -> None:
    """Bring ``dockerd`` back up in a snapshot-resumed sandbox.

    ``sandbox.snapshot_filesystem`` only captures the disk, not running
    processes -- so a sandbox booted from the minds-workspace snapshot
    has ``/var/lib/docker`` populated (with the stopped workspace
    container's image layers + on-disk state) but no ``dockerd`` running.
    Re-run the same script the snapshot itself used to bring dockerd up,
    so subsequent ``docker`` invocations in these tests can talk to the
    daemon.

    Mirrors ``_ensure_dockerd_for_release`` in
    ``libs/mngr/imbue/mngr/conftest.py`` but for our snapshot's script
    location (the release image lives at ``/start-dockerd.sh``; the
    snapshot tree puts the same script at its in-tree path).
    """
    docker_info = subprocess.run(["docker", "info"], capture_output=True)
    if docker_info.returncode == 0:
        return

    if not _START_DOCKERD_SCRIPT.is_file():
        raise FileNotFoundError(
            f"start-dockerd.sh not found at {_START_DOCKERD_SCRIPT}; this fixture is "
            "only useful inside a sandbox booted from scripts/snapshot_minds_e2e_state.py."
        )

    subprocess.run(["chmod", "+x", str(_START_DOCKERD_SCRIPT)], check=True, timeout=5)
    subprocess.run(
        [str(_START_DOCKERD_SCRIPT)],
        check=True,
        timeout=_DOCKERD_STARTUP_TIMEOUT_SECONDS,
    )


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
    """The snapshot captured a stopped FCT workspace Docker container.

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
    each registers its port into ``runtime/applications.toml``; the core set
    (system_interface, web, terminal) must be present.
    """
    result = _exec_in_container(running_workspace.container_name, "cat /code/runtime/applications.toml", timeout=30)
    assert result.returncode == 0, f"Could not read runtime/applications.toml: {result.stderr}"
    for service_name in ("system_interface", "web", "terminal"):
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
    so ``settings_path`` must live under a throwaway tree (``tmp_path`` or an FCT
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
    """Set the minds env + provider overrides and materialize the throwaway FCT + host config.

    Returns ``(fct_path, host_config_root)`` for ``create_workspace_via_electron``.
    """
    configure_logging()
    # Route env-var defaults through monkeypatch so injected MINDS_ROOT_NAME /
    # MINDS_CLIENT_CONFIG_PATH revert between tests; defaults to the committed
    # minds-staging tier.
    ensure_minds_env_defaults(setenv=monkeypatch.setenv)
    # No Modal creds here, so silence the Electron-spawned mngr's Modal discovery.
    monkeypatch.setenv("MNGR__PROVIDERS__MODAL__IS_ENABLED", "false")
    # Pin the local-docker workspace to runc; gVisor (runsc) is absent in CI /
    # the sandbox. Two layers, because runsc can be selected two ways:
    #   1. MINDS_DOCKER_RUNTIME_DEFAULT pins the create form / API default to
    #      runc so minds never stacks the `docker_runsc` create-template (whose
    #      docker_runtime an mngr env var cannot override once explicitly stacked).
    #   2. MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc still overrides the FCT
    #      `docker` template's own default, covering an older resolved FCT whose
    #      `[providers.docker]` block hardcodes runsc without a separate overlay.
    monkeypatch.setenv("MINDS_DOCKER_RUNTIME_DEFAULT", "RUNC")
    monkeypatch.setenv("MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME", "runc")
    # The Electron-spawned mngr loads two project-config trees under
    # PYTEST_CURRENT_TEST: the host-side config (a throwaway opted-in copy built
    # here) and the FCT worktree. The FCT worktree is materialized ahead of time
    # by ``materialize_paired_fct_worktree`` (baked into the snapshot image in
    # CI, or created by the local test recipe) with its pytest opt-in already
    # committed, so this only resolves it -- and errors loudly if the materialize
    # step never ran.
    fct_path = resolve_fct_path()
    host_config_root = _isolated_host_config_root(tmp_path)
    return fct_path, host_config_root


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
    Anthropic key, create a local Docker workspace from FCT, and assert the agent
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

    fct_path, host_config_root = _prepare_electron_workspace_inputs(tmp_path, monkeypatch)

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
            fct_path,
            workspace_name,
            debug_port,
            host_config_dir=host_config_root,
            anthropic_api_key=anthropic_api_key,
            on_workspace_ready=lambda page: _send_message_and_await_reply(page, token),
        )
    finally:
        destroy_agent_best_effort(workspace_name, config_project_dir=host_config_root / ".mngr")
