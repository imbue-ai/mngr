"""Project-level conftest for minds.

When running tests from apps/minds/, this conftest provides the common pytest hooks
that would otherwise come from the monorepo root conftest.py (which is not discovered
when pytest runs from a subdirectory).

When running from the monorepo root, the root conftest.py registers the hooks first,
and this file's register_conftest_hooks() call is a no-op (guarded by a module-level flag).

Also registers the shared plugin test fixtures so tests get the standard autouse
isolation (HOME, MNGR_HOST_DIR, MNGR_PREFIX, MNGR_ROOT_NAME pointed at per-test temp
values, tmux server isolation). The MNGR_PREFIX the shared fixture picks by default
is `mngr_<hex>-`, which the Modal backend guard rejects (it only accepts underscore-
prefixed env names beginning with `mngr_test-`); minds tests spawn real mngr
subprocesses that may create Modal envs, so the `mngr_test_prefix` fixture is
overridden here to produce the `mngr_test-YYYY-MM-DD-HH-MM-SS-` format that the
backend guard AND the CI cleanup script (cleanup_old_modal_test_environments.py)
both recognize.
"""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from loguru import logger
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.helpers import create_verified_user_via_admin_api
from imbue.minds.deployment_tests.helpers import delete_user_via_admin_api
from imbue.minds.testing import SYNC_E2E_CONNECTOR_URL_ENV
from imbue.minds.testing import SYNC_E2E_LITELLM_URL_ENV
from imbue.minds.testing import SYNC_E2E_SUPERTOKENS_API_KEY_ENV
from imbue.minds.testing import SYNC_E2E_SUPERTOKENS_URI_ENV
from imbue.minds.testing import SyncE2EAccount
from imbue.minds.testing import SyncE2EEnv
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_short_random_string

# Point ``MINDS_RESTIC_BINARY`` at the bundled ``resources/restic/restic``
# binary so restic_cli tests don't require a system-wide restic install.
# Mirrors what Electron's backend.js does at runtime: a Minds end user --
# or a dev running tests -- should never have to ``brew install restic``.
# Run unconditionally before any test module is imported; restic_cli.py
# reads the env var lazily, so a late-setting fixture would also work,
# but doing it here is one line and matches how the runtime flows.
#
# If ``resources/restic/restic`` doesn't exist (``pnpm build`` hasn't run),
# leave the env var unset -- ensure_restic_available() then raises a
# message that points at the right fix.
_BUNDLED_RESTIC = Path(__file__).parent / "resources" / "restic" / "restic"
if _BUNDLED_RESTIC.exists() and "MINDS_RESTIC_BINARY" not in os.environ:
    os.environ["MINDS_RESTIC_BINARY"] = str(_BUNDLED_RESTIC)

suppress_warnings()
register_marker(
    "minds_deployment: tests that exercise the minds deploy process itself by minting their own "
    "ephemeral CI env. Driven by `just minds-test-deployment`; never collected by the standard "
    "CI test runs or `just test-quick`."
)
register_marker(
    "minds_services: tests that exercise the deployed services of a pre-stood-up shared CI env. "
    "Driven by `just minds-test-deployment`; never collected by the standard CI test runs or "
    "`just test-quick`."
)
register_marker(
    "minds_snapshot_resume: tests that run in a sandbox booted from a Modal snapshot produced by "
    "`scripts/snapshot_minds_e2e_state.py` (a stopped DEFAULT_WORKSPACE_TEMPLATE workspace Docker container already on "
    "disk, plus a warm Electron/Playwright/Xvfb/Docker toolchain). Includes both the resume "
    "sanity checks (against the baked workspace) and the Electron-driven create+chat test (which "
    "drives the warm toolchain to create a fresh workspace). Run only via "
    "`just test-offload-minds-snapshot <image-id>` -- explicitly excluded from every other "
    "offload run because they need the snapshot's pre-baked state."
)
register_conftest_hooks(globals())
register_plugin_test_fixtures(globals())


