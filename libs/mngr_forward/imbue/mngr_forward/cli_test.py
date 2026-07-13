"""Tests for ``mngr forward``'s CLI option validation.

These tests stub out heavy dependencies (the FastAPI app + uvicorn loop)
by inspecting only the option-validation phase via direct calls to the
helpers. End-to-end CLI invocation is exercised by the acceptance test.
"""

# asyncio is normally banned, but this file tests the event-loop-level TLS
# teardown behavior of `mngr forward`'s hypercorn serving path, which can only
# be exercised from inside an asyncio loop.
import asyncio
import os
import socket
import ssl
import threading
from typing import Any

import click
import pytest
from hypercorn.asyncio import serve as hypercorn_serve
from hypercorn.config import Config
from loguru import logger

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_forward.cli import ForwardCliOptions
from imbue.mngr_forward.cli import _BoundedSSLShutdownEventLoop
from imbue.mngr_forward.cli import _DEFAULT_PORT
from imbue.mngr_forward.cli import _SSL_SHUTDOWN_TIMED_OUT_MESSAGE
from imbue.mngr_forward.cli import _bind_listen_socket
from imbue.mngr_forward.cli import _build_hypercorn_config
from imbue.mngr_forward.cli import _build_strategy
from imbue.mngr_forward.cli import _filter_snapshot
from imbue.mngr_forward.cli import _handle_serve_loop_exception
from imbue.mngr_forward.cli import _parse_reverse_specs
from imbue.mngr_forward.cli import _validate_options
from imbue.mngr_forward.data_types import ForwardAgentSnapshot
from imbue.mngr_forward.data_types import ForwardListSnapshot
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2
from imbue.mngr_forward.tls import InMemoryTLSConfig


def _opts(**overrides: object) -> ForwardCliOptions:
    return ForwardCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        **overrides,  # ty: ignore[invalid-argument-type]
    )


def test_validation_requires_one_target() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts())


def test_validation_rejects_both_targets() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts(service="system_interface", forward_port=8080))


def test_validation_rejects_no_observe_with_service() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts(service="system_interface", no_observe=True))


def test_validation_accepts_no_observe_with_forward_port() -> None:
    _validate_options(_opts(forward_port=8080, no_observe=True))


def test_validation_rejects_observe_via_file_with_no_observe() -> None:
    with pytest.raises(click.UsageError):
        _validate_options(_opts(forward_port=8080, no_observe=True, observe_via_file=True))


def test_validation_accepts_observe_via_file_with_service() -> None:
    _validate_options(_opts(service="system_interface", observe_via_file=True))


def test_validation_accepts_observe_via_file_with_forward_port() -> None:
    _validate_options(_opts(forward_port=8080, observe_via_file=True))


def test_build_strategy_service() -> None:
    strategy = _build_strategy(_opts(service="system_interface"))
    assert isinstance(strategy, ForwardServiceStrategy)
    assert strategy.service_name == "system_interface"


def test_build_strategy_port() -> None:
    strategy = _build_strategy(_opts(forward_port=8080))
    assert isinstance(strategy, ForwardPortStrategy)
    assert strategy.remote_port == 8080


def test_parse_reverse_specs_dynamic_remote() -> None:
    specs = _parse_reverse_specs(("0:8420",))
    assert specs == (ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420)),)


def test_parse_reverse_specs_fixed_remote() -> None:
    specs = _parse_reverse_specs(("1989:7777",))
    assert specs == (ReverseTunnelSpec(remote_port=NonNegativeInt(1989), local_port=PositiveInt(7777)),)


def test_parse_reverse_specs_repeated() -> None:
    specs = _parse_reverse_specs(("8420:8420", "9090:9090"))
    assert len(specs) == 2
    assert specs[0].local_port == 8420
    assert specs[1].local_port == 9090


def test_parse_reverse_specs_rejects_missing_colon() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("8420",))


def test_parse_reverse_specs_rejects_zero_local() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("8420:0",))


def test_parse_reverse_specs_rejects_negative() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("-1:8420",))


def test_parse_reverse_specs_rejects_non_integer() -> None:
    with pytest.raises(click.UsageError):
        _parse_reverse_specs(("abc:8420",))


def test_bind_listen_socket_binds_requested_free_port() -> None:
    """An explicitly-requested free port is bound exactly as requested."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    sock = _bind_listen_socket("127.0.0.1", free_port)
    try:
        assert sock.getsockname()[1] == free_port
    finally:
        sock.close()


def test_bind_listen_socket_errors_when_requested_port_taken() -> None:
    """An explicitly-requested port that is in use raises rather than moving silently."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupier:
        occupier.bind(("127.0.0.1", 0))
        occupier.listen()
        taken_port = occupier.getsockname()[1]
        with pytest.raises(click.ClickException):
            _bind_listen_socket("127.0.0.1", taken_port)


