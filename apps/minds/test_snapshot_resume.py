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

import subprocess
from pathlib import Path
from typing import Final

import pytest

_START_DOCKERD_SCRIPT: Final[Path] = Path("/code/mngr/libs/mngr/imbue/mngr/resources/start-dockerd.sh")
_DOCKERD_STARTUP_TIMEOUT_SECONDS: Final[int] = 180


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
