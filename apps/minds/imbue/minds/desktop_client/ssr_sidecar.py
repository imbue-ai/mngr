"""SSR sidecar supervisor.

The desktop client renders its HTML responses via a Node.js sidecar that
imports the compiled Solid components and returns a fully rendered HTML
document. This module owns the sidecar's lifecycle (spawn, health probe,
restart-on-crash) and exposes a synchronous ``render`` call used by the
``render_*`` shims in ``templates.py``.

Failure model: the sidecar is best-effort. ``render`` raises
:class:`SsrSidecarError` when the sidecar is unhealthy; the caller is
expected to fall back to a client-render shell embedding the route key
and props as JSON so the browser bundle can hydrate without SSR.

Process model:
    * Packaged Electron app: the binary is invoked as Node via
      ``ELECTRON_RUN_AS_NODE=1`` (set in ``MINDS_ELECTRON_EXEC_PATH``),
      pointing at ``resources/frontend/server/assets/server.js`` (built
      by ``scripts/build.js`` from ``frontend/src/main/server.jsx``).
    * Dev (``uv run minds run``): we invoke ``node`` from ``PATH``
      pointing at the on-disk bundle (or the source entry, if Vite has
      run with ``--mode ssr-dev`` -- the dev workflow is to run
      ``pnpm frontend:dev`` in another terminal and point this at the
      compiled ``frontend/dist-server/assets/server.js``).

Health probe: ``GET /__ssr/health`` returns 200 ``{"status":"ok"}`` when
the sidecar's HTTP server is up. ``wait_ready`` polls for a bounded
duration after launch.

Concurrency model: matches the pattern used by ``EnvelopeStreamConsumer``
and ``PermissionRequestsConsumer`` -- a single fire-and-forget worker
thread registered on the caller-supplied ``ConcurrencyGroup`` with
``is_checked=False``, which loops over (pump stdout into the logger
until the subprocess dies) -> (backoff) -> (respawn) until ``stop`` sets
the shutdown event. This keeps a transient ``OSError`` from re-raising
from the CG's ``__exit__`` and avoids leaking threads on every restart.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel

_HEALTH_PATH: Final[str] = "/__ssr/health"
_RENDER_PATH: Final[str] = "/__ssr/render"
_DEFAULT_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_RENDER_TIMEOUT_SECONDS: Final[float] = 5.0
_PROBE_INTERVAL_SECONDS: Final[float] = 0.1
_RESTART_BACKOFF_INITIAL_SECONDS: Final[float] = 0.5
_RESTART_BACKOFF_MAX_SECONDS: Final[float] = 5.0


class SsrSidecarError(RuntimeError):
    """Raised when the sidecar is unhealthy or returns a render error.

    Callers catch this and substitute the client-render fallback shell.
    """


def _pick_free_port() -> int:
    """Bind to port 0, read the OS-assigned port, release the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_node_command(server_entry: Path) -> list[str]:
    """Pick the command line that runs the sidecar.

    In a packaged Electron build, ``MINDS_ELECTRON_EXEC_PATH`` points at
    the Electron binary; running it with ``ELECTRON_RUN_AS_NODE=1``
    makes it behave like a stock Node runtime. In dev we just look up
    ``node`` on ``PATH``.
    """
    electron_path = os.environ.get("MINDS_ELECTRON_EXEC_PATH")
    if electron_path and Path(electron_path).exists():
        return [electron_path, str(server_entry)]
    node_path = shutil.which("node")
    if node_path is None:
        raise SsrSidecarError(
            "SSR sidecar requires `node` on PATH or MINDS_ELECTRON_EXEC_PATH set to an "
            "Electron binary. Neither was found."
        )
    return [node_path, str(server_entry)]


