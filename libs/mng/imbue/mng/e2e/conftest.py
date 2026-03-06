import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.session import Session

_REPO_ROOT = Path(__file__).resolve().parents[5]

_TRANSCRIPT_OUTPUT_DIR = Path(__file__).resolve().parent / ".test_output" / "transcripts"


@pytest.fixture
def e2e(
    tmp_path: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    temp_git_repo: Path,
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
    config_dir = temp_git_repo / f".{mng_test_root_name}"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.local.toml"
    settings_path.write_text("[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n")

    session = Session(env=env, cwd=temp_git_repo)

    yield session

    # Kill the isolated tmux server
    tmux_tmpdir_str = str(tmux_tmpdir)
    assert tmux_tmpdir_str.startswith("/tmp/mng-e2e-tmux-")
    socket_path = tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    kill_env = os.environ.copy()
    kill_env.pop("TMUX", None)
    kill_env["TMUX_TMPDIR"] = tmux_tmpdir_str
    subprocess.run(
        ["tmux", "-S", str(socket_path), "kill-server"],
        capture_output=True,
        env=kill_env,
    )
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)

    # Save transcript
    _TRANSCRIPT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    test_name = request.node.name
    transcript_path = _TRANSCRIPT_OUTPUT_DIR / f"{test_name}.txt"
    transcript_path.write_text(session.transcript)


def _mng(args: str) -> str:
    """Build a 'uv run mng ...' command string."""
    return f"uv run --project {_REPO_ROOT} mng {args}"


@pytest.fixture
def mng(e2e: Session) -> "MngRunner":
    """Provide a helper for running mng commands through the e2e session."""
    return MngRunner(e2e)


class MngRunner:
    """Convenience wrapper that prefixes commands with 'uv run --project <root> mng'."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def run(self, args: str, timeout: float = 30.0) -> CommandResult:
        """Run 'uv run mng <args>' and return the result."""
        return self._session.run(_mng(args), timeout=timeout)

    @property
    def session(self) -> Session:
        return self._session
