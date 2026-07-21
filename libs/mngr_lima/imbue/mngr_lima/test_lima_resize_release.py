"""End-to-end release test for Lima host CPU/memory resizing.

Installs Lima + qemu + a non-root test user (from root), then re-enters
``_lima_resize_release_helper.py`` under that user via ``runuser`` to drive
``LimaProviderInstance`` through create / resize / stop+start / verify /
destroy on a real Lima VM.

Why a real Lima: the unit tests in ``instance_test.py`` cover record
persistence and request merging, but two behaviours only fire on a real VM:

1. ``start_host`` must apply the recorded values via ``limactl edit`` while
   the VM is stopped, so the restarted VM boots with exactly the configured
   values (closing the configured/actual discrepancy).
2. Create-time resource recording probes the booted values from
   ``limactl list --json``, which must agree byte-exactly with what the
   resize surface later reports.

Runs only in release CI (``@pytest.mark.release``) and only when ``limactl``
can be installed (``@pytest.mark.lima``), so it never gates per-PR merges.
The helper can also be run directly as a non-root user on a developer machine
(``uv run python -m imbue.mngr_lima._lima_resize_release_helper``).
"""

import os
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_lima.test_lima_btrfs_release import _LIMA_USER
from imbue.mngr_lima.test_lima_btrfs_release import _ensure_lima_installed
from imbue.mngr_lima.test_lima_btrfs_release import _ensure_packages_installed
from imbue.mngr_lima.test_lima_btrfs_release import _ensure_test_user_exists
from imbue.mngr_lima.test_lima_btrfs_release import _grant_user_repo_access

pytestmark = [pytest.mark.release, pytest.mark.lima]

# Cold VM boot under qemu+TCG runs ~7-10 min (no KVM in modal sandboxes),
# plus a stop/start cycle, plus destroy.
_TEST_TIMEOUT_SECONDS = 1800


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_lima_resize_end_to_end_release() -> None:
    """A real Lima VM's CPU/memory resize persists, survives stop/start, and boots applied."""
    if os.geteuid() != 0:
        pytest.skip("Release test self-installs lima/qemu/users; requires root.")

    _ensure_packages_installed()
    _ensure_lima_installed()
    _ensure_test_user_exists()
    _grant_user_repo_access()

    # Satisfy the @pytest.mark.lima resource-guard (see the identical block in
    # test_lima_btrfs_release.py for the full explanation): the helper runs
    # under `runuser` with a scrubbed env, so a direct guarded `lima` call from
    # the test process is what touches the guard's tracking file.
    subprocess.run(["lima", "--help"], check=False, timeout=10, capture_output=True)

    # Path layout: libs/mngr_lima/imbue/mngr_lima/test_lima_resize_release.py
    #              parents:  [4]     [3]    [2]   [1]            [0]
    # parents[4] is the repo root.
    repo_root = Path(__file__).resolve().parents[4]
    venv_python = repo_root / ".venv" / "bin" / "python"
    helper_module = "imbue.mngr_lima._lima_resize_release_helper"

    if not venv_python.exists():
        pytest.skip(f"venv python not found at {venv_python} (release env not bootstrapped?)")

    result = subprocess.run(
        [
            "runuser",
            "-u",
            _LIMA_USER,
            "--",
            "env",
            "-i",
            f"HOME=/home/{_LIMA_USER}",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            str(venv_python),
            "-m",
            helper_module,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_TEST_TIMEOUT_SECONDS - 60,
    )

    assert "HELPER_RESULT: OK" in result.stdout, (
        f"Lima resize release helper failed (exit {result.returncode}):\n"
        f"=== STDOUT ===\n{result.stdout}\n"
        f"=== STDERR ===\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"Helper exited non-zero ({result.returncode}) despite OK marker:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
