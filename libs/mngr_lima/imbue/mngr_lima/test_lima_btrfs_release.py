"""End-to-end release test for the Lima btrfs host-data volume mode.

Installs Lima + qemu + a non-root test user (from root), then re-enters
``_lima_btrfs_release_helper.py`` under that user via ``runuser`` to drive
``LimaProviderInstance`` through a full create / verify / stop+start /
destroy cycle on a real Lima VM.

Why a real Lima: the unit tests in ``lima_yaml_test.py`` and
``instance_test.py`` cover YAML shape and record persistence, but the
btrfs additional-disk path has two production-only behaviours that only
fire on a real VM:

1. ``limactl disk create`` must run before ``limactl start`` -- Lima's
   ``additionalDisks: format: true`` only formats an *existing* disk
   record.
2. The provisioning script's ``chmod 0777`` + ``ln -sfn`` block has to
   land while Lima's auto-mount for the additional disk is in place;
   the symlink target is Lima's hardcoded ``/mnt/lima-<disk_name>``.

Both were found by manual lima-playground testing before this test was
written; the test exists to keep them working. It runs only in release
CI (``@pytest.mark.release``) and only when ``limactl`` can be installed
(``@pytest.mark.lima``), so it never gates per-PR merges.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# `release` keeps it out of the per-PR set; `lima` documents the env
# dependency and lets us scope future lima-specific local runs.
pytestmark = [pytest.mark.release, pytest.mark.lima]

# Lima version that supports `additionalDisks` with `format: true` and
# `fsType: btrfs`. Bump in sync with `MINIMUM_LIMA_VERSION` in
# `libs/mngr_lima/imbue/mngr_lima/constants.py` (the field-level minimum).
_LIMA_VERSION = "1.0.7"

# Non-root user that drives the actual Lima commands. Lima refuses to run
# as root, so the test does the apt-install + user-create as root, then
# re-enters the helper script under this user via `runuser`.
_LIMA_USER = "mngr-lima-test"

# Total release-test budget: cold VM boot under qemu+TCG runs ~7-10 min
# (no KVM in modal sandboxes), plus stop/start cycle, plus destroy.
_TEST_TIMEOUT_SECONDS = 1800


def _ensure_packages_installed() -> None:
    """Install qemu-system-x86 + sudo via apt if missing (idempotent)."""
    have_qemu = shutil.which("qemu-system-x86_64") is not None
    have_sudo = shutil.which("sudo") is not None
    if have_qemu and have_sudo:
        return
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    subprocess.run(["apt-get", "update", "-qq"], check=True, timeout=120, env=env)
    pkgs = []
    if not have_qemu:
        pkgs.extend(["qemu-system-x86", "qemu-utils"])
    if not have_sudo:
        pkgs.append("sudo")
    subprocess.run(["apt-get", "install", "-y", "-qq", *pkgs], check=True, timeout=600, env=env)


def _ensure_lima_installed() -> None:
    """Download + install the Lima static binary tarball if `limactl` is missing."""
    if shutil.which("limactl"):
        return
    arch = os.uname().machine
    lima_arch = "x86_64" if arch == "x86_64" else arch
    url = (
        f"https://github.com/lima-vm/lima/releases/download/"
        f"v{_LIMA_VERSION}/lima-{_LIMA_VERSION}-Linux-{lima_arch}.tar.gz"
    )
    subprocess.run(["curl", "-fsSLo", "/tmp/mngr-lima-release-test.tgz", url], check=True, timeout=180)
    subprocess.run(["tar", "-C", "/usr/local", "-xzf", "/tmp/mngr-lima-release-test.tgz"], check=True, timeout=60)


def _ensure_test_user_exists() -> None:
    """Create _LIMA_USER (with passwordless sudo) if missing."""
    id_result = subprocess.run(["id", _LIMA_USER], capture_output=True, text=True, timeout=10)
    if id_result.returncode == 0:
        return
    subprocess.run(["useradd", "-m", "-s", "/bin/bash", _LIMA_USER], check=True, timeout=30)
    sudoers_dir = Path("/etc/sudoers.d")
    sudoers_dir.mkdir(exist_ok=True)
    sudoers_file = sudoers_dir / _LIMA_USER
    sudoers_file.write_text(f"{_LIMA_USER} ALL=(ALL) NOPASSWD: ALL\n")
    sudoers_file.chmod(0o0440)


def _grant_user_repo_access() -> None:
    """Make the repo + venv world-readable so _LIMA_USER can run the helper.

    Modal-offload sandboxes check out the repo under /code/mngr owned by
    root; the venv at /code/mngr/.venv is similarly root-owned. We chmod
    o+rX everything the helper needs to import + execute. (`chown -R` to
    the test user would also work but is heavier and would race other
    tests that share the sandbox.)
    """
    # Path layout: libs/mngr_lima/imbue/mngr_lima/test_lima_btrfs_release.py
    #              parents:  [4]     [3]    [2]   [1]            [0]
    # parents[4] is the repo root.
    repo_root = Path(__file__).resolve().parents[4]
    subprocess.run(["chmod", "-R", "o+rX", str(repo_root)], check=True, timeout=120)


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_lima_btrfs_host_end_to_end_release() -> None:
    """Real Lima VM with `is_host_data_volume_exposed=False` boots, `/mngr` is btrfs+writable, data survives stop/start, destroy reclaims the disk."""
    if os.geteuid() != 0:
        pytest.skip("Release test self-installs lima/qemu/users; requires root.")

    _ensure_packages_installed()
    _ensure_lima_installed()
    _ensure_test_user_exists()
    _grant_user_repo_access()

    # Satisfy the @pytest.mark.lima resource-guard. The guard wraps the
    # `lima` binary on PATH (see libs/mngr_lima/.../register_guards.py:
    # register_resource_guard("lima")) and tracks invocations that go
    # through that wrapper. Our helper subprocess uses `limactl` (not
    # `lima`) and runs under `runuser` with `env -i`, so neither the
    # wrapper nor the guard's `_PYTEST_GUARD_*` tracking env vars are
    # visible to the helper. A direct `lima` invocation from the test
    # process (with the parent pytest env intact, including the
    # wrapper-bearing PATH and tracking env vars) is what touches the
    # guard's tracking file so makereport accepts the mark.
    # `lima --help` is used instead of `lima --version` because `lima`
    # (without an instance argument) requires the help text; the
    # important thing is that the wrapper is invoked.
    subprocess.run(["lima", "--help"], check=False, timeout=10, capture_output=True)

    # Path layout: libs/mngr_lima/imbue/mngr_lima/test_lima_btrfs_release.py
    #              parents:  [4]     [3]    [2]   [1]            [0]
    # parents[4] is the repo root.
    repo_root = Path(__file__).resolve().parents[4]
    venv_python = repo_root / ".venv" / "bin" / "python"
    helper_module = "imbue.mngr_lima._lima_btrfs_release_helper"

    if not venv_python.exists():
        pytest.skip(f"venv python not found at {venv_python} (release env not bootstrapped?)")

    # `env -i ...` scrubs the inherited environment. The pytest harness
    # sets MNGR_HOST_DIR / MNGR_ROOT_NAME / TMPDIR pointed at the
    # per-test pytest tmp_path (root-owned), which the non-root helper
    # cannot read. The helper sets up its own host_dir + profile_dir
    # under a fresh `tempfile.TemporaryDirectory()`, so it only needs
    # HOME (for ~ expansion) and PATH (to find limactl + qemu) on the
    # child env.
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
        f"Lima btrfs release helper failed (exit {result.returncode}):\n"
        f"=== STDOUT ===\n{result.stdout}\n"
        f"=== STDERR ===\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"Helper exited non-zero ({result.returncode}) despite OK marker:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
