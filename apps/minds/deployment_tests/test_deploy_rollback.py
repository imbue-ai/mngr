"""``minds_deployment`` test: auto-rollback on a broken connector ``/health/liveness``.

Drives the v1 -> broken-v2 sequence and asserts the existing
``minds env deploy`` auto-rollback path restores v1 when
``await_apps_healthy`` fails. Does *not* deploy a clean v3 -- the
"redeploy advances version" contract is covered separately by
``test_deploy_new_version``.

Mechanism: the v2 deploy subprocess is invoked with
``MINDS_INJECT_BROKEN_HEALTHCHECK=1`` in its env. ``provisioning.py``
reads that env var at deploy-secret-construction time and propagates
it into the deployed connector's Modal Secret bundle. The deployed
container then 500s on every ``/health/liveness`` request, which
fails ``await_apps_healthy``, which raises ``HealthCheckFailedError``
out of ``deploy_env``, which the CLI catches and chains into
``minds env recover`` via ``_exec_into_recover``. Recover walks its
reversal steps and ``modal app rollback``s both apps to their captured
pre-deploy versions.

The subprocess exits with the recover process's exit code (``os.execvp``
replaces the deploy process with recover). Successful rollback ->
recover exits 0 -> subprocess returncode 0. The actual assertion is on
``/version``: after rollback the connector reports v1's ``deploy_id``,
not v2's.
"""

import subprocess
import time
from pathlib import Path

import httpx
import pytest
from loguru import logger

from imbue.minds.deployment_tests.data_types import EphemeralEnvHandle
from imbue.minds.deployment_tests.helpers import build_minds_env_subprocess_env

_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.minds_deployment

# v2 deploy + 5s rollback countdown + recover (modal app rollback x2,
# neon snapshot restore, secret cleanup) all need to finish under this.
_TEST_TIMEOUT_SECONDS = 20 * 60

_REQUEST_TIMEOUT_SECONDS = 30.0
_BROKEN_DEPLOY_TIMEOUT_SECONDS = 15 * 60
# After rollback, the connector is serving traffic at v1. Polling
# ``/version`` should immediately or near-immediately report v1's id
# (modal app rollback flips the live version atomically server-side).
# Generous polling tolerance for Modal's swap window.
_VERSION_POLL_TIMEOUT_SECONDS = 90.0
_VERSION_POLL_INTERVAL_SECONDS = 2.0


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_deploy_auto_rollback_on_broken_healthcheck(ephemeral_env: EphemeralEnvHandle) -> None:
    """v2 deploys with a broken /health/liveness; assert auto-rollback restores v1.

    Flow:
    1. Capture v1's ``deploy_id`` via ``GET <connector>/version``.
    2. Run ``minds env deploy`` against the same env with
       ``MINDS_INJECT_BROKEN_HEALTHCHECK=1`` in the subprocess env.
       ``await_apps_healthy`` fails -> auto-recover fires -> recover
       rolls Modal apps back -> subprocess exits 0 (recover succeeded
       at restoring the pre-v2 state).
    3. Poll ``GET <connector>/version`` and assert ``deploy_id`` matches
       v1's id (and the response is 200 -- proving v1 is actually
       serving traffic, not just that the dashboard label moved back).

    Intentionally does NOT deploy a clean v3 -- the "redeploy advances
    version" contract is covered by ``test_deploy_new_version``.
    """
    connector_url = str(ephemeral_env.connector_url).rstrip("/")
    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        version_v1 = client.get(f"{connector_url}/version")
    assert version_v1.status_code == 200, version_v1.text
    v1_deploy_id = version_v1.json()["deploy_id"]
    assert v1_deploy_id, version_v1.json()

    # v2 deploy: same activation env, plus the injection toggle.
    # provisioning.py reads MINDS_INJECT_BROKEN_HEALTHCHECK from the
    # subprocess env and merges it into the connector's Modal Secret.
    sub_env = build_minds_env_subprocess_env(ephemeral_env.name)
    sub_env["MINDS_INJECT_BROKEN_HEALTHCHECK"] = "1"
    completed = subprocess.run(
        ["uv", "run", "minds", "env", "deploy"],
        env=sub_env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_BROKEN_DEPLOY_TIMEOUT_SECONDS,
        check=False,
    )
    logger.info("=== v2 deploy stdout ({}) ===\n{}", ephemeral_env.name, completed.stdout)
    logger.info("=== v2 deploy stderr ({}) ===\n{}", ephemeral_env.name, completed.stderr)
    # The CLI uses ``os.execvp`` to chain into ``minds env recover`` on
    # MindError, so the returncode is the recover process's exit code.
    # Successful rollback => recover exited 0 => subprocess exits 0.
    # A non-zero returncode means recover itself failed (RecoverFailedError)
    # -- surface the stderr so the operator can diagnose.
    assert completed.returncode == 0, (
        f"Broken-v2 deploy + auto-recover for {ephemeral_env.name!r} exited {completed.returncode}. "
        f"Stderr tail:\n{completed.stderr[-2000:]}"
    )

    # Post-rollback, /version should report v1's deploy_id again (the
    # rollback restored both Modal apps to their pre-v2 versions).
    final_deploy_id = _poll_for_deploy_id(
        connector_url=connector_url,
        expected_deploy_id=v1_deploy_id,
        timeout_seconds=_VERSION_POLL_TIMEOUT_SECONDS,
    )
    assert final_deploy_id == v1_deploy_id, (
        "After auto-rollback, /version still reports a different deploy_id.",
        f"expected v1 deploy_id={v1_deploy_id!r}",
        f"observed deploy_id={final_deploy_id!r}",
    )

    # And the rolled-back v1 connector must NOT be honoring the
    # broken-healthcheck env var (it's part of v2's Modal Secret bundle,
    # not v1's). A 200 here proves v1 is the version actually serving
    # traffic, not just a label flip.
    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        liveness = client.get(f"{connector_url}/health/liveness")
    assert liveness.status_code == 200, (
        "Rolled-back v1 connector returned non-200 on /health/liveness; "
        "rollback may not have actually flipped the live version.",
        liveness.status_code,
        liveness.text[:500],
    )

    # ephemeral_env fixture teardown destroys the env unconditionally.


def _poll_for_deploy_id(*, connector_url: str, expected_deploy_id: str, timeout_seconds: float) -> str:
    """Poll ``/version`` until ``deploy_id`` matches ``expected_deploy_id`` (or the budget elapses).

    Returns the most recently observed ``deploy_id`` either way so the
    caller's equality assertion surfaces a meaningful failure mode.
    """
    deadline = time.monotonic() + timeout_seconds
    last_seen = ""
    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        while time.monotonic() < deadline:
            resp = client.get(f"{connector_url}/version")
            if resp.status_code == 200:
                last_seen = resp.json().get("deploy_id", "")
                if last_seen == expected_deploy_id:
                    return last_seen
            time.sleep(_VERSION_POLL_INTERVAL_SECONDS)
    return last_seen
