"""End-to-end release test for the Lima docker-in-VM (is_host_in_docker) mode.

Installs Lima + qemu + a non-root test user (from root), then re-enters
``_lima_docker_release_helper.py`` under that user via ``runuser`` to drive
``LimaProviderInstance`` (with ``is_host_in_docker=True``) through a full
create / verify / snapshot / stop+start / destroy cycle on a real Lima VM.

Why a real Lima: the unit tests cover the generated YAML and config validation,
but the docker-in-VM path has production-only behaviours that only fire on a
real VM -- root outer SSH, the per-host btrfs subvolume + bind-mounted docker
volume, the snapshot-helper systemd unit + request/result IPC, the
container-as-host SSH over the Lima-forwarded port, and the VM stop/start
relaunch of the container.

The helper isolates its own ``LIMA_HOME`` and writes a Lima ``override.yaml``
(legacy BIOS + qemu64) so it runs both where UEFI firmware is missing and where
KVM is unavailable. It runs only in release CI (``@pytest.mark.release``) and
only when ``limactl`` can be installed (``@pytest.mark.lima``), so it never
gates per-PR merges.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.release, pytest.mark.lima]

# Lima version that supports `additionalDisks` with `format: true` and
# `fsType: btrfs` (same floor as the btrfs release test).
_LIMA_VERSION = "1.0.7"

# Non-root user that drives the actual Lima commands. Lima refuses to run as
# root, so the test does the apt-install + user-create as root, then re-enters
# the helper script under this user via `runuser`.
_LIMA_USER = "mngr-lima-test"

# Cold VM boot + in-VM docker install + image pull + snapshot + stop/start.
_TEST_TIMEOUT_SECONDS = 2400

# Isolated Lima home for this test (never touches a developer's real ~/.lima).
_LIMA_HOME = f"/home/{_LIMA_USER}/.lima-mngr-docker-release"

# Lima per-instance override: legacy BIOS (boots where UEFI/OVMF firmware is
# absent) + a generic qemu64 CPU under the qemu driver (boots where KVM is
# unavailable, falling back to TCG). This is the only place the docker-mode
# test can inject VM-level knobs, since is_host_in_docker build_args are docker
# build args rather than Lima YAML.
_LIMA_OVERRIDE_YAML = "vmType: qemu\nfirmware:\n  legacyBIOS: true\ncpuType:\n  x86_64: qemu64\n"


def _write_lima_override() -> None:
    """Create the isolated LIMA_HOME (owned by _LIMA_USER) and drop in override.yaml."""
    config_dir = Path(_LIMA_HOME) / "_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "override.yaml").write_text(_LIMA_OVERRIDE_YAML)
    subprocess.run(["chown", "-R", _LIMA_USER, _LIMA_HOME], check=True, timeout=30)


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
    """Make the repo + venv world-readable so _LIMA_USER can run the helper."""
    repo_root = Path(__file__).resolve().parents[4]
    subprocess.run(["chmod", "-R", "o+rX", str(repo_root)], check=True, timeout=120)


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_lima_docker_host_end_to_end_release() -> None:
    """Real Lima VM with is_host_in_docker=True: agent runs in a debian container, host_dir is btrfs, snapshots work via the helper, data survives a VM stop/start, destroy reclaims everything."""
    if os.geteuid() != 0:
        pytest.skip("Release test self-installs lima/qemu/users; requires root.")

    _ensure_packages_installed()
    _ensure_lima_installed()
    _ensure_test_user_exists()
    _grant_user_repo_access()
    _write_lima_override()

    # Touch the `lima` resource-guard wrapper from the parent pytest env so the
    # @pytest.mark.lima guard is satisfied (see the btrfs release test for the
    # full explanation of why this direct invocation is needed).
    subprocess.run(["lima", "--help"], check=False, timeout=10, capture_output=True)

    repo_root = Path(__file__).resolve().parents[4]
    venv_python = repo_root / ".venv" / "bin" / "python"
    helper_module = "imbue.mngr_lima._lima_docker_release_helper"

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
            # Isolate Lima state (instances, disks, the override.yaml written
            # above) so the test never touches a developer's real ~/.lima.
            f"LIMA_HOME={_LIMA_HOME}",
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
        f"Lima docker release helper failed (exit {result.returncode}):\n"
        f"=== STDOUT ===\n{result.stdout}\n"
        f"=== STDERR ===\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"Helper exited non-zero ({result.returncode}) despite OK marker:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