def test_bind_listen_socket_falls_back_when_default_port_taken() -> None:
    """With no explicit port, a busy default falls back to an OS-assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupier:
        try:
            occupier.bind(("127.0.0.1", _DEFAULT_PORT))
        except OSError:
            pytest.skip(f"default port {_DEFAULT_PORT} is unavailable on this host")
        occupier.listen()
        sock = _bind_listen_socket("127.0.0.1", None)
        try:
            bound_port = sock.getsockname()[1]
            assert bound_port not in (_DEFAULT_PORT, 0)
        finally:
            sock.close()


def _fd_from_bind(config: Config) -> int:
    """Parse the fd number out of a ``fd://<n>`` bind entry."""
    assert len(config.bind) == 1
    bind = config.bind[0]
    assert bind.startswith("fd://")
    return int(bind[len("fd://") :])


def test_build_hypercorn_config_plain_http_when_flag_off() -> None:
    """Flag off yields a plain ``Config`` with no TLS, handed the socket by fd."""
    sock = _bind_listen_socket("127.0.0.1", None)
    try:
        config = _build_hypercorn_config(sock, use_http2=False)
        dup_fd = _fd_from_bind(config)
        try:
            assert not isinstance(config, InMemoryTLSConfig)
            assert config.ssl_enabled is False
            assert config.graceful_timeout == 1.0
            # The fd handed to hypercorn is a dup, not the original -- so
            # hypercorn closing it on shutdown does not double-close the
            # socket the caller's ``finally`` also closes.
            assert dup_fd != sock.fileno()
        finally:
            os.close(dup_fd)
    finally:
        sock.close()


def test_build_hypercorn_config_enables_tls_when_flag_on() -> None:
    """Flag on yields an ``InMemoryTLSConfig`` whose context is a real SSLContext."""
    sock = _bind_listen_socket("127.0.0.1", None)
    try:
        config = _build_hypercorn_config(sock, use_http2=True)
        dup_fd = _fd_from_bind(config)
        try:
            assert isinstance(config, InMemoryTLSConfig)
            assert config.ssl_enabled is True
            assert isinstance(config.create_ssl_context(), ssl.SSLContext)
            assert dup_fd != sock.fileno()
        finally:
            os.close(dup_fd)
    finally:
        sock.close()


def test_filter_snapshot_supports_provider_name_filter() -> None:
    """`--agent-include` / `--agent-exclude` must work the same in --no-observe mode

    as they do in observe mode, so a CEL expression referencing
    ``agent.provider_name`` (which observe mode populates) must also be
    available against the snapshot.
    """
    snapshot = ForwardListSnapshot(
        agents=(
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_1, provider_name="modal"),
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_2, provider_name="docker"),
        )
    )
    filtered = _filter_snapshot(snapshot, include=("agent.provider_name == 'modal'",), exclude=())
    assert tuple(entry.agent_id for entry in filtered.agents) == (TEST_AGENT_ID_1,)


def test_filter_snapshot_supports_host_id_and_name_filter() -> None:
    """All four observe-mode CEL fields are available against the snapshot."""
    snapshot = ForwardListSnapshot(
        agents=(
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_1, host_id="host-a", agent_name="alpha"),
            ForwardAgentSnapshot(agent_id=TEST_AGENT_ID_2, host_id="host-b", agent_name="beta"),
        )
    )
    by_host = _filter_snapshot(snapshot, include=("agent.host_id == 'host-a'",), exclude=())
    assert tuple(entry.agent_id for entry in by_host.agents) == (TEST_AGENT_ID_1,)
    by_name = _filter_snapshot(snapshot, include=(), exclude=("agent.name == 'alpha'",))
    assert tuple(entry.agent_id for entry in by_name.agents) == (TEST_AGENT_ID_2,)


def _asyncio_error_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "asyncio"]


def test_serve_loop_exception_handler_drops_ssl_shutdown_timeout(caplog: pytest.LogCaptureFixture) -> None:
    """The TimeoutError of an abandoned TLS teardown must not reach asyncio's default handler."""
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level("ERROR", logger="asyncio"):
            _handle_serve_loop_exception(
                loop,
                {
                    "message": "Unhandled exception in client_connected_cb",
                    "exception": TimeoutError("SSL shutdown timed out"),
                },
            )
    finally:
        loop.close()
    assert _asyncio_error_records(caplog) == []


def test_serve_loop_exception_handler_drops_ssl_errors(caplog: pytest.LogCaptureFixture) -> None:
    """TLS handshake failures are dropped, matching hypercorn's own runner behavior."""
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level("ERROR", logger="asyncio"):
            _handle_serve_loop_exception(
                loop,
                {
                    "message": "SSL handshake failed",
                    "exception": ssl.SSLError(1, "TLSV1_ALERT_UNKNOWN_CA"),
                },
            )
    finally:
        loop.close()
    assert _asyncio_error_records(caplog) == []


