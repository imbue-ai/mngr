import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import init_git_repo_with_config
from imbue.skitwright.runner import run_command
from imbue.skitwright.session import Session


class E2eSession(Session):
    """Session subclass that adds e2e-specific helpers like tutorial block writing.

    Use the class method `create` instead of constructing directly.
    """

    output_dir: Path

    @classmethod
    def create(cls, env: dict[str, str], cwd: Path, output_dir: Path) -> "E2eSession":
        """Create an E2eSession with the given output directory."""
        session = cls(env=env, cwd=cwd)
        session.output_dir = output_dir
        return session

    def write_tutorial_block(self, block: str) -> None:
        """Write the original tutorial script block to the test output directory.

        The block text is dedented and stripped so that Python-indented
        triple-quoted strings produce clean output without leading whitespace.
        """
        cleaned = textwrap.dedent(block).strip() + "\n"
        (self.output_dir / "tutorial_block.txt").write_text(cleaned)


_E2E_DIR = Path(__file__).resolve().parent
_BIN_DIR = _E2E_DIR / "bin"
_REPO_ROOT = next(p for p in [_E2E_DIR, *_E2E_DIR.parents] if (p / ".git").exists())
_TEST_OUTPUT_DIR = _REPO_ROOT / ".test_output" / "e2e"
_DEBUGGING_DOC = "libs/mngr/imbue/mngr/e2e/DEBUGGING.md"

_ASCIINEMA_SHUTDOWN_TIMEOUT_SECONDS = 5.0


_LEVEL = {"no": 0, "on-failure": 1, "yes": 2}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register e2e-specific command line options."""
    group = parser.getgroup("mngr-e2e", "mngr e2e test options")
    group.addoption(
        "--mngr-e2e-keep-env",
        choices=["yes", "on-failure", "no"],
        default="no",
        help="Keep test environment (agents, tmux) after tests finish. "
        "'yes' = always, 'on-failure' = only when test fails, 'no' = never (default: no)",
    )
    group.addoption(
        "--mngr-e2e-artifacts",
        choices=["yes", "on-failure", "no"],
        default="yes",
        help="Save test artifacts (transcript, asciinema recordings, tutorial block). "
        "'yes' = always (default), 'on-failure' = only when test fails, 'no' = never",
    )
    group.addoption(
        "--mngr-e2e-run-name",
        default=None,
        help="Override the auto-generated timestamp directory name for test output. "
        "When provided, output goes to .test_output/e2e/<run_name>/ instead of a timestamp.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Validate that --mngr-e2e-artifacts is at least as broad as --mngr-e2e-keep-env."""
    keep = config.getoption("--mngr-e2e-keep-env", default="no")
    artifacts = config.getoption("--mngr-e2e-artifacts", default="yes")
    if _LEVEL[artifacts] < _LEVEL[keep]:
        raise pytest.UsageError(
            f"--mngr-e2e-artifacts={artifacts} cannot be lower than --mngr-e2e-keep-env={keep}. "
            f"Keeping the environment requires saving artifacts (for the destroy-env script)."
        )


def _should_keep_env(config: pytest.Config, test_failed: bool) -> bool:
    """Determine whether to keep the test environment based on the CLI flag."""
    value = config.getoption("--mngr-e2e-keep-env", default="no")
    if value == "yes":
        return True
    if value == "on-failure":
        return test_failed
    return False


def _should_save_artifacts(config: pytest.Config, test_failed: bool) -> bool:
    """Determine whether to save test artifacts based on the CLI flag."""
    value = config.getoption("--mngr-e2e-artifacts", default="yes")
    if value == "yes":
        return True
    if value == "on-failure":
        return test_failed
    return False


_e2e_test_failed: dict[str, bool] = {}


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, None, None]:
    """Track whether the test call phase failed, for use in e2e fixture teardown."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        _e2e_test_failed[item.nodeid] = True


@pytest.fixture(scope="session")
def e2e_run_dir(request: pytest.FixtureRequest) -> Path:
    """Create a named or timestamped directory for this test run's output."""
    run_name = request.config.getoption("--mngr-e2e-run-name", default=None)
    if run_name is None:
        run_name = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = _TEST_OUTPUT_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _read_asciinema_pids(test_output_dir: Path) -> list[int]:
    """Read all asciinema PIDs from .pid files in the given directory."""
    pids: list[int] = []
    for pid_file in test_output_dir.glob("*.pid"):
        try:
            pids.append(int(pid_file.read_text().strip()))
        except (ValueError, OSError):
            pass
    return pids


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _stop_asciinema_processes(test_output_dir: Path) -> None:
    """Send SIGINT to all asciinema processes and wait for them to terminate."""
    pids = _read_asciinema_pids(test_output_dir)
    if not pids:
        return

    # Send SIGINT so asciinema flushes the recording and exits
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass

    # Wait for all processes to terminate
    all_exited = poll_until(
        condition=lambda: not any(_is_pid_alive(pid) for pid in pids),
        timeout=_ASCIINEMA_SHUTDOWN_TIMEOUT_SECONDS,
        poll_interval=0.1,
    )

    if not all_exited:
        still_alive = [pid for pid in pids if _is_pid_alive(pid)]
        sys.stderr.write(
            f"\n  WARNING: {len(still_alive)} asciinema process(es) did not terminate "
            f"within {_ASCIINEMA_SHUTDOWN_TIMEOUT_SECONDS}s: {still_alive}\n"
        )

    # Clean up pid files -- they are only useful while asciinema is running
    for pid_file in test_output_dir.glob("*.pid"):
        pid_file.unlink(missing_ok=True)


