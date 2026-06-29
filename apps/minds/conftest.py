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
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import generate_test_environment_name

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
    "`scripts/snapshot_minds_e2e_state.py` (a stopped FCT workspace Docker container already on "
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
    os.environ for the whole test, covering both the desktop client's in-process
    spawn AND clean_env()-based subprocess calls uniformly.
    """
    return f"{generate_test_environment_name()}-"


_XVFB_DISPLAY: Final[str] = ":99"
_XVFB_READY_TIMEOUT_SECONDS: Final[int] = 30


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
    """
    existing = os.environ.get("DISPLAY")
    if existing:
        yield existing
        return

    process = subprocess.Popen(
        ["Xvfb", _XVFB_DISPLAY, "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    socket_path = Path(f"/tmp/.X11-unix/X{_XVFB_DISPLAY.lstrip(':')}")
    deadline = time.monotonic() + _XVFB_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if socket_path.exists():
            break
        if process.poll() is not None:
            raise RuntimeError(f"Xvfb exited early with code {process.returncode} before {socket_path} appeared")
        time.sleep(0.2)
    else:
        process.terminate()
        raise TimeoutError(f"Xvfb did not create {socket_path} within {_XVFB_READY_TIMEOUT_SECONDS}s")

    os.environ["DISPLAY"] = _XVFB_DISPLAY
    try:
        yield _XVFB_DISPLAY
    finally:
        os.environ.pop("DISPLAY", None)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
