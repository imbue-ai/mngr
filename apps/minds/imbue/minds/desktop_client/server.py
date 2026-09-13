"""Graceful cheroot WSGI server + lifecycle for the Flask desktop client.

Replaces the FastAPI/uvicorn ``_PreShutdownAwareServer`` subclass and the
async ``_managed_lifespan`` teardown with one synchronous, explicitly-ordered
shutdown path:

1. On SIGINT/SIGTERM, before the server drains, flip ``shutdown_event`` and
   poke the backend resolver's change callback so every long-lived SSE
   generator wakes and returns cleanly (no mid-stream cancellation, no
   tracebacks on a clean exit).
2. Stop the WSGI server (from a helper thread -- ``stop()`` must not be called
   from the thread running ``serve()``).
3. Once serving has stopped, run the teardown sequence: close the shared
   HTTP client, terminate the envelope + permission-requests consumers, stop
   the pre-warmed mngr caller, and drain the root concurrency group.

The server is cheroot's pure-Python threaded WSGI server rather than the
Werkzeug development server. Werkzeug's dev server hardcodes ``Connection:
close`` on every response (it cannot drain a keep-alive socket before the next
request line), so it never reuses connections. The prior uvicorn server spoke
HTTP/1.1 with keep-alive, and the Electron shell relies on that: its main
process consumes the one-time login code with a ``net.request`` to
``/authenticate`` that 307-redirects to ``/`` and *awaits* the followed
response before loading the chrome view. Chromium's network stack does not
follow that redirect cleanly when the 307 closes the socket, so the await --
and the whole UI startup -- hangs. cheroot restores real HTTP/1.1 keep-alive
(and still streams Server-Sent-Events chunk-by-chunk), matching the wire
behavior the shell was built against.

``desktop_client_runtime`` is a context manager that owns startup (HTTP client
+ geo detection) and the teardown above; ``serve_desktop_client`` runs the
server inside it.
"""

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Final

import httpx
from flask import Flask
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.region_preference import start_geo_detection
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.ws_gateway import create_websocket_aware_wsgi_server
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.minds.utils.sentry.core import flush_sentry_on_shutdown

# Hard timeout for the shared HTTP client used by the share-readiness probe
# (mirrors the old FastAPI lifespan's httpx client timeout).
_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0

# Worker-thread pool size for the cheroot server. This is a HARD cap, not a
# floor: cheroot's ThreadPool.grow() runs exactly once at startup with this
# value and nothing in plain cheroot ever grows the pool afterwards (``max``
# only bounds manual grow() calls). With keep-alive, each live connection --
# page loads, static assets, every /ui/ws WebSocket, and the long-lived SSE
# streams -- holds a worker thread for its lifetime, so this must comfortably
# exceed windows x (1 WebSocket + a few keep-alive HTTP connections) plus the
# transient operation-log streams.
_SERVER_THREAD_POOL_SIZE: Final[int] = 50


def _wake_sse_handlers(state: DesktopClientState) -> None:
    """Wake every long-lived SSE generator so it observes the shutdown flag now.

    The chrome SSE blocks on a ``shutdown_event.wait()`` with a long timeout;
    poking the backend resolver's change callback fires the same event the SSE
    loop waits on, so it returns immediately instead of after the full poll
    interval.
    """
    if isinstance(state.backend_resolver, MngrCliBackendResolver):
        state.backend_resolver.notify_change()


@contextmanager
def desktop_client_runtime(state: DesktopClientState, is_externally_managed_client: bool) -> Iterator[None]:
    """Own the desktop client's startup and ordered teardown.

    On enter: create the shared sync HTTP client (unless one was injected) and
    kick off the one-shot IP-geolocation lookup. On exit: run the teardown
    sequence so the process exits quickly and cleanly. ``is_externally_managed_client``
    is True when the caller injected its own ``http_client`` (e.g. tests), in
    which case the runtime neither creates nor closes it.
    """
    if not is_externally_managed_client:
        # TLS verification is deliberately OFF: this client's only consumer is
        # the share-URL readiness probe, and Python's ssl refuses to match a
        # wildcard certificate against a hostname label containing an
        # underscore (e.g. ``system_interface--...``), which browsers accept.
        # A verifying probe therefore reports "certificate verify failed"
        # forever on share links that work fine in every browser. The probe
        # reads only the redirect status/location (the Access-app "live"
        # signal) and transfers nothing sensitive; the user's browser still
        # fully verifies the real link.
        state.http_client = httpx.Client(follow_redirects=False, timeout=_PROXY_TIMEOUT_SECONDS, verify=False)
    # Kick off the one-shot IP-geolocation lookup in the background so the create
    # form can default each provider's region to the user's nearest datacenter.
    if state.root_concurrency_group is not None:
        start_geo_detection(state.root_concurrency_group, state.geo_location_cache)
        # Start the /ui channel publisher strand so connected SPA windows
        # receive edge-driven state updates for the process lifetime.
        if state.ui_publisher is not None:
            state.ui_publisher.start(state.root_concurrency_group)
    try:
        yield
    finally:
        _shutdown_desktop_client(state, is_externally_managed_client)