def _write_destroy_script(
    test_output_dir: Path,
    env: dict[str, str],
    temp_git_repo: Path,
    tmux_tmpdir: Path,
) -> None:
    """Write a destroy-env script that cleans up the kept test environment."""
    socket_path = tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    script_path = test_output_dir / "destroy-env"
    script_path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f'export MNGR_HOST_DIR="{env["MNGR_HOST_DIR"]}"\n'
        f'export TMUX_TMPDIR="{tmux_tmpdir}"\n'
        "unset TMUX\n"
        "\n"
        'echo "Destroying all agents..."\n'
        f'cd "{temp_git_repo}" && mngr destroy --all --force || true\n'
        "\n"
        'echo "Killing tmux server..."\n'
        f'tmux -S "{socket_path}" kill-server 2>/dev/null || true\n'
        "\n"
        f'echo "Removing tmux tmpdir..."\n'
        f'rm -rf "{tmux_tmpdir}"\n'
        "\n"
        'echo "Environment destroyed."\n'
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def e2e(
    temp_host_dir: Path,
    temp_git_repo: Path,
    project_config_dir: Path,
    e2e_run_dir: Path,
    request: pytest.FixtureRequest,
) -> Generator[E2eSession, None, None]:
    """Provide an isolated E2eSession for running mngr CLI commands.

    Sets up a subprocess environment with:
    - Isolated MNGR_HOST_DIR (from parent fixture; sufficient for full isolation)
    - Isolated TMUX_TMPDIR (own tmux server, separate from the one the parent
      autouse fixture creates for the in-process test environment)
    - A temporary git repo as the working directory
    - Remote providers (Modal, Docker) left enabled for e2e testing
    - A custom connect_command that records tmux sessions via asciinema

    Output is saved to .test_output/e2e/<timestamp>/<test_name>/ (relative to repo root).
    """
    # Create a separate tmux tmpdir for subprocess-spawned tmux sessions.
    # The parent autouse fixture isolates the in-process tmux server, but
    # subprocesses need their own isolation since they inherit env vars.
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="mngr-e2e-tmux-", dir="/tmp"))

    # Set up per-test output directory under the run directory
    test_name = request.node.name
    test_output_dir = e2e_run_dir / test_name
    test_output_dir.mkdir(parents=True, exist_ok=True)

    # Build subprocess environment from the current (already-isolated) env.
    # MNGR_HOST_DIR is the only env var needed for isolation -- it segregates
    # the test's agent data from the host mngr. MNGR_PREFIX and MNGR_ROOT_NAME
    # are already set by the parent autouse fixture and inherited via
    # os.environ.copy().
    env = os.environ.copy()
    env["MNGR_HOST_DIR"] = str(temp_host_dir)
    env["TMUX_TMPDIR"] = str(tmux_tmpdir)
    env["MNGR_TEST_ASCIINEMA_DIR"] = str(test_output_dir)
    env.pop("TMUX", None)

    # Transform the inherited prefix from mngr_{uuid}- to mngr_test-{uuid}-
    # so that Modal environment names (which are {prefix}{user_id}) pass the
    # mngr_test- guard in the Modal backend. This only affects subprocess
    # commands; the in-process prefix remains unchanged for tmux cleanup.
    inherited_prefix = env.get("MNGR_PREFIX", "mngr_")
    env["MNGR_PREFIX"] = inherited_prefix.replace("mngr_", "mngr_test-", 1)

    # Add the e2e bin directory to PATH so the connect script is available
    env["PATH"] = f"{_BIN_DIR}:{env.get('PATH', '')}"

    # Configure connect_command for create/start
    # Remote providers (Modal, Docker) are left enabled so that e2e tests exercise them
    settings_path = project_config_dir / "settings.local.toml"
    settings_path.write_text(
        "[commands.create]\n"
        'connect_command = "mngr-e2e-connect"\n'
        "\n"
        "[commands.start]\n"
        'connect_command = "mngr-e2e-connect"\n'
    )

    # Ensure .claude/settings.local.json is gitignored. Remote providers
    # (Modal, Docker) need to write Claude hooks to this file, and the
    # gitignore check fails if it would appear as an unstaged change.
    gitignore_path = temp_git_repo / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(".claude/settings.local.json\n")
        run_command("git add .gitignore && git commit -m 'Add .gitignore'", env=env, cwd=temp_git_repo, timeout=10.0)

    session = E2eSession.create(env=env, cwd=temp_git_repo, output_dir=test_output_dir)

    yield session

    # Detect test failure
    test_failed = _e2e_test_failed.pop(request.node.nodeid, False)
    config = request.config
    keep_env = _should_keep_env(config, test_failed)
    save_artifacts = _should_save_artifacts(config, test_failed)

    # Save artifacts (transcript, etc.) unless disabled.
    # Always keep the directory if the env is being kept (for the destroy script).
    if save_artifacts or keep_env:
        transcript_path = test_output_dir / "transcript.txt"
        transcript_path.write_text(session.transcript)
    else:
        shutil.rmtree(test_output_dir, ignore_errors=True)

    if test_failed:
        sys.stderr.write(f"\n  Test output: {test_output_dir}\n")
        sys.stderr.write(f"  Debugging tips: {_DEBUGGING_DOC} (relative to git root)\n")

    if keep_env:
        _write_destroy_script(test_output_dir, env, temp_git_repo, tmux_tmpdir)
        sys.stderr.write(f"\n  Environment kept alive. To clean up: {test_output_dir}/destroy-env\n")
        sys.stderr.write(f"  MNGR_HOST_DIR={temp_host_dir}\n")
        sys.stderr.write(f"  TMUX_TMPDIR={tmux_tmpdir}\n")
        sys.stderr.write(f"  CWD={temp_git_repo}\n")
        return

    # Interrupt asciinema recording processes so they flush and exit
    _stop_asciinema_processes(test_output_dir)

    # Destroy all agents before killing tmux
    run_command(
        "mngr destroy --all --force",
        env=env,
        cwd=temp_git_repo,
        timeout=30.0,
    )

    # Kill the isolated tmux server
    tmux_tmpdir_str = str(tmux_tmpdir)
    assert tmux_tmpdir_str.startswith("/tmp/mngr-e2e-tmux-")
    socket_path = tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    kill_env = os.environ.copy()
    kill_env.pop("TMUX", None)
    kill_env["TMUX_TMPDIR"] = tmux_tmpdir_str
    run_command(
        f"tmux -S {socket_path} kill-server",
        env=kill_env,
        cwd=temp_git_repo,
        timeout=10.0,
    )
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)


