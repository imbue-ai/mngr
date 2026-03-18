import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mng.utils.testing import get_short_random_string
from imbue.skitwright.runner import run_command
from imbue.skitwright.session import Session

_TRANSCRIPT_OUTPUT_DIR = Path(__file__).resolve().parent / ".test_output" / "transcripts"


def _is_keep_on_failure() -> bool:
    return os.environ.get("MNG_E2E_KEEP_ON_FAILURE", "").lower() in ("1", "true", "yes")


_e2e_test_failed: dict[str, bool] = {}


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, None, None]:
    """Track whether the test call phase failed, for use in e2e fixture teardown."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        _e2e_test_failed[item.nodeid] = True


@pytest.fixture
def e2e(
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    temp_git_repo: Path,
    project_config_dir: Path,
    request: pytest.FixtureRequest,
) -> Generator[Session, None, None]:
    """Provide an isolated skitwright Session for running mng CLI commands.

    Sets up a subprocess environment with:
    - Isolated MNG_HOST_DIR, MNG_PREFIX, MNG_ROOT_NAME (from parent fixtures)
    - Isolated TMUX_TMPDIR (own tmux server, separate from the one the parent
      autouse fixture creates for the in-process test environment)
    - A temporary git repo as the working directory
    - Disabled remote providers (Modal, Docker) via settings.local.toml

    The transcript is saved to .test_output/transcripts/ after each test.
    """
    # Create a separate tmux tmpdir for subprocess-spawned tmux sessions.
    # The parent autouse fixture isolates the in-process tmux server, but
    # subprocesses need their own isolation since they inherit env vars.
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="mng-e2e-tmux-", dir="/tmp"))

    # Build subprocess environment from the current (already-isolated) env
    env = os.environ.copy()
    env["MNG_HOST_DIR"] = str(temp_host_dir)
    env["MNG_PREFIX"] = mng_test_prefix
    env["MNG_ROOT_NAME"] = mng_test_root_name
    env["TMUX_TMPDIR"] = str(tmux_tmpdir)
    env.pop("TMUX", None)

    # Disable remote providers so tests don't attempt Modal/Docker operations
    settings_path = project_config_dir / "settings.local.toml"
    settings_path.write_text("[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n")

    session = Session(env=env, cwd=temp_git_repo)

    yield session

    # Save transcript
    _TRANSCRIPT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    test_name = request.node.name
    transcript_path = _TRANSCRIPT_OUTPUT_DIR / f"{test_name}.txt"
    transcript_path.write_text(session.transcript)

    # Detect test failure
    test_failed = _e2e_test_failed.pop(request.node.nodeid, False)

    if test_failed:
        sys.stderr.write(f"\n  Transcript saved to: {transcript_path}\n")

    if test_failed and _is_keep_on_failure():
        sys.stderr.write("\n  MNG_E2E_KEEP_ON_FAILURE is set: agents and tmux session kept running.\n")
        sys.stderr.write(f"  TMUX_TMPDIR={tmux_tmpdir}\n")
        sys.stderr.write(f"  MNG_HOST_DIR={temp_host_dir}\n")
        return

    # Destroy all agents before killing tmux
    run_command(
        "mng destroy --all --force",
        env=env,
        cwd=temp_git_repo,
        timeout=30.0,
    )

    # Kill the isolated tmux server
    tmux_tmpdir_str = str(tmux_tmpdir)
    assert tmux_tmpdir_str.startswith("/tmp/mng-e2e-tmux-")
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


@pytest.fixture
def agent_name() -> str:
    """Return a unique agent name for use in e2e tests."""
    return f"e2e-{get_short_random_string()}"