@pytest.fixture
def mngr_test_prefix() -> str:
    """Override the shared mngr_test_prefix to use `mngr_test-YYYY-MM-DD-HH-MM-SS-`.

    The shared fixture defaults to `mngr_<hex>-`, which the Modal backend guards
    reject when used to create a Modal env under pytest. Minds tests spawn real
    mngr subprocesses that can create Modal envs, so the prefix needs to match
    the timestamped format the guards AND the CI cleanup script recognize.

    Why an autouse (via the shared setup_test_mngr_env) instead of a per-call
    subprocess env=... override like other plugins use: the desktop client spawns
    mngr via `ConcurrencyGroup.run_process_to_completion()` with no env= argument,
    so the subprocess inherits os.environ. The only seam for injecting the right
    prefix into that subprocess is os.environ, which the autouse fixture
    (setup_test_mngr_env -> monkeypatch.setenv("MNGR_PREFIX", ...)) already owns.
    Overriding mngr_test_prefix here makes the autouse put the correct value in
    os.environ for the whole test, covering every mngr subprocess the desktop
    client spawns uniformly.
    """
    return f"{generate_test_environment_name()}-"


_SNAPSHOT_START_DOCKERD_SCRIPT = Path("/code/mngr/libs/mngr/imbue/mngr/resources/start-dockerd.sh")
_SNAPSHOT_DOCKERD_STARTUP_TIMEOUT_SECONDS = 180


@pytest.fixture(scope="session")
def snapshot_sandbox_dockerd() -> None:
    """Bring ``dockerd`` back up in a snapshot-resumed sandbox.

    ``sandbox.snapshot_filesystem`` only captures the disk, not running
    processes -- so a sandbox booted from the minds-workspace snapshot has
    ``/var/lib/docker`` populated (with the stopped workspace container's
    image layers + on-disk state) but no ``dockerd`` running. Re-run the same
    script the snapshot itself used to bring dockerd up, so ``docker``
    invocations in the requesting tests can talk to the daemon.

    Shared by ``test_snapshot_resume.py`` (via its module-autouse wrapper) and
    ``test_sync_e2e.py`` (requested explicitly). Mirrors
    ``_ensure_dockerd_for_release`` in ``libs/mngr/imbue/mngr/conftest.py``
    but for the snapshot's in-tree script location.
    """
    docker_info = subprocess.run(["docker", "info"], capture_output=True)
    if docker_info.returncode == 0:
        return

    if not _SNAPSHOT_START_DOCKERD_SCRIPT.is_file():
        raise FileNotFoundError(
            f"start-dockerd.sh not found at {_SNAPSHOT_START_DOCKERD_SCRIPT}; this fixture is "
            "only useful inside a sandbox booted from scripts/snapshot_minds_e2e_state.py."
        )

    subprocess.run(["chmod", "+x", str(_SNAPSHOT_START_DOCKERD_SCRIPT)], check=True, timeout=5)
    subprocess.run(
        [str(_SNAPSHOT_START_DOCKERD_SCRIPT)],
        check=True,
        timeout=_SNAPSHOT_DOCKERD_STARTUP_TIMEOUT_SECONDS,
    )


class _XvfbStartupError(RuntimeError):
    """Raised when the Xvfb display server fails to start for an Electron test."""