class SsrSidecar(MutableModel):
    """Owns a single Node SSR subprocess and an HTTP client aimed at it.

    Thread-safety: ``start``, ``stop``, and ``render`` are safe to call
    from any thread. Internal state is guarded by a ``threading.RLock``;
    the subprocess and the http client are stable references between
    spawns -- the supervisor thread swaps them under the lock when it
    detects a crash and respawns.

    Lifecycle: call ``start(concurrency_group)`` once; it does the
    synchronous first-spawn (picks port, opens an httpx client, launches
    the subprocess, waits for the health endpoint) and registers a
    single supervisor thread with ``is_checked=False``. The supervisor
    pumps the subprocess's stdout into the logger; when the subprocess
    exits the supervisor sleeps for the current backoff, picks a fresh
    port, and respawns. ``stop`` sets the shutdown event and terminates
    the subprocess, which the supervisor observes between iterations
    and exits.

    Follows the codebase pattern of ``MutableModel`` with public
    ``Field`` constructor args and ``PrivateAttr`` internal state, the
    same shape ``EnvelopeStreamConsumer`` and ``PermissionRequestsConsumer``
    use.
    """

    server_entry: Path = Field(frozen=True, description="On-disk Node entry point for the SSR HTTP server.")
    manifest_path: Path | None = Field(
        default=None,
        frozen=True,
        description="Path to the Vite client manifest (consumed by the SSR sidecar to resolve hashed asset paths).",
    )
    vite_dev_url: str | None = Field(
        default=None,
        frozen=True,
        description="If set, instructs the sidecar to source its client bundle from this Vite dev server.",
    )
    ready_timeout_seconds: float = Field(
        default=_DEFAULT_READY_TIMEOUT_SECONDS,
        frozen=True,
        description="Max time wait_ready will spend polling the health endpoint before raising.",
    )
    render_timeout_seconds: float = Field(
        default=_RENDER_TIMEOUT_SECONDS,
        frozen=True,
        description="Per-call timeout for the underlying httpx render request.",
    )

    # ``_port`` is rebound on every spawn so a re-leased port doesn't
    # produce a permanent EADDRINUSE backoff after the first restart.
    _port: int | None = PrivateAttr(default=None)
    _proc: subprocess.Popen[bytes] | None = PrivateAttr(default=None)
    _client: httpx.Client | None = PrivateAttr(default=None)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    _shutting_down: threading.Event = PrivateAttr(default_factory=threading.Event)

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise SsrSidecarError("SSR sidecar is not started")
        return f"http://127.0.0.1:{self._port}"

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Spawn the sidecar, wait until healthy, register the supervisor.

        Synchronously: picks a free port, opens an ``httpx.Client``,
        launches the subprocess, and polls the health endpoint until it
        returns 200 (or the ready timeout elapses).

        Asynchronously: registers a single daemon supervisor thread on
        ``concurrency_group`` with ``is_checked=False`` (matching the
        ``EnvelopeStreamConsumer`` / ``PermissionRequestsConsumer``
        pattern). The supervisor self-loops over log-pumping +
        respawn-on-crash until ``stop`` sets the shutdown event.

        Raises :class:`SsrSidecarError` if the initial spawn fails or
        the sidecar does not become ready within
        ``ready_timeout_seconds``.
        """
        # Vite emits the SSR bundle as plain ``.js``, but the bundle uses
        # ESM syntax. Without a ``"type": "module"`` package.json sibling
        # Node prints a noisy MODULE_TYPELESS_PACKAGE_JSON warning at
        # every spawn (and reparses the file). Writing a tiny marker
        # next to the entry once silences the warning for both dev and
        # packaged builds.
        self._ensure_esm_marker()
        self._spawn()
        self.wait_ready()
        concurrency_group.start_new_thread(
            target=self._supervise_loop,
            name="ssr-sidecar-supervisor",
            daemon=True,
            # Fire-and-forget: any transient OSError from poll() or httpx
            # during shutdown must not re-raise from the CG's __exit__.
            is_checked=False,
        )

    def wait_ready(self, timeout: float | None = None) -> None:
        """Poll the sidecar's health endpoint until it returns 200.

        Waits between probes via ``_shutting_down.wait`` (not ``time.sleep``)
        so ``stop`` interrupts the poll immediately, and so the file honors
        the project ratchet against ``time.sleep`` (see
        ``cli/run.py::_sleep_then_open`` for the same pattern).
        """
        effective_timeout = timeout if timeout is not None else self.ready_timeout_seconds
        deadline = time.monotonic() + effective_timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            if self._shutting_down.is_set():
                raise SsrSidecarError("SSR sidecar wait_ready interrupted by shutdown")
            with self._lock:
                proc = self._proc
                client = self._client
            if proc is not None and proc.poll() is not None:
                raise SsrSidecarError(f"SSR sidecar exited during startup with code {proc.returncode}")
            if client is None:
                raise SsrSidecarError("SSR sidecar wait_ready called before spawn")
            try:
                response = client.get(_HEALTH_PATH, timeout=1.0)
                if response.status_code == 200:
                    return
                last_exc = SsrSidecarError(f"health probe returned {response.status_code}")
            except httpx.HTTPError as exc:
                last_exc = exc
            if self._shutting_down.wait(timeout=_PROBE_INTERVAL_SECONDS):
                raise SsrSidecarError("SSR sidecar wait_ready interrupted by shutdown")
        raise SsrSidecarError(f"SSR sidecar did not become ready within {effective_timeout}s (last error: {last_exc})")

    def render(self, *, route: str, props: dict[str, Any], bundle: str = "app") -> str:
        """Render a route to an HTML string via the sidecar.

        ``bundle`` selects which client bundle's route registry to use
        (one of ``"app"`` / ``"chrome"`` / ``"sidebar"``). Defaults to
        ``"app"`` so legacy callers that pre-date the multi-bundle split
        keep working without changes.

        Raises :class:`SsrSidecarError` on any transport or render-side
        failure (including the brief window when the supervisor is
        respawning the subprocess). Callers fall back to a client-render
        shell that embeds ``{ "route": route, "props": props }`` for
        client-side hydration.
        """
        client = self._require_client()
        try:
            response = client.post(
                _RENDER_PATH,
                json={"bundle": bundle, "route": route, "props": props},
                timeout=self.render_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise SsrSidecarError(f"SSR sidecar transport error: {exc}") from exc
        if response.status_code != 200:
            raise SsrSidecarError(f"SSR sidecar render returned {response.status_code}: {response.text[:200]}")
        return response.text

    def stop(self) -> None:
        """Terminate the sidecar and close the HTTP client.

        Sets the shutdown event first so the supervisor thread exits on
        its next iteration instead of attempting another respawn.
        """
        self._shutting_down.set()
        with self._lock:
            proc = self._proc
            client = self._client
            self._proc = None
            self._client = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        if client is not None:
            client.close()

    def _ensure_esm_marker(self) -> None:
        """Write a ``"type": "module"`` package.json next to the entry.

        Idempotent. Bytes match what scripts/build.js emits for the
        packaged sidecar, so the file is identical regardless of how the
        bundle got there.
        """
        marker = self.server_entry.parent / "package.json"
        if marker.exists():
            return
        marker.write_text(
            '{\n  "name": "minds-ssr-sidecar",\n  "private": true,\n  "type": "module"\n}\n',
            encoding="utf-8",
        )

    def _require_client(self) -> httpx.Client:
        with self._lock:
            client = self._client
        if client is None:
            raise SsrSidecarError("SSR sidecar is not started")
        return client

    def _spawn(self) -> None:
        """Pick a fresh port, spawn the subprocess, open a matching client.

        Closes any previous client first. A fresh port per spawn avoids
        a permanent EADDRINUSE backoff failure on restart when the OS
        has re-leased the port we picked at construction time.
        """
        port = _pick_free_port()
        env = os.environ.copy()
        env["MINDS_SSR_PORT"] = str(port)
        if self.manifest_path is not None:
            env["MINDS_VITE_MANIFEST"] = str(self.manifest_path)
        if self.vite_dev_url is not None:
            env["MINDS_VITE_DEV_URL"] = self.vite_dev_url
        env["ELECTRON_RUN_AS_NODE"] = "1"
        cmd = _resolve_node_command(self.server_entry)
        logger.info("Starting SSR sidecar: {} (port={})", " ".join(cmd), port)
        # bufsize is left at the default (-1, default buffer size). Python
        # emits a RuntimeWarning if ``bufsize=1`` is paired with
        # ``text=False`` because line buffering only applies in text
        # mode; the binary readline loop in _pump_stdout works correctly
        # with the default buffered binary reader, and Node flushes
        # stdout per ``console.log`` so log lines remain visible.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=False,
        )
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=self.render_timeout_seconds,
        )
        with self._lock:
            previous_client = self._client
            self._port = port
            self._proc = proc
            self._client = client
        if previous_client is not None:
            previous_client.close()

    def _supervise_loop(self) -> None:
        """Single-thread supervisor: pumps stdout, respawns on crash.

        Each iteration:
            1. Reads ``self._proc.stdout`` line-by-line into loguru
               until the read loop hits EOF (i.e. the subprocess has
               closed its stdout, which happens when it exits).
            2. Checks the shutdown event; if set, exits.
            3. Sleeps for the current backoff (interruptible by
               ``stop``), then respawns and resets the backoff to the
               initial value once the new process reports healthy.

        Reading ``self._proc`` per iteration (not capturing it once at
        the top) is what lets the log pump and crash watcher live in a
        single thread without losing track of the current subprocess
        after a respawn. Running as ``is_checked=False`` means any
        transient OSError here does not propagate to the CG's __exit__.
        """
        backoff = _RESTART_BACKOFF_INITIAL_SECONDS
        while not self._shutting_down.is_set():
            with self._lock:
                proc = self._proc
            if proc is None or proc.stdout is None:
                # stop() has cleared the subprocess slot; we're done.
                return
            self._pump_stdout(proc)
            if self._shutting_down.is_set():
                return
            exit_code = proc.poll()
            logger.warning("SSR sidecar exited with code {}; restarting in {}s", exit_code, backoff)
            if self._shutting_down.wait(timeout=backoff):
                return
            try:
                self._spawn()
                self.wait_ready()
            except SsrSidecarError as exc:
                logger.error("SSR sidecar restart failed: {}", exc)
                backoff = min(backoff * 2, _RESTART_BACKOFF_MAX_SECONDS)
                continue
            backoff = _RESTART_BACKOFF_INITIAL_SECONDS

    def _pump_stdout(self, proc: subprocess.Popen[bytes]) -> None:
        """Forward ``proc``'s stdout into loguru, one line at a time.

        Blocks until the read loop hits EOF (i.e. ``proc`` has closed
        its stdout, which happens when it exits). Catches and logs any
        I/O error so the supervisor loop survives transient pipe
        failures and can move on to the respawn step.
        """
        stdout = proc.stdout
        if stdout is None:
            return
        try:
            for raw_line in iter(stdout.readline, b""):
                if not raw_line:
                    break
                text = raw_line.rstrip(b"\n").decode("utf-8", errors="replace")
                if text:
                    logger.bind(component="ssr-sidecar").info(text)
        except OSError as exc:
            logger.warning("SSR sidecar log pump exited: {}", exc)
