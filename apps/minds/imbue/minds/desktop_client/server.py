"""Graceful Werkzeug WSGI server + lifecycle for the Flask desktop client.

Replaces the FastAPI/uvicorn ``_PreShutdownAwareServer`` subclass and the
async ``_managed_lifespan`` teardown with one synchronous, explicitly-ordered
shutdown path:

1. On SIGINT/SIGTERM, before the server drains, flip ``shutdown_event`` and
   poke the backend resolver's change callback so every long-lived SSE
   generator wakes and returns cleanly (no mid-stream cancellation, no
   tracebacks on a clean exit).
2. Stop the WSGI server (from a helper thread -- ``shutdown()`` must not be
   called from the thread running ``serve_forever()``).
3. Once serving has stopped, run the teardown sequence: close the shared
   HTTP client, terminate the envelope + permission-requests consumers, stop
   the pre-warmed mngr caller, and drain the root concurrency group.

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
from werkzeug.serving import WSGIRequestHandler
from werkzeug.serving import make_server

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.region_preference import start_geo_detection
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.utils.mngr_caller import get_default_mngr_caller

# Hard timeout for the shared HTTP client used by the share-readiness probe
# (mirrors the old FastAPI lifespan's httpx client timeout).
_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0


class _Http11RequestHandler(WSGIRequestHandler):
    """Werkzeug request handler pinned to HTTP/1.1.

    Werkzeug's default ``protocol_version`` is HTTP/1.0, under which a streaming
    response with no Content-Length (our Server-Sent-Events generators) cannot
    use chunked transfer-encoding and is instead delimited by closing the
    connection -- which breaks incremental ``EventSource`` streaming and keep-alive.
    The prior uvicorn server spoke HTTP/1.1; pin it here to preserve that wire
    behavior so SSE streams chunk-by-chunk as the browser expects.
    """

    protocol_version = "HTTP/1.1"


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
        state.http_client = httpx.Client(follow_redirects=False, timeout=_PROXY_TIMEOUT_SECONDS)
    # Kick off the one-shot IP-geolocation lookup in the background so the create
    # form can default each provider's region to the user's nearest datacenter.
    if state.root_concurrency_group is not None:
        start_geo_detection(state.root_concurrency_group, state.geo_location_cache)
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
    # Terminate the idle pre-warmed mngr process so it doesn't wait out the
    # full shutdown timeout blocked reading its socket for the next request.
    get_default_mngr_caller().stop()
    # Exit the root ConcurrencyGroup, waiting up to its shutdown timeout for any
    # still-in-flight strands (e.g. a detached tunnel-setup task) to finish.
    root_concurrency_group: ConcurrencyGroup | None = state.root_concurrency_group
    if root_concurrency_group is not None:
        logger.info("Exiting root concurrency group...")
        try:
            root_concurrency_group.__exit__(None, None, None)
        except ConcurrencyExceptionGroup as exc:
            # Strands reported failures or timed out during shutdown; log but
            # don't propagate so other cleanup can run.
            logger.warning("Root concurrency group exit reported errors: {}", exc)


def serve_desktop_client(app: Flask, state: DesktopClientState, host: str, port: int) -> None:
    """Serve ``app`` on ``host:port`` until SIGINT/SIGTERM, then return.

    Installs SIGINT/SIGTERM handlers that flip ``shutdown_event`` and wake the
    SSE handlers before stopping the threaded WSGI server. Must be called from
    the main thread (signal handlers can only be installed there). The teardown
    sequence runs in :func:`desktop_client_runtime`'s ``finally`` after this
    returns.
    """
    # ``threaded=True`` yields a ThreadedWSGIServer, whose ``daemon_threads`` is
    # already True -- so a still-iterating SSE connection can never block process
    # exit (and the shutdown flag makes those threads return promptly anyway).
    # The HTTP/1.1 request handler preserves chunked SSE streaming + keep-alive
    # parity with the prior uvicorn server (Werkzeug defaults to HTTP/1.0).
    server = make_server(host, port, app, threaded=True, request_handler=_Http11RequestHandler)

    def _handle_exit(signal_number: int, _frame: FrameType | None) -> None:
        logger.info("Received signal {}; beginning graceful shutdown", signal_number)
        # Flip the flag and wake the SSE loops BEFORE stopping the server so
        # their generators return cleanly instead of being cut off mid-stream.
        state.shutdown_event.set()
        _wake_sse_handlers(state)
        # ``shutdown()`` blocks until ``serve_forever()`` returns and must run
        # on a different thread than the one serving.
        threading.Thread(target=server.shutdown, name="minds-server-shutdown", daemon=True).start()

    previous_handler_by_signal = {
        signal_number: signal.signal(signal_number, _handle_exit) for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        server.serve_forever()
    finally:
        server.server_close()
        for signal_number, previous_handler in previous_handler_by_signal.items():
            signal.signal(signal_number, previous_handler)
