"""Tests for claude_shim.sh.

The shim is installed as an executable named ``claude`` on the agent's PATH. It
exists to strip ``MAIN_CLAUDE_SESSION_ID`` from nested ``claude`` invocations
(so mngr's readiness hooks, all guarded on that variable, no-op for child
sessions) and then exec the real ``claude`` binary with the original args.

Each test lays out a small bin directory tree:
  - <shim_dir>/claude    -- the shim under test
  - <real_dir>/claude    -- a stub standing in for the real binary
and invokes the shim with PATH="<shim_dir>:<real_dir>".
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
from pathlib import Path

_SHIM_SOURCE = importlib.resources.files("imbue.mngr_claude.resources").joinpath("claude_shim.sh").read_text()


def _install_shim(shim_dir: Path) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "claude"
    shim.write_text(_SHIM_SOURCE)
    shim.chmod(0o755)
    return shim


def _install_stub_real_claude(real_dir: Path, body: str) -> Path:
    real_dir.mkdir(parents=True, exist_ok=True)
    stub = real_dir / "claude"
    stub.write_text("#!/bin/bash\n" + body)
    stub.chmod(0o755)
    return stub


def _run_shim(shim: Path, real_dir: Path, args: list[str], main_sid: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": f"{shim.parent}:{real_dir}:{os.environ.get('PATH', '')}"}
    if main_sid is not None:
        env["MAIN_CLAUDE_SESSION_ID"] = main_sid
    return subprocess.run([str(shim), *args], env=env, capture_output=True, text=True, timeout=30)


def test_shim_unsets_main_claude_session_id_for_nested_invocation(tmp_path: Path) -> None:
    """The shim must scrub MAIN_CLAUDE_SESSION_ID before exec-ing the real binary."""
    shim = _install_shim(tmp_path / "shim_bin")
    real_dir = tmp_path / "real_bin"
    _install_stub_real_claude(real_dir, 'printf "sid=%s\\n" "${MAIN_CLAUDE_SESSION_ID:-UNSET}"\n')

    result = _run_shim(shim, real_dir, [], main_sid="should-be-removed")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sid=UNSET", (
        f"Expected MAIN_CLAUDE_SESSION_ID to be scrubbed, got stdout={result.stdout!r}"
    )


def test_shim_forwards_args_to_real_claude(tmp_path: Path) -> None:
    """The shim must exec the real binary with the original argv intact."""
    shim = _install_shim(tmp_path / "shim_bin")
    real_dir = tmp_path / "real_bin"
    # Print each arg on its own line so we can compare exactly (incl. spaces).
    _install_stub_real_claude(real_dir, 'for a in "$@"; do printf "%s\\n" "$a"; done\n')

    result = _run_shim(shim, real_dir, ["--print", "hello world", "-p"], main_sid="x")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["--print", "hello world", "-p"]


def test_shim_errors_when_real_claude_is_absent(tmp_path: Path) -> None:
    """When no real claude is on PATH (after dropping the shim dir), fail loudly.

    The PATH still needs `bash`/`env` for the shebang to launch, so we provide a
    tools dir with just those (symlinked from the host) and deliberately no
    `claude` -- exercising the not-found branch rather than a launch failure.
    """
    shim = _install_shim(tmp_path / "shim_bin")
    tools_dir = tmp_path / "tools_bin"
    tools_dir.mkdir()
    for tool in ("bash", "env"):
        host_tool = shutil.which(tool)
        assert host_tool is not None, f"{tool} must be available to run the shim"
        os.symlink(host_tool, tools_dir / tool)

    result = subprocess.run(
        [str(shim)],
        env={"PATH": f"{shim.parent}:{tools_dir}"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 127, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "could not find the real 'claude'" in result.stderr
