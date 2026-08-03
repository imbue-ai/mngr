"""Gateway unit tests + the cheroot source-drift guard.

The full WebSocket path (handshake, teardown, shutdown ordering) is covered
end-to-end in ``ui_channel_test.py``; here we pin the pieces that make that
integration safe against dependency drift.
"""

import inspect
from collections.abc import Callable
from typing import Any

from cheroot import wsgi
from inline_snapshot import snapshot

from imbue.minds.desktop_client.ws_gateway import WS_HANDLED_ENVIRON_KEY
from imbue.minds.desktop_client.ws_gateway import WebSocketAwareGateway
from imbue.minds.desktop_client.ws_gateway import create_websocket_aware_wsgi_server
from imbue.minds.desktop_client.ws_gateway import is_websocket_upgrade_environ
from imbue.minds.desktop_client.ws_gateway import mark_websocket_handled


def test_cheroot_gateway_respond_source_has_not_drifted() -> None:
    """``WebSocketAwareGateway.respond`` is a copy of ``Gateway.respond`` plus the
    hijack suppression; if a cheroot upgrade changes the upstream method, this
    fails so the copy is re-audited instead of silently diverging.
    """
    assert inspect.getsource(wsgi.Gateway.respond) == snapshot('''\
    def respond(self):
        """Process the current request.

        From :pep:`333`:

            The start_response callable must not actually transmit
            the response headers. Instead, it must store them for the
            server or gateway to transmit only after the first
            iteration of the application return value that yields
            a NON-EMPTY string, or upon the application's first
            invocation of the write() callable.
        """
        response = self.req.server.wsgi_app(self.env, self.start_response)
        try:
            for chunk in filter(None, response):
                if not isinstance(chunk, bytes):
                    raise ValueError('WSGI Applications must yield bytes')
                self.write(chunk)
        finally:
            # Send headers if not already sent
            self.req.ensure_headers_sent()
            if hasattr(response, 'close'):
                response.close()
''')


def test_is_websocket_upgrade_environ_matches_case_insensitively() -> None:
    assert is_websocket_upgrade_environ({"HTTP_UPGRADE": "WebSocket"})
    assert is_websocket_upgrade_environ({"HTTP_UPGRADE": "websocket"})
    assert not is_websocket_upgrade_environ({"HTTP_UPGRADE": "h2c"})
    assert not is_websocket_upgrade_environ({})


def test_mark_websocket_handled_sets_the_environ_flag() -> None:
    environ: dict[str, object] = {}
    mark_websocket_handled(environ)
    assert environ[WS_HANDLED_ENVIRON_KEY] is True


def _empty_ok_wsgi_app(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
    start_response("200 OK", [])
    return [b""]


def test_server_factory_uses_the_websocket_aware_gateway() -> None:
    server = create_websocket_aware_wsgi_server(("127.0.0.1", 0), _empty_ok_wsgi_app, numthreads=2)
    assert server.gateway is WebSocketAwareGateway
