"""Fast in-app ``mngr`` CLI invocation via a pre-warmed, single-use server process.

Spawning a fresh ``mngr`` subprocess pays a large fixed cost on every call: a
brand-new Python interpreter plus importing ``imbue.mngr.main`` and every
enabled plugin (measured at roughly 1.3-3.3s depending on filesystem cache
warmth).

:class:`MngrCaller` avoids paying that cost on the request path by keeping a
single "warm" ``mngr`` process running ahead of time. A warm process is a fresh
Python interpreter (started via ``python -m imbue.minds.utils.mngr_caller``)
that has already imported ``imbue.mngr.main`` and is sitting on a Unix-domain
socket waiting for one request. When :meth:`MngrCaller.call` runs it claims the
waiting warm process, hands it the argv over the socket, reads back
stdout/stderr/exit-code, and the warm process then exits. As soon as a warm
process is claimed, a replacement is spawned so the next call again finds one
ready.

This deliberately avoids the ``multiprocessing`` forkserver's fork-without-exec
model, which is unreliable on macOS. Each warm process is a clean, freshly
execed interpreter, so there is no inherited process-global state to worry
about, and mngr's CLI-time mutations (loguru reconfiguration, ``sys.argv``,
``setproctitle``, ``sys.stdout``/``sys.stderr``) happen in a throwaway process
that never touches the long-lived backend.

The warm-server entry point lives in this same module (guarded by
``if __name__ == "__main__"``) so it stays encapsulated alongside
:class:`MngrCaller`, and so ``python -m imbue.minds.utils.mngr_caller`` warms by
importing ``imbue.mngr.main`` once, off the request path.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import traceback
from collections.abc import Mapping
from collections.abc import Sequence
from multiprocessing.connection import Client
from multiprocessing.connection import Connection
from multiprocessing.connection import Listener
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final

import click
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.polling import poll_for_value

# Module path of the warm-server entry point, launched as ``python -m <module>``.
_WARM_SERVER_MODULE: Final[str] = "imbue.minds.utils.mngr_caller"

_DEFAULT_CALL_TIMEOUT_SECONDS: Final[float] = 60.0

# How long to wait for a freshly spawned warm process to start listening on its
# socket. This must comfortably exceed mngr's import cost (and any contention),
# since a cold call spawns the warm process and immediately connects to it.
_DEFAULT_WARM_STARTUP_TIMEOUT_SECONDS: Final[float] = 120.0

# How often to retry connecting while a warm process is still starting up.
_CONNECT_POLL_INTERVAL_SECONDS: Final[float] = 0.02

# When terminating a claimed/replaced warm process, give it this long to die
# before escalating to SIGKILL.
_TERMINATE_FORCE_KILL_SECONDS: Final[float] = 5.0

# Sentinel returncode used when a call is terminated for exceeding its timeout.
_TIMEOUT_RETURNCODE: Final[int] = -1

# AF_UNIX socket paths are limited to ~104 bytes, so keep names short.
_SOCKET_DIR_PREFIX: Final[str] = "mngrc-"


class MngrCallerError(Exception):
    """Base error for the in-app ``mngr`` CLI caller."""

    ...


class WarmProcessStartupError(MngrCallerError):
    """Raised when a warm ``mngr`` process never started listening on its socket."""

    ...


class MngrCallResult(MutableModel):
    """Outcome of one ``mngr`` CLI invocation run inside a warm process."""

    returncode: int = Field(description="Process-style exit code; 0 means success.")
    stdout: str = Field(default="", description="Captured stdout (mngr writes JSONL/human output here).")
    stderr: str = Field(default="", description="Captured stderr (mngr writes log lines here).")
    is_timed_out: bool = Field(
        default=False, description="True if the call exceeded its timeout and the warm process was terminated."
    )


def _coerce_exit_code(code: object) -> int:
    """Map a click/SystemExit code (int, ``None``, or str) to a process-style int."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    # A string exit message conventionally means failure.
    return 1


