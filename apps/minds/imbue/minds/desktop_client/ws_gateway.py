"""WebSocket-capable cheroot WSGI plumbing for the desktop client.

cheroot has no native WebSocket support and ``simple_websocket`` only knows how
to pull the raw socket out of Werkzeug/Gunicorn/Eventlet/Gevent environs, so a
stock cheroot + simple_websocket combination fails every handshake with
``RuntimeError: Cannot obtain socket from WSGI environment``. This module
closes that gap (verified end-to-end by the 2026-08-02 spike; see
``blueprint/minds-mithril-spa/plan-minds-mithril-spa.md``):

1. :class:`WebSocketAwareGateway` exposes a ``dup()``'d copy of the
   connection's raw socket under the ``werkzeug.socket`` environ key for
   ``Upgrade: websocket`` requests -- the same key (and the same dup trick)
   simple_websocket's gevent mode uses. The dup is load-bearing:
   simple_websocket closes the fd it is handed when the session ends, and if
   that were cheroot's own fd, cheroot's later ``conn.close()``
   (``shutdown(SHUT_RDWR)``) would hit a dead fd, raise ``EBADF``, and take
   down the whole server via cheroot's fatal-interrupt path.
2. After a route has actually hijacked the socket (it calls
   :func:`mark_websocket_handled`), the gateway suppresses cheroot's normal
   response handling for that request: headers are marked already-sent (the
   WebSocket handshake and close were spoken on the raw socket by
   simple_websocket) and the connection is flagged close-only so the spent
   socket never re-enters the keep-alive pool. Ordinary responses on the same
   route -- e.g. a 401 from an auth check that runs before the handshake --
   are untouched, because the flag is only set once hijacking happened.

Sizing note: cheroot's worker pool NEVER grows after startup
(``ThreadPool.grow`` is called exactly once, with ``min``; ``max`` only caps
manual ``grow()`` calls, which plain cheroot never makes). Every live
WebSocket therefore pins one worker thread for its connection lifetime,
exactly as every long-lived SSE stream already does -- ``numthreads`` is the
hard concurrency cap for WS + SSE + in-flight HTTP combined.
"""

from typing import Any

from cheroot import wsgi
from cheroot.wsgi import Gateway_10

from imbue.minds.errors import MindError

# Environ key a route handler sets (via mark_websocket_handled) once
# simple_websocket has completed the handshake and owns the socket.
WS_HANDLED_ENVIRON_KEY: str = "minds.ws_handled"


class NonBytesWsgiChunkError(MindError, ValueError):
    """Raised when a WSGI application yields a non-bytes chunk."""

    ...


def mark_websocket_handled(environ: dict[str, Any]) -> None:
    """Record in ``environ`` that this request's socket was hijacked for a WebSocket session."""
    environ[WS_HANDLED_ENVIRON_KEY] = True


class WebSocketAwareGateway(Gateway_10):
    """``Gateway_10`` that lets simple_websocket run WebSocket sessions under cheroot."""

    def get_environ(self) -> dict[str, Any]:  # ty: ignore[invalid-method-override]
        environ = super().get_environ()
        if is_websocket_upgrade_environ(environ):
            # simple_websocket closes the fd it is handed when the session
            # ends; give it its own dup so cheroot's fd stays valid for the
            # worker's own conn.close() afterwards (clean FIN, no EBADF).
            environ["werkzeug.socket"] = self.req.conn.socket.dup()
        return environ

    def respond(self) -> None:
        """``Gateway.respond`` plus post-hijack suppression.

        This is a copy of cheroot 11's ``Gateway.respond`` with one addition in
        the ``finally`` block; ``ws_gateway_test.py`` snapshots the upstream
        source so a cheroot upgrade that changes ``respond`` fails loudly here
        instead of drifting silently.
        """
        response = self.req.server.wsgi_app(self.env, self.start_response)
        try:
            for chunk in filter(None, response):
                if not isinstance(chunk, bytes):
                    raise NonBytesWsgiChunkError("WSGI Applications must yield bytes")
                self.write(chunk)
        finally:
            if self.env.get(WS_HANDLED_ENVIRON_KEY):
                # The WebSocket session already spoke the entire protocol
                # (101 handshake through close frame) on the raw socket:
                # nothing may be written after it, and the connection must
                # not be reused for keep-alive.
                self.req.sent_headers = True
                self.req.close_connection = True
            else:
                # An upgrade-headed request that never hijacked (auth
                # rejection, 404, failed handshake): nothing owns the dup'd
                # fd from get_environ, so close it here rather than leaving
                # its lifetime to the environ dict's garbage collection.
                dup_socket = self.env.get("werkzeug.socket")
                if dup_socket is not None:
                    dup_socket.close()
            self.req.ensure_headers_sent()
            if hasattr(response, "close"):
                response.close()


def create_websocket_aware_wsgi_server(bind_addr: tuple[str, int], wsgi_app: Any, numthreads: int) -> wsgi.Server:
    """A ``cheroot.wsgi.Server`` wired to :class:`WebSocketAwareGateway`.

    ``gateway`` is a plain attribute cheroot reads per-request, so assigning
    the subclass post-construction is the supported hook (no server subclass
    needed).
    """
    server = wsgi.Server(bind_addr, wsgi_app, numthreads=numthreads)
    server.gateway = WebSocketAwareGateway
    return server


def is_websocket_upgrade_environ(environ: dict[str, Any]) -> bool:
    """Whether ``environ`` describes a WebSocket upgrade request (case-insensitive header match)."""
    return environ.get("HTTP_UPGRADE", "").lower() == "websocket"