def _shutdown_desktop_client(state: DesktopClientState, is_externally_managed_client: bool) -> None:
    """Run the ordered teardown sequence after the server has stopped serving.

    Order matters: stop every long-lived strand that is blocked on external I/O
    BEFORE draining the concurrency group, so the drain does not wait out the
    full shutdown timeout on a thread wedged in a subprocess pipe or a socket
    read with no read timeout.
    """
    # Signal SSE handlers to exit (idempotent: the signal handler set this first).
    state.shutdown_event.set()
    _wake_sse_handlers(state)
    # Stop the channel publisher strand and evict any /ui/ws handlers that
    # connected after the signal-path shutdown (both calls are idempotent).
    if state.ui_publisher is not None:
        state.ui_publisher.stop()
    state.ui_channel_broadcaster.shutdown()
    if not is_externally_managed_client and state.http_client is not None:
        state.http_client.close()
    # SIGTERMs the mngr forward subprocess and closes its pipes so the reader
    # threads exit their for-line loops.
    if state.envelope_stream_consumer is not None:
        state.envelope_stream_consumer.terminate()
    # Sets the consumer's stop event and closes the in-flight follow-stream so
    # its reader thread unblocks from its iter_lines read.
    if state.permission_requests_consumer is not None:
        state.permission_requests_consumer.stop()
    # Tear down any hub-brokered cross-workspace SSH tunnels (paramiko reverse
    # forwards + their connections) so their threads don't outlive the app.
    state.ssh_tunnel_manager.cleanup()
    # Stop the workspace-record sync loop and wait for any in-flight pass to
    # finish BEFORE tearing down the shared mngr caller below. The loop runs
    # ``mngr`` through that caller, so stopping the caller first would let a
    # mid-pass call race its teardown and crash the loop's thread.
    if state.sync_scheduler is not None:
        state.sync_scheduler.stop()
    # Same reason: these all exec through the shared caller stopped just below.
    update_service = state.workspace_update_service
    if update_service is not None:
        update_service.detector.stop()
        update_service.stop_run_polling()
        update_service.apply_window.stop()
    if state.update_scheduler is not None:
        state.update_scheduler.stop()
    # Terminate the idle pre-warmed mngr process so it doesn't wait out the
    # full shutdown timeout blocked reading its socket for the next request.
    get_default_mngr_caller().stop()
    # Exit the root ConcurrencyGroup, waiting up to its shutdown timeout for any
    # still-in-flight strands (e.g. a detached tunnel-setup task) to finish.
    root_concurrency_group: ConcurrencyGroup | None = state.root_concurrency_group
    if root_concurrency_group is not None:
        # Trigger the group's own shutdown event before exiting: the long-lived
        # watcher strands (system-interface-health-probe, discovery-health-
        # watchdog) loop on ``is_shutting_down()`` and can only wind down once
        # this event is set -- without it the exit below always waited out its
        # full timeout and abandoned them. Triggered here, after the ordered
        # teardown above, because the group's strand-starting methods raise
        # once it is shutting down and the steps above may still legitimately
        # start cleanup work on the group.
        root_concurrency_group.shutdown()
        logger.info("Exiting root concurrency group...")
        try:
            root_concurrency_group.__exit__(None, None, None)
        except ConcurrencyExceptionGroup as exc:
            # Strands reported failures or timed out during shutdown; log but
            # don't propagate so other cleanup can run.
            logger.warning("Root concurrency group exit reported errors: {}", exc)
    # Last: flush Sentry and any pending S3 attachment uploads so errors captured
    # during the session (including any logged above during teardown) are sent
    # before the process exits. No-op when Sentry was never initialized.
    flush_sentry_on_shutdown()


def serve_desktop_client(app: Flask, state: DesktopClientState, host: str, port: int) -> None:
    """Serve ``app`` on ``host:port`` until SIGINT/SIGTERM, then return.

    Installs SIGINT/SIGTERM handlers that flip ``shutdown_event`` and wake the
    SSE handlers before stopping the threaded WSGI server. Must be called from
    the main thread (signal handlers can only be installed there). The teardown
    sequence runs in :func:`desktop_client_runtime`'s ``finally`` after this
    returns.
    """
    # cheroot speaks HTTP/1.1 with keep-alive and streams responses without a
    # Content-Length chunk-by-chunk (our SSE generators), matching the wire
    # behavior of the prior uvicorn server -- see this module's docstring for
    # why keep-alive is load-bearing for the Electron shell's startup. The
    # WebSocket-aware subclass additionally lets simple_websocket serve
    # /ui/ws sessions on cheroot's sockets (see ws_gateway.py).
    server = create_websocket_aware_wsgi_server((host, port), app, numthreads=_SERVER_THREAD_POOL_SIZE)

    def _handle_exit(signal_number: int, _frame: FrameType | None) -> None:
        logger.info("Received signal {}; beginning graceful shutdown", signal_number)
        # Flip the flag and wake the SSE loops BEFORE stopping the server so
        # their generators return cleanly instead of being cut off mid-stream.
        state.shutdown_event.set()
        _wake_sse_handlers(state)
        # Push the sentinel to every /ui/ws handler for the same reason: a live
        # WebSocket otherwise pins its worker thread until cheroot's
        # shutdown_timeout expires (measured 5.5s naive vs 0.5s with the wake).
        state.ui_channel_broadcaster.shutdown()
        # ``stop()`` blocks until the serve loop winds down and must run on a
        # different thread than the one calling ``serve()``.
        threading.Thread(target=server.stop, name="minds-server-shutdown", daemon=True).start()

    previous_handler_by_signal = {
        signal_number: signal.signal(signal_number, _handle_exit) for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    server.prepare()
    try:
        server.serve()
    finally:
        # Always close the listening socket and tear down the worker pool, even
        # if ``serve()`` exits by some path other than the signal-driven
        # ``stop()`` (e.g. cheroot re-raising an internal worker interrupt).
        # ``stop()`` is idempotent (it returns early once ``ready`` is False),
        # so this is a no-op on the normal signal-driven shutdown.
        server.stop()
        for signal_number, previous_handler in previous_handler_by_signal.items():
            signal.signal(signal_number, previous_handler)