def test_serve_loop_exception_handler_delegates_unrelated_errors(caplog: pytest.LogCaptureFixture) -> None:
    """Anything that is not benign TLS teardown noise still reaches the default handler."""
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level("ERROR", logger="asyncio"):
            _handle_serve_loop_exception(
                loop,
                {
                    "message": "something exploded in a task",
                    "exception": RuntimeError("kaboom-7c1f"),
                },
            )
    finally:
        loop.close()
    assert any("something exploded in a task" in message for message in _asyncio_error_records(caplog))


def test_serve_loop_exception_handler_delegates_other_timeouts(caplog: pytest.LogCaptureFixture) -> None:
    """Only the SSL-shutdown TimeoutError is suppressed; other timeouts must stay visible."""
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level("ERROR", logger="asyncio"):
            _handle_serve_loop_exception(
                loop,
                {
                    "message": "some other timeout",
                    "exception": TimeoutError("read timed out"),
                },
            )
    finally:
        loop.close()
    assert any("some other timeout" in message for message in _asyncio_error_records(caplog))


class _FastSSLShutdownEventLoop(_BoundedSSLShutdownEventLoop):
    """Serve loop with a sub-second TLS shutdown bound so the test stays fast."""

    ssl_shutdown_timeout_seconds: float = 0.4


async def _lifespan_only_asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app for serving-layer tests; no HTTP request is ever made."""
    assert scope["type"] == "lifespan"
    await receive()
    await send({"type": "lifespan.startup.complete"})
    await receive()
    await send({"type": "lifespan.shutdown.complete"})


def _run_tls_server_until_stopped(config: Config, stop_serving: threading.Event) -> None:
    """Serve the dummy app the way ``_serve_forward_app`` does, until the event is set."""

    async def _stop_trigger() -> None:
        while not stop_serving.is_set():
            await asyncio.sleep(0.05)

    with asyncio.Runner(loop_factory=_FastSSLShutdownEventLoop) as runner:
        runner.get_loop().set_exception_handler(_handle_serve_loop_exception)
        runner.run(hypercorn_serve(_lifespan_only_asgi_app, config, shutdown_trigger=_stop_trigger))


def _connect_tls_client_when_listening(port: int) -> ssl.SSLSocket:
    """Open a TLS client connection (cert checks off), retrying until the server listens."""
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE

    def _try_connect() -> ssl.SSLSocket | None:
        try:
            raw_socket = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        except ConnectionRefusedError:
            return None
        return client_context.wrap_socket(raw_socket, server_hostname="localhost")

    tls_socket, _, _ = poll_for_value(_try_connect, timeout=10.0, poll_interval=0.05)
    assert tls_socket is not None, "the TLS server never started listening"
    return tls_socket


def test_abandoned_tls_connection_is_torn_down_quickly_and_quietly(caplog: pytest.LogCaptureFixture) -> None:
    """End-to-end repro of the --use-http2 teardown noise (issue #2455).

    A TLS client completes a handshake and then goes silent, never answering
    the server's close_notify. The serve loop must force-close the connection
    within the bounded SSL shutdown wait (not asyncio's 30s default), and the
    escaping TimeoutError must be dropped instead of surfacing as an
    "Unhandled exception in client_connected_cb" traceback.
    """
    listen_socket = _bind_listen_socket("127.0.0.1", 0)
    listen_port = listen_socket.getsockname()[1]
    config = _build_hypercorn_config(listen_socket, use_http2=True)
    # Shrink the keep-alive so the server initiates the close (and thereby the
    # TLS shutdown) shortly after the client goes idle.
    config.keep_alive_timeout = 0.25

    stop_serving = threading.Event()
    suppressed_messages: list[str] = []
    sink_id = logger.add(suppressed_messages.append, level="DEBUG")
    server_thread = threading.Thread(
        target=_run_tls_server_until_stopped,
        args=(config, stop_serving),
        name="forward-tls-teardown-test-server",
        daemon=True,
    )

    def _is_teardown_suppressed() -> bool:
        return any(_SSL_SHUTDOWN_TIMED_OUT_MESSAGE in message for message in tuple(suppressed_messages))

    with caplog.at_level("ERROR", logger="asyncio"):
        server_thread.start()
        try:
            client_socket = _connect_tls_client_when_listening(listen_port)
            try:
                # If the 30s stdlib shutdown timeout were still in effect, or
                # the teardown error were not routed through the suppression
                # path, this wait would time out.
                wait_for(
                    condition=_is_teardown_suppressed,
                    timeout=10.0,
                    poll_interval=0.05,
                    error_message="the abandoned TLS connection was not torn down within the bounded wait",
                )
            finally:
                client_socket.close()
        finally:
            stop_serving.set()
            server_thread.join(timeout=10.0)
            logger.remove(sink_id)
            listen_socket.close()

    assert not server_thread.is_alive()
    assert _asyncio_error_records(caplog) == []
