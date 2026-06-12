import os
import pty
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
import tomlkit
from loguru import logger

from imbue.mngr.api.connect import CONNECT_COMMAND_ACTIVE_ENV_VAR
from imbue.mngr.config.consts import PROFILES_DIRNAME
from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.config.data_types import USER_ID_FILENAME
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_modal.backend import MODAL_NAME_MAX_LENGTH
from imbue.mngr_modal.backend import truncate_modal_name
from imbue.skitwright.data_types import CommandResult
from imbue.skitwright.data_types import OutputLine
from imbue.skitwright.data_types import OutputSource
from imbue.skitwright.runner import run_command
from imbue.skitwright.session import Session

# Launches its single argument as a shell command under a pseudo-terminal, so
# that a tmux-attaching command (``mngr connect``/``conn``) gets a real terminal
# and attaches instead of failing with "open terminal failed: not a terminal".
# Run via ``python -c`` in a background subprocess by ``run_connecting_command``.
_PTY_CONNECT_LAUNCHER = "import pty, sys; raise SystemExit(pty.spawn(['sh', '-c', sys.argv[1]]))"


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

    def collect_remote_diagnostics(self, agent_name: str) -> str:
        """Collect diagnostic info from a remote agent for debugging failures.

        Captures tmux sessions, Claude Code pane content, session_started file
        status, and running processes. Returns a formatted string suitable for
        inclusion in assertion messages.
        """
        diag_parts = [f"Diagnostics from remote agent '{agent_name}':"]
        for diag_cmd, label in [
            (f"mngr exec {agent_name} 'tmux list-sessions 2>&1'", "tmux sessions"),
            (
                f'mngr exec {agent_name} \'SESSION=$(tmux list-sessions -F "#{{session_name}}" 2>/dev/null | head -1);'
                f' tmux capture-pane -p -t "=$SESSION" 2>&1 || echo no-pane\'',
                "claude pane",
            ),
            (
                # Agent state lives at $MNGR_HOST_DIR/agents/$MNGR_AGENT_ID/ per
                # libs/mngr/docs/conventions.md, and MNGR_HOST_DIR defaults to
                # ~/.mngr (i.e. /root/.mngr on remote hosts where HOME=/root).
                f'mngr exec {agent_name} \'ls -la "$HOME/.mngr/agents"/*/session_started 2>/dev/null'
                " || echo session_started-not-found'",
                "session_started",
            ),
            (
                f"mngr exec {agent_name} 'ps aux | grep -E \"claude|node\" | grep -v grep || echo no-claude-process'",
                "processes",
            ),
        ]:
            # Best-effort: never let a diagnostic subprocess failure mask the
            # primary test assertion that triggered this call. Narrowed to
            # OSError (covers Popen spawn failures like FileNotFoundError /
            # PermissionError) so unrelated programmer errors still propagate.
            try:
                diag = self.run(diag_cmd, comment=f"diagnostic: {label}", timeout=15.0)
                diag_parts.append(f"\n[{label}] stdout: {diag.stdout}\n[{label}] stderr: {diag.stderr}")
            except OSError as exc:
                diag_parts.append(f"\n[{label}] error: {exc!r}")
        return "\n".join(diag_parts)

    def _has_tmux_client(self, session_target: str) -> bool:
        """Return True if a tmux client is attached to the given session target."""
        result = subprocess.run(
            ["tmux", "list-clients", "-t", session_target],
            env=self._env,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def run_connect_interactively(
        self,
        command: str,
        agent_name: str,
        timeout: float = 30.0,
        comment: str | None = None,
    ) -> CommandResult:
        """Run an interactive ``mngr connect``-style command under a PTY and detach.

        ``mngr connect`` execs ``tmux attach`` for a local agent, which requires a
        real terminal and blocks until the client detaches. The plain pipe-based
        :meth:`run` therefore cannot exercise it -- ``tmux attach`` aborts with
        "open terminal failed: not a terminal". This helper instead:

          1. spawns the command with its stdio wired to a pseudo-terminal,
          2. waits until a tmux client has attached to the agent's session,
          3. detaches that client from outside via ``tmux detach-client``,

        after which ``tmux attach`` exits 0 and the command returns cleanly. The
        captured terminal output and exit code are recorded in the transcript and
        returned as a :class:`CommandResult`, exactly like :meth:`run`.

        Only valid for local agents (remote agents connect over SSH, not a local
        tmux attach). The session name is derived as ``{MNGR_PREFIX}{agent_name}``,
        matching :func:`connect_to_agent`.
        """
        session_name = f"{self._env.get('MNGR_PREFIX', '')}{agent_name}"
        # Leading "=" forces tmux exact-session matching (same rule the connect
        # code uses), so we never target a different session by prefix.
        session_target = f"={session_name}"

        env = dict(self._env)
        # The fixture sets a global ``connect_command`` (the no-op asciinema
        # recorder) under ``[commands.connect]`` so that the plain pipe-based
        # ``run`` can exercise ``mngr conn`` without a real tmux attach. That
        # override would also intercept the standalone connect here, leaving no
        # client to poll for. Setting MNGR_CONNECT_COMMAND_ACTIVE makes
        # ``resolve_connect_command`` fall back to the builtin attach (the same
        # mechanism a connect_command uses when it re-invokes mngr), so this
        # helper exercises the real ``tmux attach`` it is designed to detach.
        env[CONNECT_COMMAND_ACTIVE_ENV_VAR] = "1"
        # mngr refuses to attach when it detects it is already inside a tmux
        # session (the nested-tmux guard). When the test runner itself runs under
        # tmux, ``$TMUX``/``$TMUX_PANE`` leak in and trip that guard, so the
        # builtin attach errors out and no client ever appears. Drop them so the
        # connect attaches to the fixture's isolated tmux server (selected via
        # ``TMUX_TMPDIR``); the teardown clears ``$TMUX`` for the same reason.
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=str(self._cwd),
            start_new_session=True,
        )
        # The child holds its own copy of the slave; close ours so that reading
        # the master sees EOF once the child exits.
        os.close(slave_fd)

        captured = bytearray()

        def _read_chunk() -> bytes:
            # Returns b"" on EOF (child closed the PTY) or on the EIO that Linux
            # raises when the last writer of a PTY exits -- both mean "done".
            try:
                return os.read(master_fd, 4096)
            except OSError:
                return b""

        def _drain() -> None:
            chunk = _read_chunk()
            while chunk:
                captured.extend(chunk)
                chunk = _read_chunk()

        reader = threading.Thread(target=_drain)
        reader.start()

        # Wait until the connect process has attached a client to the session,
        # then detach it so `tmux attach` (and thus the connect command) exits 0.
        attached = poll_until(
            condition=lambda: self._has_tmux_client(session_target),
            timeout=timeout,
            poll_interval=0.2,
        )
        if attached:
            subprocess.run(
                ["tmux", "detach-client", "-s", session_name],
                env=self._env,
                capture_output=True,
            )

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

        reader.join()
        os.close(master_fd)

        # A PTY merges stdout/stderr into one terminal stream and uses CRLF line
        # endings; normalize so transcript lines are clean.
        text = captured.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        output_lines = tuple(OutputLine(source=OutputSource.STDOUT, text=line) for line in text.split("\n") if line)
        exit_code = 124 if timed_out else (proc.returncode if proc.returncode is not None else -1)
        result = CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=text,
            stderr="",
            output_lines=output_lines,
        )
        self._transcript.record(result, comment=comment)
        return result

    def write_tutorial_block(self, block: str) -> None:
        """Write the original tutorial script block to the test output directory.

        The block text is dedented and stripped so that Python-indented
        triple-quoted strings produce clean output without leading whitespace.
        """
        cleaned = textwrap.dedent(block).strip() + "\n"
        (self.output_dir / "tutorial_block.txt").write_text(cleaned)

    def run_connecting_command(
        self,
        command: str,
        agent_name: str,
        comment: str | None = None,
        timeout: float = 30.0,
    ) -> CommandResult:
        """Run an interactive ``mngr connect``/``conn`` command and verify it attaches.

        ``mngr connect`` replaces itself with ``tmux attach``, which blocks until
        the user detaches and requires a real terminal, so it cannot be exercised
        with :meth:`run` (that either hangs when stdin is a tty or fails with
        "open terminal failed: not a terminal" when it is not).

        Instead this launches ``command`` under a pseudo-terminal in a background
        subprocess (so the tmux attach succeeds), then polls ``tmux list-clients``
        until a client is attached to the agent's session -- the observable effect
        of a successful connect. The background process is then terminated, which
        detaches the client while leaving the agent's tmux session running.

        Returns a synthetic :class:`CommandResult` (exit code 0 if a client
        attached within ``timeout``, else 124) that is also recorded in the
        transcript, so callers can assert with ``expect(...).to_succeed()``.
        """
        session_name = f"{self._env.get('MNGR_PREFIX', '')}{agent_name}"
        socket_path = Path(self._env["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / "default"

        proc = subprocess.Popen(
            [sys.executable, "-c", _PTY_CONNECT_LAUNCHER, command],
            env=self._env,
            cwd=str(self._cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            process_group=0,
        )
        try:
            attached = poll_until(
                condition=lambda: _is_client_attached(socket_path, session_name),
                timeout=timeout,
                poll_interval=0.5,
            )
        finally:
            _terminate_process_group(proc)

        detail = (
            f"client attached to tmux session '{session_name}'"
            if attached
            else f"no client attached to tmux session '{session_name}' within {timeout}s"
        )
        result = CommandResult(
            command=command,
            exit_code=0 if attached else 124,
            stdout=detail + "\n",
            stderr="",
            output_lines=(OutputLine(source=OutputSource.STDOUT, text=detail),),
        )
        self._transcript.record(result, comment=comment)
        return result


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
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
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


def _is_client_attached(socket_path: Path, session_name: str) -> bool:
    """Return True if a tmux client is attached to the given session.

    ``tmux list-clients -t =<session>`` lists only clients attached to that exact
    session, so any non-empty output means a client is attached. Returns False
    while the server is still starting (non-zero exit) or no client has attached.
    """
    completed = subprocess.run(
        ["tmux", "-S", str(socket_path), "list-clients", "-t", f"={session_name}"],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and completed.stdout.strip() != ""


def _terminate_process_group(proc: "subprocess.Popen[bytes]") -> None:
    """Terminate a background process group, escalating to SIGKILL if needed.

    The connect command is launched with ``process_group=0`` so the whole tree
    (python -> sh -> mngr -> tmux attach) can be torn down at once. Killing it
    detaches the tmux client without stopping the agent's tmux session.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


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
        logger.warning(
            "{} asciinema process(es) did not terminate within {}s: {}",
            len(still_alive),
            _ASCIINEMA_SHUTDOWN_TIMEOUT_SECONDS,
            still_alive,
        )

    # Clean up pid files -- they are only useful while asciinema is running
    for pid_file in test_output_dir.glob("*.pid"):
        pid_file.unlink(missing_ok=True)


def _setup_test_profile(host_dir: Path) -> str:
    """Create a mngr profile in the test's host directory.

    Sets up config.toml, profile directory, user_id, and tmux_onboarding_shown
    so that the subprocess mngr uses a predictable profile with a user_id that
    follows the mngr_test-YYYY-MM-DD-HH-MM-SS convention (parseable by the
    Modal environment cleanup script).

    Returns the user_id that was written.
    """
    profile_id = uuid4().hex
    profile_dir = host_dir / PROFILES_DIRNAME / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Write config.toml pointing to this profile
    config_path = host_dir / ROOT_CONFIG_FILENAME
    config_path.write_text(f'profile = "{profile_id}"\n')

    # Opt this profile's config into pytest runs. The subprocess mngr inherits
    # PYTEST_CURRENT_TEST and loads this profile's settings.toml, so without
    # is_allowed_in_pytest = true (it defaults to False) the config loader would
    # refuse to run.
    (profile_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n")

    # Build a user_id that produces a Modal environment name matching the
    # mngr_test-YYYY-MM-DD-HH-MM-SS-{identifier} pattern (recognized by
    # cleanup_old_modal_test_environments).
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    identifier = os.environ.get("MNGR_AGENT_NAME") or uuid4().hex[:8]
    user_id = f"{timestamp}-{identifier}"
    # Write without trailing newline (matching the format used by get_or_create_user_id)
    user_id_path = profile_dir / USER_ID_FILENAME
    user_id_path.write_text(user_id)

    # Suppress tmux onboarding screen in test transcripts
    (profile_dir / "tmux_onboarding_shown").write_text("")

    return user_id


def _delete_modal_environment(environment_name: str, env: dict[str, str], cwd: Path) -> None:
    """Delete the Modal environment for this test."""
    logger.info("Deleting Modal environment: {}", environment_name)
    try:
        result = run_command(
            f"uv run modal environment delete {shlex.quote(environment_name)} --yes",
            env=env,
            cwd=cwd,
            timeout=30.0,
        )
        if result.exit_code != 0:
            logger.warning("Failed to delete Modal environment {}: {}", environment_name, result.stderr.strip())
        else:
            logger.info("Deleted Modal environment: {}", environment_name)
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Error deleting Modal environment {}: {}", environment_name, exc)


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


# Resolve the real home directory at import time, before any test fixture
# monkeypatches HOME to an isolated temp directory.
_REAL_HOME = Path.home()


def _load_modal_credentials(env: dict[str, str]) -> None:
    """Load Modal credentials from ~/.modal.toml into the env dict.

    Mirrors the logic in mngr_modal's conftest, which uses monkeypatch for
    in-process tests. E2e subprocesses need the vars set explicitly since
    monkeypatch doesn't propagate to child processes.
    """
    modal_toml_path = _REAL_HOME / ".modal.toml"
    if not modal_toml_path.exists():
        return
    for value in tomlkit.loads(modal_toml_path.read_text()).values():
        if isinstance(value, dict) and value.get("active", ""):
            env["MODAL_TOKEN_ID"] = value.get("token_id", "")
            env["MODAL_TOKEN_SECRET"] = value.get("token_secret", "")
            break


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

    # Load Modal credentials from ~/.modal.toml if present and not already in
    # env vars. The Modal conftest does this via monkeypatch for in-process
    # tests, but e2e subprocesses need the vars set explicitly.
    if "MODAL_TOKEN_ID" not in env:
        _load_modal_credentials(env)

    env["MNGR_HOST_DIR"] = str(temp_host_dir)
    env["TMUX_TMPDIR"] = str(tmux_tmpdir)
    env["MNGR_TEST_ASCIINEMA_DIR"] = str(test_output_dir)
    env.pop("TMUX", None)
    # e2e tests create fresh Modal environments, so they must deploy the
    # snapshot_and_shutdown function rather than looking up an existing one.
    env.pop("MNGR_MODAL_DISABLE_SNAPSHOT_DEPLOY", None)

    # Use a short fixed prefix so that derived names (e.g. Modal environment
    # names, which are {prefix}{user_id}) stay well under provider length
    # limits. Test isolation comes from MNGR_HOST_DIR, not the prefix.
    # The mngr_test- prefix is required by the Modal backend guard.
    test_prefix = "mngr_test-"
    env["MNGR_PREFIX"] = test_prefix

    # Create the mngr profile proactively so that:
    # 1. The user_id follows the timestamp convention for Modal cleanup
    # 2. The tmux onboarding screen is suppressed in test transcripts
    test_user_id = _setup_test_profile(temp_host_dir)
    # Pre-compute the Modal environment name so create (inside the mngr
    # subprocess) and delete (below) agree without either side re-deriving it.
    test_modal_env_name = truncate_modal_name(f"{test_prefix}{test_user_id}", max_length=MODAL_NAME_MAX_LENGTH)

    # Add the e2e bin directory to PATH so the connect script is available
    env["PATH"] = f"{_BIN_DIR}:{env.get('PATH', '')}"

    # Configure connect_command for create/start.
    # Remote providers (Modal, Docker) are left enabled so that e2e tests
    # exercise the full discovery path. Tests that trigger Modal (via
    # mngr list, mngr destroy --gc, etc.) need @pytest.mark.modal.
    # is_allowed_in_pytest opts this local-layer config into the pytest run.
    # Every config file loaded during a pytest run must opt in individually, and
    # this one is loaded alongside the profile's settings.toml.
    #
    # allow_settings_key_assignment_narrowing opts the harness into the
    # assign-by-default merge semantics (the documented future default). It is
    # required because connect_command lives in this local layer under
    # ``[commands.create]``/``[commands.start]``, while tutorial tests legitimately
    # set other ``commands.create.*`` keys (e.g. provider) at the project scope.
    # Both spellings land in the command's single ``defaults`` map, so the
    # higher-precedence local layer would otherwise be flagged as narrowing the
    # lower-precedence project layer. Opting in keeps connect_command winning
    # (highest precedence) while letting project-scope command settings persist.
    settings_path = project_config_dir / "settings.local.toml"
    # Set a default agent type. `mngr create --type` no longer has a
    # source-coded default (it was dropped in favor of a user-config value that
    # scripts/install.sh writes during installation). Real users get this from
    # the installer; the e2e fixture mirrors that here so tutorial commands that
    # omit --type (e.g. `mngr create my-task --provider modal`) run as written.
    # "claude" matches the historical source default these tests relied on.
    settings_path.write_text(
        "is_allowed_in_pytest = true\n"
        "allow_settings_key_assignment_narrowing = true\n"
        "\n"
        "[commands.create]\n"
        'type = "claude"\n'
        'connect_command = "mngr-e2e-connect"\n'
        "\n"
        "[commands.start]\n"
        'connect_command = "mngr-e2e-connect"\n'
        "\n"
        "[commands.connect]\n"
        'connect_command = "mngr-e2e-connect"\n'
    )

    # NOTE: the project-scope ``settings.toml`` is deliberately NOT seeded here.
    # ``mngr config edit``/``config set`` tutorial tests assert on the genuine
    # first-use behavior, where the project config file does not yet exist (e.g.
    # ``config edit`` creates it from a template, and ``config set`` writes a
    # fresh file). Those tests therefore read the project file back with ``cat``
    # rather than a follow-up ``mngr`` command, since a freshly-created project
    # file does not carry the ``is_allowed_in_pytest`` opt-in. The opt-in for
    # commands that load merged config comes from the profile ``settings.toml``
    # and the project ``settings.local.toml`` seeded above.

    # Ensure .claude/settings.local.json and the per-test project config dir
    # are gitignored. Remote providers (Modal, Docker) need to write Claude
    # hooks to .claude/settings.local.json. The project config dir holds the
    # settings.toml / settings.local.toml that the e2e fixture seeds and that
    # tests further mutate via `mngr config set`. Without these entries, every
    # `mngr create` invocation against this repo would have to pass
    # --no-ensure-clean to satisfy the working-tree-clean check.
    gitignore_path = temp_git_repo / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(f".claude/settings.local.json\n/{project_config_dir.name}/\n")
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
        logger.warning("Test output: {}", test_output_dir)
        logger.warning("Debugging tips: {} (relative to git root)", _DEBUGGING_DOC)

    if keep_env:
        _write_destroy_script(test_output_dir, env, temp_git_repo, tmux_tmpdir)
        logger.info("Environment kept alive. To clean up: {}/destroy-env", test_output_dir)
        logger.info("MNGR_HOST_DIR={}", temp_host_dir)
        logger.info("TMUX_TMPDIR={}", tmux_tmpdir)
        logger.info("CWD={}", temp_git_repo)
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

    # Delete the Modal environment (if one was created)
    _delete_modal_environment(test_modal_env_name, env=env, cwd=temp_git_repo)

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