@pytest.fixture(scope="session")
def xvfb_display() -> Iterator[str]:
    """Provide an X display for Electron tests, starting Xvfb if none is set.

    Two contexts:
    - Local runs go through ``xvfb-run -a`` (the ``just minds-test-electron``
      recipe), which already sets ``DISPLAY``; this fixture then just yields it.
    - The snapshot offload stage runs ``uv run pytest`` directly (no
      ``xvfb-run`` wrapper) in a sandbox booted from the snapshot image, which
      has the ``Xvfb`` binary baked in but no running display server. There this
      fixture starts ``Xvfb`` and points ``DISPLAY`` at it so the Electron
      renderer has somewhere to draw.

    Uses Xvfb's ``-displayfd`` so there is no poll/sleep: Xvfb picks a free
    display itself and writes its number to the pipe once the server is ready
    (and EOFs if it dies first), so a single blocking read both waits for
    readiness and learns the display number.
    """
    existing = os.environ.get("DISPLAY")
    if existing:
        yield existing
        return

    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        # Close the parent's copy so the read end EOFs if Xvfb dies before writing.
        os.close(write_fd)

    with os.fdopen(read_fd, "r") as ready_reader:
        display_number = ready_reader.readline().strip()
    if not display_number:
        process.terminate()
        raise _XvfbStartupError("Xvfb exited before reporting a display number; cannot run the Electron test")

    display = f":{display_number}"
    os.environ["DISPLAY"] = display
    try:
        yield display
    finally:
        os.environ.pop("DISPLAY", None)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def sync_e2e_env() -> SyncE2EEnv:
    """The real connector env the workspace-sync e2e tests target, or skip.

    The coordinates are forwarded into the snapshot offload sandbox only on
    ``run_minds_release_tests`` CI runs (and can be exported by an operator
    pointing at a dev env for local iteration); on every other run the vars
    are absent and the sync e2e tests skip.
    """
    values: dict[str, str] = {}
    for env_var in (
        SYNC_E2E_CONNECTOR_URL_ENV,
        SYNC_E2E_LITELLM_URL_ENV,
        SYNC_E2E_SUPERTOKENS_URI_ENV,
        SYNC_E2E_SUPERTOKENS_API_KEY_ENV,
    ):
        value = os.environ.get(env_var)
        if not value:
            pytest.skip(f"{env_var} is not set; the sync e2e tests need a real connector env")
        values[env_var] = value
    return SyncE2EEnv(
        connector_url=values[SYNC_E2E_CONNECTOR_URL_ENV],
        litellm_proxy_url=values[SYNC_E2E_LITELLM_URL_ENV],
        supertokens_connection_uri=SecretStr(values[SYNC_E2E_SUPERTOKENS_URI_ENV]),
        supertokens_api_key=SecretStr(values[SYNC_E2E_SUPERTOKENS_API_KEY_ENV]),
    )


@pytest.fixture
def sync_e2e_account(sync_e2e_env: SyncE2EEnv) -> Iterator[SyncE2EAccount]:
    """A unique, pre-verified, paid account on the sync e2e env; deleted on teardown.

    The address lives under ``imbue.com`` because the ci/dev deploy tiers seed
    that domain into ``paid_domains`` -- imbue-cloud backups (R2 bucket
    provisioning) are paid-gated, and these tests exercise them for real. The
    account is provisioned through the SuperTokens admin API (setup machinery,
    not part of the user journey under test); the tests then sign in through
    the real UI with the returned email + password.
    """
    email = f"sync-e2e-{get_short_random_string()}@imbue.com"
    password = SecretStr(f"pw-{uuid4().hex}")
    user_id, access_token = create_verified_user_via_admin_api(
        connection_uri=sync_e2e_env.supertokens_connection_uri,
        api_key=sync_e2e_env.supertokens_api_key,
        connector_url=AnyUrl(sync_e2e_env.connector_url),
        email=NonEmptyStr(email),
        password=password,
    )
    yield SyncE2EAccount(email=email, password=password, user_id=str(user_id), access_token=access_token)
    try:
        delete_user_via_admin_api(
            connection_uri=sync_e2e_env.supertokens_connection_uri,
            api_key=sync_e2e_env.supertokens_api_key,
            user_id=NonEmptyStr(str(user_id)),
        )
    except httpx.HTTPError as e:
        logger.warning("Could not delete sync e2e account {}: {}", email, e)
