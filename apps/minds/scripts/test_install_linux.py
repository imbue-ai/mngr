"""Integration tests for how ``install-linux.sh`` reports a step that failed.

The script runs top to bottom with no seams, so these run it for real with a
few system commands replaced by stubs on ``PATH``: enough to get it past the
preflight on any machine and make one chosen command fail.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Final

APP_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
INSTALL_LINUX_PATH: Final[Path] = APP_ROOT / "scripts" / "install-linux.sh"

SYSTEM_PACKAGES_STEP: Final[str] = "System packages (base tools + Electron runtime libraries)"
DISK_SPACE_STEP: Final[str] = "Free disk space"
FAILED_STEP_LINE_TAIL: Final[str] = (
    "failed (its output is above). Fix what it reports, then re-run this script -- "
    "steps that are already done are skipped."
)

# A Linux box with every apt package already installed, as a normal user. The
# script also reads /proc/version to detect WSL; it is absent off Linux, and
# names no Microsoft kernel on a CI runner, so both take the plain-Linux path.
_LINUX_STUBS: Final[dict[str, str]] = {
    "id": 'case "$1" in -u) echo 1000 ;; -un) echo tester ;; *) echo tester ;; esac',
    "uname": 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; *) echo 6.8.0-generic ;; esac',
    "apt-get": "exit 0",
    "dpkg-query": "printf 'install ok installed'",
    # macOS's realpath has no -m; the script is only ever given an absolute path here.
    "realpath": 'for last in "$@"; do :; done; echo "$last"',
}


def _write_stubs(bin_dir: Path, stubs: dict[str, str]) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in stubs.items():
        stub_path = bin_dir / name
        stub_path.write_text(f"#!/bin/sh\n{body}\n")
        stub_path.chmod(0o755)


def _run_install(tmp_path: Path, stubs: dict[str, str], *flags: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _write_stubs(bin_dir, {**_LINUX_STUBS, **stubs})
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(INSTALL_LINUX_PATH), "--install-dir", str(tmp_path / "mngr"), "--no-launch", *flags],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_failing_command_names_its_step_once_after_the_tools_own_output(tmp_path: Path) -> None:
    result = _run_install(
        tmp_path,
        {"df": "echo 'df: cannot read table of mounted file systems' >&2; exit 1"},
        "--non-interactive",
        "--skip-docker",
    )

    assert result.returncode == 1
    tool_output_at = result.stderr.index("df: cannot read table of mounted file systems")
    failure_line = f"error: {DISK_SPACE_STEP} {FAILED_STEP_LINE_TAIL}"
    assert result.stderr.count(failure_line) == 1
    assert result.stderr.index(failure_line) > tool_output_at


def test_a_privileged_command_that_fails_inside_a_helper_names_its_step(tmp_path: Path) -> None:
    # dpkg-query reporting nothing installed sends the packages through
    # run_privileged, whose sudo then fails inside that function.
    result = _run_install(
        tmp_path,
        {
            "dpkg-query": "exit 1",
            "sudo": "echo 'sudo: a password is required' >&2; exit 1",
        },
        "--yes",
        "--skip-docker",
    )

    assert result.returncode == 1
    assert "sudo: a password is required" in result.stderr
    assert result.stderr.count(f"error: {SYSTEM_PACKAGES_STEP} {FAILED_STEP_LINE_TAIL}") == 1


def test_the_scripts_own_refusal_prints_only_its_own_error(tmp_path: Path) -> None:
    result = _run_install(tmp_path, {"uname": "echo Darwin"}, "--non-interactive")

    assert result.returncode == 1
    assert "error: this script sets up Imbue Studio on Linux" in result.stderr
    assert FAILED_STEP_LINE_TAIL not in result.stderr