class MinimalInstallEnv(FrozenModel):
    """A fresh mngr install in an isolated venv, with subprocess env and git repo."""

    venv_dir: Path
    env: dict[str, str]
    repo_dir: Path

    def run_mngr(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run the venv's mngr binary with the given arguments."""
        mngr_bin = str(self.venv_dir / "bin" / "mngr")
        return subprocess.run(
            [mngr_bin, *args],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
            env=self.env,
            timeout=30,
        )

    def run_python(self, script: str) -> subprocess.CompletedProcess[str]:
        """Run a Python script in the isolated venv."""
        python_bin = str(self.venv_dir / "bin" / "python")
        return subprocess.run(
            [python_bin, "-c", script],
            capture_output=True,
            text=True,
            cwd=self.repo_dir,
            env=self.env,
            timeout=30,
        )


@pytest.fixture
def minimal_install_env(
    isolated_mngr_venv: Path,
    temp_host_dir: Path,
    mngr_test_root_name: str,
    tmp_path: Path,
) -> MinimalInstallEnv:
    """Provide a fresh mngr install in an isolated venv for install tests.

    The venv is built from the workspace (not the dev venv), so it exercises
    the real install path: entry points, dependency resolution, etc.

    The subprocess environment is intentionally minimal (not inherited from
    the parent process). PATH contains only the venv bin and the directories
    of mngr's declared system dependencies (from scripts/install.sh: git,
    tmux, jq, curl, ssh, rsync). This catches code that depends on tools from the
    developer's environment (e.g. the modal CLI being on PATH).
    """
    # Build PATH from only the venv and the directories containing mngr's
    # declared system dependencies (from scripts/install.sh). This mirrors
    # what a user would have after running install.sh.
    system_deps = ["git", "tmux", "jq", "curl", "ssh", "rsync"]
    dep_dirs: set[str] = set()
    for dep in system_deps:
        dep_path = shutil.which(dep)
        if dep_path is not None:
            dep_dirs.add(str(Path(dep_path).parent))
    system_path = ":".join(sorted(dep_dirs))

    env = {
        "PATH": f"{isolated_mngr_venv / 'bin'}:{system_path}",
        "HOME": str(temp_host_dir.parent),
        "MNGR_HOST_DIR": str(temp_host_dir),
        "MNGR_ROOT_NAME": mngr_test_root_name,
    }

    repo_dir = tmp_path / "repo"
    init_git_repo_with_config(repo_dir)

    return MinimalInstallEnv(venv_dir=isolated_mngr_venv, env=env, repo_dir=repo_dir)
