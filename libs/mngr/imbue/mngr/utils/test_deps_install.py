"""Integration and release tests for system dependency installation.

The integration tests in this file place mock ``brew`` / ``apt-get`` / ``sudo``
binaries on PATH and call ``_install_via_brew`` / ``_install_via_apt``
directly, verifying that they spawn the real subprocess with the exact
command line we expect. Unit tests in ``deps_test.py`` cover only the
*planning* layer (``describe_install_commands``, OS-branch selection,
install-method routing) and never exercise the subprocess call.

The release tests exercise the real package manager end-to-end against a
likely-already-installed package. They are macOS-only (where brew is the
mngr installer's target) and require brew to actually be on PATH.
Release tests do not run in CI; invoke them locally with
``just test-quick "libs/mngr/imbue/mngr/utils/test_deps_install.py -m release"``.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from imbue.mngr.utils.deps import _install_via_apt
from imbue.mngr.utils.deps import _install_via_brew
from imbue.mngr.utils.testing import write_executable_script


def _logging_mock(log_file: Path, *, exit_code: int = 0) -> str:
    """Bash script that logs ``$(basename argv[0]) $*`` then exits with ``exit_code``."""
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        echo "$(basename "$0") $*" >> "{log_file}"
        exit {exit_code}
        """
    )


def _install_mock_bin(bin_dir: Path, name: str, log_file: Path, *, exit_code: int = 0) -> None:
    write_executable_script(bin_dir / name, _logging_mock(log_file, exit_code=exit_code))


# -- _install_via_brew --


@pytest.mark.timeout(30)
def test_install_via_brew_invokes_brew_with_install_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_install_via_brew(["tmux", "jq"])` calls ``brew install tmux jq`` and returns True."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    log_file.touch()
    _install_mock_bin(bin_dir, "brew", log_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    assert _install_via_brew(["tmux", "jq"]) is True

    assert log_file.read_text().strip().splitlines() == ["brew install tmux jq"]


@pytest.mark.timeout(30)
def test_install_via_brew_returns_false_when_brew_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns False when brew exits non-zero (mirrors ``brew install`` failure)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    log_file.touch()
    _install_mock_bin(bin_dir, "brew", log_file, exit_code=1)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    assert _install_via_brew(["nonexistent-package"]) is False
    # Even on failure, brew was invoked: run_process_to_completion raised
    # ProcessError, ConcurrencyGroup.__exit__ wrapped it in a
    # ConcurrencyExceptionGroup, and _install_via_brew swallowed that group.
    assert log_file.read_text().strip().splitlines() == ["brew install nonexistent-package"]


# -- _install_via_apt --


@pytest.mark.timeout(30)
def test_install_via_apt_runs_update_then_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_install_via_apt(["tmux", "jq"])` issues update + install via sudo, in order."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    log_file.touch()
    # Mock sudo as a no-op logger so the test never actually delegates. The
    # shutil.which("apt-get") guard at the top of _install_via_apt requires
    # apt-get to be findable on PATH, so put a stub there too even though
    # mock sudo never invokes it.
    _install_mock_bin(bin_dir, "sudo", log_file)
    _install_mock_bin(bin_dir, "apt-get", log_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    assert _install_via_apt(["tmux", "jq"]) is True

    assert log_file.read_text().strip().splitlines() == [
        "sudo apt-get update -qq",
        "sudo apt-get install -y -qq tmux jq",
    ]


@pytest.mark.timeout(30)
def test_install_via_apt_stops_at_first_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``apt-get update`` fails, ``apt-get install`` is not attempted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "calls.log"
    log_file.touch()
    _install_mock_bin(bin_dir, "sudo", log_file, exit_code=1)
    _install_mock_bin(bin_dir, "apt-get", log_file)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    assert _install_via_apt(["tmux"]) is False
    # Only the first command was attempted: run_process_to_completion raised
    # ProcessError on the failing `update`, so control never reached the
    # second `install` call inside the `with ConcurrencyGroup(...)` block.
    assert log_file.read_text().strip().splitlines() == ["sudo apt-get update -qq"]


# -- Release tests: actual brew invocation --


@pytest.mark.release
@pytest.mark.skipif(sys.platform != "darwin", reason="brew install path is macOS-only")
@pytest.mark.skipif(shutil.which("brew") is None, reason="brew is not installed on this machine")
@pytest.mark.timeout(180)
def test_install_via_brew_against_real_brew() -> None:
    """End-to-end: ``_install_via_brew([name])`` succeeds against real brew.

    Picks the first already-installed formula from ``brew list --formula``
    and runs ``_install_via_brew`` against it. Homebrew is a no-op when
    the package is already present, so this exercises the real subprocess
    plumbing -- ``shutil.which`` lookup, PATH resolution, argv handling,
    exit code propagation, ProcessError / ConcurrencyExceptionGroup flow --
    without mutating the machine.
    """
    listed = subprocess.run(["brew", "list", "--formula"], capture_output=True, timeout=30, text=True)
    assert listed.returncode == 0, (
        f"`brew list --formula` exited {listed.returncode}\nstdout: {listed.stdout}\nstderr: {listed.stderr}"
    )
    formulas = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not formulas:
        pytest.skip("No brew formulas installed on this machine; nothing safe to re-install")

    assert _install_via_brew([formulas[0]]) is True
