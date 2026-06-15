"""Fast in-app ``mngr`` CLI invocation via a preloaded forkserver.

Spawning a fresh ``mngr`` subprocess pays a large fixed cost on every call: a
brand-new Python interpreter plus importing ``imbue.mngr.main`` and every
enabled plugin (measured at roughly 1.3-3.3s depending on filesystem cache
warmth).

:class:`MngrCaller` avoids it by running the CLI in a child forked from a
``multiprocessing`` forkserver that has already imported ``imbue.mngr.main``.
The forkserver is pre-warmed once at app startup (:meth:`MngrCaller.prewarm`),
paying the import cost a single time and off the request path. Each subsequent
:meth:`MngrCaller.call` forks a fresh, already-warm process, runs
``mngr <argv>`` in it, and captures stdout/stderr/exit-code.

Running in a forked child (rather than directly in the minds backend process)
also sidesteps the process-global state that ``mngr``'s CLI mutates -- loguru
reconfiguration (``logger.remove()`` + re-add), ``sys.argv``, ``setproctitle``,
and ``sys.stdout``/``sys.stderr`` -- because those mutations happen in a
throwaway process and never touch the long-lived backend.

The child target functions live in this module (not in a test file) so the
forkserver can import them by name when it forks: forkserver children do not
inherit the dynamic ``sys.path`` additions that pytest makes for test modules.
"""

import contextlib
import io
import os
import sys
import threading
import traceback
from collections.abc import Mapping
from collections.abc import Sequence
from multiprocessing.connection import Connection
from multiprocessing.context import ForkServerContext
from typing import Final

import click
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError

# Imported up-front by the forkserver so every forked child starts warm. We do
# NOT import ``imbue.mngr.main`` at this module's top level: that would pay the
# multi-second import cost inside the minds backend process itself, which is
# exactly what we are trying to avoid. The forkserver imports it instead.
_FORKSERVER_PRELOAD_MODULES: Final[tuple[str, ...]] = (
    "imbue.mngr.main",
    "imbue.minds.utils.mngr_caller",
)

_DEFAULT_CALL_TIMEOUT_SECONDS: Final[float] = 60.0

# After a timeout we terminate the child; give it this long to die before we
# escalate to SIGKILL.
_TERMINATE_JOIN_SECONDS: Final[float] = 5.0

# Sentinel returncode used when a call is terminated for exceeding its timeout.
_TIMEOUT_RETURNCODE: Final[int] = -1


class MngrCallResult(MutableModel):
    """Outcome of one ``mngr`` CLI invocation run inside a forkserver child."""

    returncode: int = Field(description="Process-style exit code; 0 means success.")
    stdout: str = Field(default="", description="Captured stdout (mngr writes JSONL/human output here).")
    stderr: str = Field(default="", description="Captured stderr (mngr writes log lines here).")
    is_timed_out: bool = Field(
        default=False, description="True if the call exceeded its timeout and the child was terminated."
    )