def _execute_mngr_cli(
    cli: click.Command,
    argv: tuple[str, ...],
    env_overrides: Mapping[str, str],
) -> tuple[int, str, str]:
    """Run ``mngr <argv>`` in this (throwaway) warm process and capture its output.

    All of mngr's global-state mutation (loguru, ``sys.argv``, stdout/stderr) is
    confined to this process, which exits right after, so it never affects the
    minds backend.
    """
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    returncode = 0
    os.environ.update(env_overrides)
    sys.argv = ["mngr", *argv]
    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
        try:
            return_value = cli.main(args=list(argv), prog_name="mngr", standalone_mode=False)
            returncode = _coerce_exit_code(return_value)
        except SystemExit as exc:
            returncode = _coerce_exit_code(exc.code)
        except click.exceptions.Abort:
            returncode = 1
        except click.ClickException as exc:
            exc.show()
            returncode = exc.exit_code
        except MngrError:
            # mngr already emitted a structured error (jsonl) / logged it to the
            # captured buffers; surface it as a failed command. Genuinely
            # unexpected exceptions are left to propagate: the process then
            # exits, and the caller observes the closed socket (EOF) and returns
            # failure.
            stderr_buffer.write(traceback.format_exc())
            returncode = 1
        # Flush any loguru records that were enqueued to async sinks so they land
        # in the captured stderr buffer before we read it.
        logger.complete()
    return returncode, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def _run_warm_mngr_server(socket_path: Path) -> None:
    """Warm-process entry point: import mngr, then serve exactly one CLI request.

    Imports ``imbue.mngr.main`` eagerly (this is the warm-up) before binding the
    socket, so by the time a client can connect the interpreter is fully warm.
    Serves a single request and then returns, so each warm process is single-use.
    """
    # This inline import is the whole point of the warm process: it pays mngr's
    # multi-second import cost here (in a throwaway interpreter), off the minds
    # backend's request path. It is intentionally allow-listed by the
    # inline-imports ratchet.
    from imbue.mngr.main import cli

    listener = Listener(address=str(socket_path), family="AF_UNIX", authkey=None)
    try:
        connection = listener.accept()
        try:
            argv, env_overrides = connection.recv()
            returncode, stdout, stderr = _execute_mngr_cli(cli, argv, env_overrides)
            connection.send((returncode, stdout, stderr))
        finally:
            connection.close()
    finally:
        listener.close()


class _WarmMngrProcess(MutableModel):
    """A single, pre-warmed, single-use ``mngr`` server process and its socket."""

    socket_path: Path = Field(description="AF_UNIX socket the warm process listens on.")
    running_process: RunningProcess = Field(description="The spawned warm-server subprocess.")

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def connect(self, startup_timeout_seconds: float) -> Connection:
        """Connect to the warm process, waiting for it to start listening.

        Polls until the socket accepts a connection or ``startup_timeout_seconds``
        elapses. Raises :class:`WarmProcessStartupError` if the warm process exits
        before it starts listening or the deadline passes.
        """
        connection, _, _ = poll_for_value(
            self._try_connect,
            timeout=startup_timeout_seconds,
            poll_interval=_CONNECT_POLL_INTERVAL_SECONDS,
        )
        if connection is None:
            raise WarmProcessStartupError(
                f"mngr warm process did not start listening within {startup_timeout_seconds:.0f}s"
            )
        return connection

    def _try_connect(self) -> Connection | None:
        """Attempt one connection; return None while the warm process is still starting up."""
        if self.running_process.is_finished():
            raise WarmProcessStartupError(
                f"mngr warm process exited before listening (exit {self.running_process.returncode}); "
                f"stderr: {self.running_process.read_stderr()[:1024]}"
            )
        try:
            return Client(address=str(self.socket_path), family="AF_UNIX", authkey=None)
        except (FileNotFoundError, ConnectionRefusedError):
            return None

    def terminate(self) -> None:
        """Terminate the warm process (a no-op if it has already exited)."""
        try:
            self.running_process.terminate(force_kill_seconds=_TERMINATE_FORCE_KILL_SECONDS)
        except TimeoutExpired:
            logger.warning("Timed out force-killing warm mngr process on socket {}", self.socket_path)


