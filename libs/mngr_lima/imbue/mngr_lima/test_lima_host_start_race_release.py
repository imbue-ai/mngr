"""Release-tier behavior guard for the Lima host-start readiness race.

Installs Lima + qemu + a non-root test user (from root), then re-enters
``_lima_host_start_race_helper.py`` under that user via ``runuser`` to boot a
real Lima VM and sample its stopped->ready restart.

Why a real Lima: the boot-in-flight fix (``is_limactl_start_in_flight_for_instance``
and the STARTING classification) rests on two Lima behaviors that only exist on a
real VM -- (1) `limactl list` reports ``Running`` before the guest sshd is
reachable, yet the `limactl start` process stays alive across that whole window,
and (2) that process exits only once sshd answers. The helper asserts both.

Cadence: run the lima release tests (``just test <path>``) whenever
``minimum_lima_version`` in ``constants.py`` (or the shipped/installed lima) is
bumped. This test does not run per-PR (``@pytest.mark.release``) and only when
``limactl`` can be installed (``@pytest.mark.lima``).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# `release` keeps it out of the per-PR set; `lima` documents the env dependency.
pytestmark = [pytest.mark.release, pytest.mark.lima]

# Lima version to install in the sandbox. Bump in sync with `MINIMUM_LIMA_VERSION`
# in `libs/mngr_lima/imbue/mngr_lima/constants.py`.
_LIMA_VERSION = "1.0.7"

# Non-root user that drives the actual Lima commands (Lima refuses to run as root).
_LIMA_USER = "mngr-lima-test"

# Total budget: cold VM boot under qemu+TCG (~7-10 min, no KVM in modal sandboxes),
# plus a stop + sampled-restart cycle, plus destroy.
_TEST_TIMEOUT_SECONDS = 2400


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
    subprocess.run(["curl", "-fsSLo", "/tmp/mngr-lima-race-test.tgz", url], check=True, timeout=180)
    subprocess.run(["tar", "-C", "/usr/local", "-xzf", "/tmp/mngr-lima-race-test.tgz"], check=True, timeout=60)


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
    """Make the repo + venv world-readable so _LIMA_USER can run the helper."""
    # Path layout: libs/mngr_lima/imbue/mngr_lima/test_lima_host_start_race_release.py
    #              parents:  [4]     [3]    [2]   [1]            [0]
    # parents[4] is the repo root.
    repo_root = Path(__file__).resolve().parents[4]
    subprocess.run(["chmod", "-R", "o+rX", str(repo_root)], check=True, timeout=120)


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_lima_host_start_readiness_race_release() -> None:
    """Real Lima VM: during a stopped->ready restart, every observed `Running`-but-unreachable sample is caught by the boot-in-flight detector, and `limactl start` exits only once sshd answers."""
    if os.geteuid() != 0:
        pytest.skip("Release test self-installs lima/qemu/users; requires root.")

    _ensure_packages_installed()
    _ensure_lima_installed()
    _ensure_test_user_exists()
    _grant_user_repo_access()

    # Satisfy the @pytest.mark.lima resource-guard: a direct `lima` invocation from the
    # test process (with the wrapper-bearing PATH + tracking env vars intact) touches the
    # guard's tracking file so makereport accepts the mark. The helper subprocess uses
    # `limactl` under `runuser`/`env -i`, so it never reaches the guard itself.
    subprocess.run(["lima", "--help"], check=False, timeout=10, capture_output=True)

    repo_root = Path(__file__).resolve().parents[4]
    venv_python = repo_root / ".venv" / "bin" / "python"
    helper_module = "imbue.mngr_lima._lima_host_start_race_helper"

    if not venv_python.exists():
        pytest.skip(f"venv python not found at {venv_python} (release env not bootstrapped?)")

    # `env -i ...` scrubs the inherited (root-owned, pytest tmp_path-pointed) environment;
    # the helper sets up its own dirs and needs only HOME + PATH.
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
        f"Lima host-start-race helper failed (exit {result.returncode}):\n"
        f"=== STDOUT ===\n{result.stdout}\n"
        f"=== STDERR ===\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"Helper exited non-zero ({result.returncode}) despite OK marker:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