def _coerce_exit_code(code: object) -> int:
    """Map a click/SystemExit code (int, ``None``, or str) to a process-style int."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    # A string exit message conventionally means failure.
    return 1


def _noop_child() -> None:
    """Trivial forkserver target used only to force the forkserver to start.

    Starting any process forces ``multiprocessing`` to launch the forkserver
    and run its preload imports; this no-op child lets :meth:`MngrCaller.prewarm`
    trigger that without doing any real work.
    """


def _run_mngr_cli_in_child(
    conn: Connection,
    argv: tuple[str, ...],
    env_overrides: Mapping[str, str],
) -> None:
    """Run ``mngr <argv>`` in this forked child and send the result back over ``conn``.

    Because ``imbue.mngr.main`` is preloaded in the forkserver, importing it
    here is instant. All of mngr's global-state mutation (loguru, ``sys.argv``,
    stdout/stderr) is confined to this throwaway process, so it never affects
    the minds backend.
    """
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    returncode = 0
    os.environ.update(env_overrides)
    sys.argv = ["mngr", *argv]
    # This inline import is the whole point of the forkserver: importing
    # ``imbue.mngr.main`` at module scope would pay its multi-second cost inside
    # the minds backend process. Here it resolves instantly because the
    # forkserver preloaded it, and it is paid once (in the forkserver), never in
    # the backend.
    from imbue.mngr.main import cli

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
            # unexpected exceptions are left to propagate: the child then exits,
            # and the parent observes the closed pipe (EOF) and returns failure.
            stderr_buffer.write(traceback.format_exc())
            returncode = 1
        # Flush any loguru records that were enqueued to async sinks so they
        # land in the captured stderr buffer before we read it.
        logger.complete()
    conn.send((returncode, stdout_buffer.getvalue(), stderr_buffer.getvalue()))
    conn.close()


class MngrCaller(MutableModel):
    """Runs ``mngr`` CLI commands in children forked from a preloaded forkserver.

    A single instance should be shared process-wide (the underlying
    ``multiprocessing`` forkserver is itself a per-process singleton); use
    :func:`get_default_mngr_caller` to obtain the shared instance.
    """

    default_timeout_seconds: float = Field(
        default=_DEFAULT_CALL_TIMEOUT_SECONDS,
        description="Timeout applied to a call when none is passed explicitly.",
    )

    # ``multiprocessing`` contexts/threads are not pydantic-native; hold them as
    # private runtime state and allow arbitrary types through.
    _context: ForkServerContext | None = PrivateAttr(default=None)
    _context_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_prewarm_started: bool = PrivateAttr(default=False)

    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def _get_context(self) -> ForkServerContext:
        """Return the forkserver context, configuring its preload list once."""
        with self._context_lock:
            if self._context is None:
                context = ForkServerContext()
                context.set_forkserver_preload(list(_FORKSERVER_PRELOAD_MODULES))
                self._context = context
            return self._context

    def prewarm(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the forkserver in the background so the first real call is fast.

        Non-blocking and idempotent: the first call dispatches a tracked thread
        on ``concurrency_group`` that forces the forkserver to start and run its
        (multi-second) preload imports; later calls return immediately. The
        thread runs under the app's concurrency group so the warmup work shows
        up in its resource accounting. Intended to be invoked once at startup.
        """
        with self._context_lock:
            if self._is_prewarm_started:
                return
            self._is_prewarm_started = True
        concurrency_group.start_new_thread(
            self._ensure_forkserver_running,
            name="mngr-caller-prewarm",
            is_checked=False,
            on_failure=lambda exc: logger.warning("mngr forkserver pre-warm thread failed: {}", exc),
        )

    def _ensure_forkserver_running(self) -> None:
        """Force the forkserver to start (and run its preload imports)."""
        try:
            context = self._get_context()
            warmup_process = context.Process(target=_noop_child, name="mngr-caller-warmup")
            warmup_process.start()
            warmup_process.join()
        except OSError as exc:
            # Best-effort warmup: if the OS refuses the warmup fork, the first
            # real call will simply start the forkserver itself (cold).
            logger.warning("mngr forkserver pre-warm failed: {}", exc)

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> MngrCallResult:
        """Run ``mngr <argv>`` in a fresh forkserver child and return its result.

        ``argv`` is the argument vector *after* the ``mngr`` program name (e.g.
        ``["message", "-m", "hi", "--", "agent"]``). ``env_overrides`` are
        applied to the child's ``os.environ`` before the CLI runs. On timeout
        the child is terminated and a result with ``is_timed_out=True`` and a
        non-zero ``returncode`` is returned.
        """
        resolved_timeout = self.default_timeout_seconds if timeout is None else timeout
        context = self._get_context()
        receive_conn, send_conn = context.Pipe(duplex=False)
        child = context.Process(
            target=_run_mngr_cli_in_child,
            args=(send_conn, tuple(argv), dict(env_overrides or {})),
            name="mngr-call",
        )
        child.start()
        # The parent keeps only the receiving end; closing our copy of the send
        # end lets ``recv`` see EOF if the child dies without sending.
        send_conn.close()
        try:
            if receive_conn.poll(resolved_timeout):
                try:
                    returncode, stdout, stderr = receive_conn.recv()
                    return MngrCallResult(returncode=returncode, stdout=stdout, stderr=stderr)
                except EOFError:
                    return MngrCallResult(
                        returncode=1,
                        stderr="mngr child process exited without returning a result",
                    )
            child.terminate()
            return MngrCallResult(
                returncode=_TIMEOUT_RETURNCODE,
                is_timed_out=True,
                stderr=f"mngr {' '.join(argv)} timed out after {resolved_timeout:.0f}s",
            )
        finally:
            receive_conn.close()
            child.join(_TERMINATE_JOIN_SECONDS)
            if child.is_alive():
                child.kill()
                child.join()


_DEFAULT_CALLER_HOLDER: dict[str, MngrCaller | None] = {"caller": None}
_DEFAULT_CALLER_LOCK = threading.Lock()


def get_default_mngr_caller() -> MngrCaller:
    """Return the shared, process-wide :class:`MngrCaller` singleton.

    Constructing it is cheap and does not start the forkserver; call
    :meth:`MngrCaller.prewarm` (once, at startup) to pay the import cost ahead
    of the first real invocation.
    """
    with _DEFAULT_CALLER_LOCK:
        if _DEFAULT_CALLER_HOLDER["caller"] is None:
            _DEFAULT_CALLER_HOLDER["caller"] = MngrCaller()
        return _DEFAULT_CALLER_HOLDER["caller"]