class MngrCaller(MutableModel):
    """Runs ``mngr`` CLI commands by handing them to pre-warmed, single-use processes.

    A single instance should be shared process-wide; use
    :func:`get_default_mngr_caller` to obtain the shared instance.
    """

    default_timeout_seconds: float = Field(
        default=_DEFAULT_CALL_TIMEOUT_SECONDS,
        description="Timeout applied to a call when none is passed explicitly.",
    )
    warm_startup_timeout_seconds: float = Field(
        default=_DEFAULT_WARM_STARTUP_TIMEOUT_SECONDS,
        description="How long a call waits for a freshly spawned warm process to start listening.",
    )

    # ``ConcurrencyGroup``/``RunningProcess``/locks are not pydantic-native; hold
    # them as private runtime state and allow arbitrary types through.
    _concurrency_group: ConcurrencyGroup | None = PrivateAttr(default=None)
    _owned_concurrency_group: ConcurrencyGroup | None = PrivateAttr(default=None)
    _warm_process: _WarmMngrProcess | None = PrivateAttr(default=None)
    _socket_directory: Path | None = PrivateAttr(default=None)
    _socket_counter: int = PrivateAttr(default=0)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_prewarm_started: bool = PrivateAttr(default=False)

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def prewarm(self, concurrency_group: ConcurrencyGroup) -> None:
        """Spawn the first warm process in the background so the first real call is fast.

        Non-blocking and idempotent: records the concurrency group used to manage
        warm processes and dispatches a tracked thread that spawns the first warm
        process (which pays mngr's import cost off the request path). Intended to
        be invoked once at startup.
        """
        with self._lock:
            if self._is_prewarm_started:
                return
            self._is_prewarm_started = True
            self._concurrency_group = concurrency_group
        concurrency_group.start_new_thread(
            self._ensure_warm_process_exists,
            name="mngr-caller-prewarm",
            is_checked=False,
            # Best-effort warmup: if spawning fails, the first real call will
            # simply spawn (and wait for) its own warm process.
            on_failure=lambda exc: logger.opt(exception=True).error("mngr warm-process pre-warm failed: {}", exc),
        )

    def _get_concurrency_group(self) -> ConcurrencyGroup:
        """Return the concurrency group used to spawn warm processes.

        Uses the group recorded by :meth:`prewarm` when present; otherwise lazily
        creates and enters an owned group (torn down by :meth:`stop`). The owned
        group keeps standalone use (e.g. ``MngrCaller().call(...)``) self-managing.
        """
        with self._lock:
            if self._concurrency_group is not None:
                return self._concurrency_group
            if self._owned_concurrency_group is None:
                owned_group = ConcurrencyGroup(name="mngr-caller")
                owned_group.__enter__()
                self._owned_concurrency_group = owned_group
            return self._owned_concurrency_group

    def _allocate_socket_path(self) -> Path:
        """Return a fresh, short AF_UNIX socket path inside this caller's temp directory."""
        with self._lock:
            if self._socket_directory is None:
                self._socket_directory = Path(tempfile.mkdtemp(prefix=_SOCKET_DIR_PREFIX))
            self._socket_counter += 1
            return self._socket_directory / f"w{self._socket_counter}.sock"

    def _spawn_warm_process(self) -> _WarmMngrProcess:
        """Launch a fresh warm ``mngr`` server process listening on a new socket."""
        socket_path = self._allocate_socket_path()
        command = [sys.executable, "-m", _WARM_SERVER_MODULE, str(socket_path)]
        # The warm process exits on its own (after one request) or is terminated;
        # its non-zero exit on termination is expected, so it is not group-checked.
        running_process = self._get_concurrency_group().run_process_in_background(command, is_checked_by_group=False)
        return _WarmMngrProcess(socket_path=socket_path, running_process=running_process)

    def _store_or_terminate_warm_process(self, warm_process: _WarmMngrProcess) -> None:
        """Keep ``warm_process`` as the idle one, or terminate it if one already exists."""
        process_to_terminate: _WarmMngrProcess | None = None
        with self._lock:
            if self._warm_process is None:
                self._warm_process = warm_process
            else:
                process_to_terminate = warm_process
        if process_to_terminate is not None:
            process_to_terminate.terminate()

    def _ensure_warm_process_exists(self) -> None:
        """Spawn an idle warm process if none is currently waiting."""
        with self._lock:
            if self._warm_process is not None:
                return
        self._store_or_terminate_warm_process(self._spawn_warm_process())

    def _claim_warm_process(self) -> _WarmMngrProcess:
        """Take the idle warm process for use, then spawn its replacement.

        If no warm process is waiting yet (a cold call), spawn one to use now. In
        both cases a fresh replacement is spawned so the next call finds one ready.
        """
        with self._lock:
            claimed_process = self._warm_process
            self._warm_process = None
        if claimed_process is None:
            claimed_process = self._spawn_warm_process()
        self._store_or_terminate_warm_process(self._spawn_warm_process())
        return claimed_process

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> MngrCallResult:
        """Run ``mngr <argv>`` in a pre-warmed process and return its result.

        ``argv`` is the argument vector *after* the ``mngr`` program name (e.g.
        ``["message", "-m", "hi", "--", "agent"]``). ``env_overrides`` are applied
        to the warm process's ``os.environ`` before the CLI runs. On timeout the
        warm process is terminated and a result with ``is_timed_out=True`` and a
        non-zero ``returncode`` is returned.
        """
        resolved_timeout = self.default_timeout_seconds if timeout is None else timeout
        warm_process = self._claim_warm_process()
        request = (tuple(argv), dict(env_overrides or {}))
        # Always reap the claimed warm process (normally it exits on its own after
        # responding; on a startup/exec failure or a hang it must be terminated).
        try:
            try:
                connection = warm_process.connect(self.warm_startup_timeout_seconds)
            except WarmProcessStartupError as exc:
                return MngrCallResult(returncode=1, stderr=str(exc))
            try:
                connection.send(request)
                if connection.poll(resolved_timeout):
                    try:
                        returncode, stdout, stderr = connection.recv()
                        return MngrCallResult(returncode=returncode, stdout=stdout, stderr=stderr)
                    except EOFError:
                        return MngrCallResult(
                            returncode=1,
                            stderr="mngr warm process exited without returning a result",
                        )
                return MngrCallResult(
                    returncode=_TIMEOUT_RETURNCODE,
                    is_timed_out=True,
                    stderr=f"mngr {' '.join(argv)} timed out after {resolved_timeout:.0f}s",
                )
            finally:
                connection.close()
        finally:
            warm_process.terminate()

    def stop(self) -> None:
        """Terminate the idle warm process and release all resources.

        Safe to call when nothing was started. Production relies on the
        concurrency group passed to :meth:`prewarm` for shutdown cleanup; this is
        primarily for standalone/test use of an owned concurrency group.
        """
        with self._lock:
            idle_process = self._warm_process
            self._warm_process = None
            owned_group = self._owned_concurrency_group
            self._owned_concurrency_group = None
            socket_directory = self._socket_directory
            self._socket_directory = None
        if idle_process is not None:
            idle_process.terminate()
        if owned_group is not None:
            owned_group.__exit__(None, None, None)
        if socket_directory is not None:
            shutil.rmtree(socket_directory, ignore_errors=True)


_DEFAULT_CALLER_HOLDER: dict[str, MngrCaller | None] = {"caller": None}
_DEFAULT_CALLER_LOCK = threading.Lock()


def get_default_mngr_caller() -> MngrCaller:
    """Return the shared, process-wide :class:`MngrCaller` singleton.

    Constructing it is cheap and does not spawn any warm process; call
    :meth:`MngrCaller.prewarm` (once, at startup) to spawn the first warm process
    ahead of the first real invocation.
    """
    with _DEFAULT_CALLER_LOCK:
        if _DEFAULT_CALLER_HOLDER["caller"] is None:
            _DEFAULT_CALLER_HOLDER["caller"] = MngrCaller()
        return _DEFAULT_CALLER_HOLDER["caller"]


def _main() -> None:
    socket_path = Path(sys.argv[1])
    _run_warm_mngr_server(socket_path)


if __name__ == "__main__":
    _main()
