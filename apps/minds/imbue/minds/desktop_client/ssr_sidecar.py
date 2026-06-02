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
      pointing at ``resources/frontend/server/index.cjs`` (built by
      ``scripts/build.js`` from ``frontend/src/main/server.js``).
    * Dev (``uv run minds run``): we invoke ``node`` from ``PATH``
      pointing at the on-disk bundle (or the source entry, if Vite has
      run with ``--mode ssr-dev`` -- the dev workflow is to run
      ``pnpm frontend:dev`` in another terminal and point this at the
      compiled ``frontend/dist-server/server.js``).

Health probe: ``GET /__ssr/health`` returns 200 ``{"status":"ok"}`` when
the sidecar's HTTP server is up. ``wait_ready`` polls for a bounded
duration after launch.

This class is intentionally minimal: it owns the subprocess and an
``httpx.Client`` aimed at it. Concurrency control (start_new_thread,
restart-on-crash bookkeeping) is delegated to a caller-supplied
:class:`ConcurrencyGroup` so the lifecycle composes with the rest of the
desktop client's threading model.
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

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup


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


class SsrSidecar:
    """Owns a single Node SSR subprocess and an HTTP client aimed at it.

    Thread-safety: ``start``, ``stop``, and ``render`` are safe to call
    from any thread. Internal state is guarded by a ``threading.RLock``;
    the subprocess and the http client are stable references once
    ``start`` returns.
    """

    def __init__(
        self,
        *,
        server_entry: Path,
        manifest_path: Path | None = None,
        vite_dev_url: str | None = None,
        port: int | None = None,
        ready_timeout_seconds: float = _DEFAULT_READY_TIMEOUT_SECONDS,
        render_timeout_seconds: float = _RENDER_TIMEOUT_SECONDS,
        parent_cg: ConcurrencyGroup | None = None,
    ) -> None:
        self._server_entry = server_entry
        self._manifest_path = manifest_path
        self._vite_dev_url = vite_dev_url
        self._port = port if port is not None else _pick_free_port()
        self._ready_timeout_seconds = ready_timeout_seconds
        self._render_timeout_seconds = render_timeout_seconds
        self._parent_cg = parent_cg
        self._proc: subprocess.Popen[bytes] | None = None
        self._client: httpx.Client | None = None
        self._lock = threading.RLock()
        self._shutting_down = threading.Event()
        self._supervisor_thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        """Spawn the sidecar and wait for it to report healthy.

        Raises :class:`SsrSidecarError` if the sidecar fails to come up
        within ``ready_timeout_seconds``.
        """
        # Vite emits the SSR bundle as plain ``.js``, but the bundle uses
        # ESM syntax. Without a ``"type": "module"`` package.json sibling
        # Node prints a noisy MODULE_TYPELESS_PACKAGE_JSON warning at
        # every spawn (and reparses the file). Writing a tiny marker
        # next to the entry once silences the warning for both dev and
        # packaged builds.
        self._ensure_esm_marker()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            env = os.environ.copy()
            env["MINDS_SSR_PORT"] = str(self._port)
            if self._manifest_path is not None:
                env["MINDS_VITE_MANIFEST"] = str(self._manifest_path)
            if self._vite_dev_url is not None:
                env["MINDS_VITE_DEV_URL"] = self._vite_dev_url
            env["ELECTRON_RUN_AS_NODE"] = "1"
            cmd = _resolve_node_command(self._server_entry)
            logger.info("Starting SSR sidecar: {} (port={})", " ".join(cmd), self._port)
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=1,
                text=False,
            )
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self._render_timeout_seconds,
            )
            if self._parent_cg is not None:
                self._supervisor_thread = self._parent_cg.start_new_thread(
                    target=self._supervise_loop,
                    name="ssr-sidecar-supervisor",
                    daemon=True,
                )
                self._parent_cg.start_new_thread(
                    target=self._pump_logs,
                    name="ssr-sidecar-log-pump",
                    daemon=True,
                )
        self.wait_ready()

    def wait_ready(self, timeout: float | None = None) -> None:
        """Poll the sidecar's health endpoint until it returns 200."""
        deadline = time.monotonic() + (timeout if timeout is not None else self._ready_timeout_seconds)
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            if self._shutting_down.is_set():
                raise SsrSidecarError("SSR sidecar wait_ready interrupted by shutdown")
            if self._proc is not None and self._proc.poll() is not None:
                raise SsrSidecarError(
                    f"SSR sidecar exited during startup with code {self._proc.returncode}"
                )
            try:
                client = self._require_client()
                response = client.get(_HEALTH_PATH, timeout=1.0)
                if response.status_code == 200:
                    return
                last_exc = SsrSidecarError(f"health probe returned {response.status_code}")
            except httpx.HTTPError as exc:
                last_exc = exc
            time.sleep(_PROBE_INTERVAL_SECONDS)
        raise SsrSidecarError(
            f"SSR sidecar did not become ready within {self._ready_timeout_seconds}s "
            f"(last error: {last_exc})"
        )

    def is_healthy(self) -> bool:
        """Cheap probe used by the proxy fallback path."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            client = self._client
        if client is None:
            return False
        try:
            response = client.get(_HEALTH_PATH, timeout=0.5)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def render(self, *, route: str, props: dict[str, Any]) -> str:
        """Render a route to an HTML string via the sidecar.

        Raises :class:`SsrSidecarError` on any transport or render-side
        failure. Callers fall back to a client-render shell that embeds
        ``{ "route": route, "props": props }`` for client-side hydration.
        """
        client = self._require_client()
        try:
            response = client.post(
                _RENDER_PATH,
                json={"route": route, "props": props},
                timeout=self._render_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise SsrSidecarError(f"SSR sidecar transport error: {exc}") from exc
        if response.status_code != 200:
            raise SsrSidecarError(
                f"SSR sidecar render returned {response.status_code}: {response.text[:200]}"
            )
        return response.text

    def stop(self) -> None:
        """Terminate the sidecar and close the HTTP client."""
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
        marker = self._server_entry.parent / "package.json"
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

    def _supervise_loop(self) -> None:
        """Background loop that restarts the sidecar on crash.

        Runs in the parent ConcurrencyGroup. Returns when the
        ``_shutting_down`` event is set, which the lifespan teardown
        triggers via :meth:`stop`.
        """
        backoff = _RESTART_BACKOFF_INITIAL_SECONDS
        while not self._shutting_down.is_set():
            with self._lock:
                proc = self._proc
            if proc is None:
                # ``stop`` clears the slot; that's our exit signal.
                return
            exit_code = proc.poll()
            if exit_code is None:
                backoff = _RESTART_BACKOFF_INITIAL_SECONDS
                time.sleep(0.5)
                continue
            logger.warning("SSR sidecar exited with code {}; restarting in {}s", exit_code, backoff)
            time.sleep(backoff)
            if self._shutting_down.is_set():
                return
            try:
                self._restart_locked()
            except SsrSidecarError as exc:
                logger.error("SSR sidecar restart failed: {}", exc)
            backoff = min(backoff * 2, _RESTART_BACKOFF_MAX_SECONDS)

    def _restart_locked(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._proc = None
        self.start()

    def _pump_logs(self) -> None:
        """Forward the subprocess's stdout into loguru, one line at a time."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw_line in iter(proc.stdout.readline, b""):
                if not raw_line:
                    break
                text = raw_line.rstrip(b"\n").decode("utf-8", errors="replace")
                if text:
                    logger.bind(component="ssr-sidecar").info(text)
        except Exception as exc:
            logger.warning("SSR sidecar log pump exited: {}", exc)
