"""Tests for ``mngr forward``'s CLI helpers.

Drives the helpers directly rather than through a CLI process: option
validation, listen-socket binding and fallback, snapshot filtering, and the
hypercorn serving layer's TLS teardown behavior (serve-loop exception handling
plus an end-to-end abandoned-connection repro over a loopback socket).
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
from hypercorn.config import Config
from hypercorn.typing import ASGIReceiveCallable
from hypercorn.typing import ASGISendCallable
from hypercorn.typing import Scope
from loguru import logger

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_forward.cli import ForwardCliOptions
from imbue.mngr_forward.cli import ServeLoopExceptionReporter
from imbue.mngr_forward.cli import _BoundedSSLShutdownEventLoop
from imbue.mngr_forward.cli import _DEFAULT_PORT
from imbue.mngr_forward.cli import _SSL_SHUTDOWN_TIMED_OUT_MESSAGE
from imbue.mngr_forward.cli import _bind_listen_socket
from imbue.mngr_forward.cli import _build_hypercorn_config
from imbue.mngr_forward.cli import _build_strategy
from imbue.mngr_forward.cli import _filter_snapshot
from imbue.mngr_forward.cli import _parse_reverse_specs
from imbue.mngr_forward.cli import _run_serve_loop
from imbue.mngr_forward.cli import _validate_options
from imbue.mngr_forward.data_types import ForwardAgentSnapshot
from imbue.mngr_forward.data_types import ForwardListSnapshot
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.server import ForwardRepeatRateLimiter
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2
from imbue.mngr_forward.testing import make_in_memory_test_ca
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
    """No CA yields a plain ``Config`` with no TLS, handed the socket by fd."""
    sock = _bind_listen_socket("127.0.0.1", None)
    try:
        config = _build_hypercorn_config(sock, ca=None)
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
    """A CA yields an ``InMemoryTLSConfig`` whose context is a real SSLContext."""
    sock = _bind_listen_socket("127.0.0.1", None)
    try:
        config = _build_hypercorn_config(sock, ca=make_in_memory_test_ca())
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


def _raised_runtime_error(message: str) -> RuntimeError:
    """Return a RuntimeError carrying a real traceback, as one that escaped a task does."""
    try:
        raise RuntimeError(message)
    except RuntimeError as raised:
        return raised


def _reported_serve_loop_lines(
    caplog: pytest.LogCaptureFixture,
    context: dict[str, Any],
    reporter: ServeLoopExceptionReporter | None = None,
    times: int = 1,
) -> tuple[list[str], list[str]]:
    """Run a loop-exception context through the reporter ``times`` over on one loop; return its error and debug lines.

    The loop is only what ``set_exception_handler`` hands a handler, and the
    reporter never touches it, so a burst shares one rather than paying for a
    loop and a sink per report.
    """
    messages: list[Any] = []
    sink_id = logger.add(messages.append, level="DEBUG", format="{message}")
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level("ERROR", logger="asyncio"):
            handler = (reporter if reporter is not None else ServeLoopExceptionReporter()).handle
            for _ in range(times):
                handler(loop, context)
    finally:
        loop.close()
        logger.remove(sink_id)
    error_lines = [str(message).rstrip("\n") for message in messages if message.record["level"].name == "ERROR"]
    debug_lines = [str(message).rstrip("\n") for message in messages if message.record["level"].name == "DEBUG"]
    return error_lines, debug_lines


def test_serve_loop_exception_reporter_drops_ssl_shutdown_timeout(caplog: pytest.LogCaptureFixture) -> None:
    """The TimeoutError of an abandoned TLS teardown must not be reported at all."""
    error_lines, _ = _reported_serve_loop_lines(
        caplog,
        {
            "message": "Unhandled exception in client_connected_cb",
            "exception": TimeoutError("SSL shutdown timed out"),
        },
    )
    assert error_lines == []
    assert _asyncio_error_records(caplog) == []


def test_serve_loop_exception_reporter_drops_ssl_errors(caplog: pytest.LogCaptureFixture) -> None:
    """TLS handshake failures are dropped, matching hypercorn's own runner behavior."""
    error_lines, _ = _reported_serve_loop_lines(
        caplog, {"message": "SSL handshake failed", "exception": ssl.SSLError(1, "TLSV1_ALERT_UNKNOWN_CA")}
    )
    assert error_lines == []
    assert _asyncio_error_records(caplog) == []


def test_serve_loop_exception_reporter_reports_an_unrelated_failure_as_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real failure costs the consumer reading our stderr one line that names it and where it was raised.

    Deferring to asyncio's own handler instead wrote the headline, every
    context object and the whole traceback to the `asyncio` stdlib logger,
    which nothing configures: dozens of stderr lines per report, through
    logging's last-resort handler rather than loguru.
    """
    error_lines, _ = _reported_serve_loop_lines(
        caplog,
        {
            "message": "Task exception was never retrieved",
            "exception": _raised_runtime_error("kaboom-7c1f"),
            "future": "<Task finished name='Task-1'>",
        },
    )

    assert len(error_lines) == 1
    assert len(error_lines[0].splitlines()) == 1
    assert "Task exception was never retrieved" in error_lines[0]
    assert "RuntimeError('kaboom-7c1f')" in error_lines[0]
    assert "cli_test.py" in error_lines[0] and "_raised_runtime_error" in error_lines[0]
    assert _asyncio_error_records(caplog) == []


def test_serve_loop_exception_reporter_keeps_the_context_and_traceback_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing asyncio's own handler would have said is lost: it follows the summary at debug."""
    _, debug_lines = _reported_serve_loop_lines(
        caplog,
        {
            "message": "Task exception was never retrieved",
            "exception": _raised_runtime_error("kaboom-7c1f"),
            "transport": "<_SSLProtocolTransport object>",
        },
    )

    assert len(debug_lines) == 1
    assert "transport: '<_SSLProtocolTransport object>'" in debug_lines[0]
    assert "Traceback (most recent call last):" in debug_lines[0]
    assert "RuntimeError: kaboom-7c1f" in debug_lines[0]


def test_serve_loop_exception_reporter_rations_a_storm_of_one_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A storm of one failure costs a line an interval, carrying the count of what it stood in for.

    The pool-timeout storm that followed a network outage was 1640
    unretrieved-task reports in ninety seconds; relayed as asyncio wrote them
    they were 94% of a log rotation and pushed the day's earlier logs out of
    the bug report.
    """
    clock = [0.0]
    reporter = ServeLoopExceptionReporter(
        limiter=ForwardRepeatRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])
    )
    storm_context = {
        "message": "Task exception was never retrieved",
        "exception": _raised_runtime_error("pool timeout"),
    }

    relayed, _ = _reported_serve_loop_lines(caplog, storm_context, reporter=reporter, times=1640)
    assert len(relayed) == 1

    clock[0] = 61.0
    due_lines, _ = _reported_serve_loop_lines(caplog, storm_context, reporter=reporter)
    assert len(due_lines) == 1
    assert "1639 identical report(s) suppressed since the last one" in due_lines[0]


def test_serve_loop_exception_reporter_reports_a_different_failure_inside_the_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rationing is per failure: a storm of one must not silence a different one that starts during it."""
    clock = [0.0]
    reporter = ServeLoopExceptionReporter(
        limiter=ForwardRepeatRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])
    )
    _reported_serve_loop_lines(
        caplog,
        {"message": "Task exception was never retrieved", "exception": _raised_runtime_error("pool timeout")},
        reporter=reporter,
    )

    other_lines, _ = _reported_serve_loop_lines(
        caplog,
        {"message": "Task exception was never retrieved", "exception": ValueError("something else entirely")},
        reporter=reporter,
    )
    assert len(other_lines) == 1
    assert "ValueError('something else entirely')" in other_lines[0]


def _raised_runtime_error_elsewhere(message: str) -> RuntimeError:
    """A RuntimeError from a second call site, so its traceback names a different frame."""
    try:
        raise RuntimeError(message)
    except RuntimeError as raised:
        return raised


def test_serve_loop_exception_reporter_reports_one_class_raised_at_two_sites_separately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two unrelated bugs of the same class under one headline are two failures, and each owes a report.

    A report the limiter suppresses is written nowhere -- not at debug either --
    and is then counted as a repeat of whatever holds the key. Nearly everything
    the loop is handed arrives under one of a handful of fixed headlines, so
    keying on the headline and the class alone hides the second bug for a whole
    interval and misdescribes it when the count finally lands.
    """
    clock = [0.0]
    reporter = ServeLoopExceptionReporter(
        limiter=ForwardRepeatRateLimiter(interval_seconds=60.0, now_fn=lambda: clock[0])
    )
    headline = "Task exception was never retrieved"

    first_lines, _ = _reported_serve_loop_lines(
        caplog, {"message": headline, "exception": _raised_runtime_error("pool timeout")}, reporter=reporter
    )
    second_lines, _ = _reported_serve_loop_lines(
        caplog,
        {"message": headline, "exception": _raised_runtime_error_elsewhere("a wholly different wedge")},
        reporter=reporter,
    )

    assert len(first_lines) == 1
    assert "_raised_runtime_error" in first_lines[0]
    assert len(second_lines) == 1, "the second site's failure was swallowed as a repeat of the first's"
    assert "RuntimeError('a wholly different wedge')" in second_lines[0]
    assert "_raised_runtime_error_elsewhere" in second_lines[0]

    # The same site inside the interval is still a repeat, so a storm costs one line.
    repeat_lines, _ = _reported_serve_loop_lines(
        caplog, {"message": headline, "exception": _raised_runtime_error("pool timeout")}, reporter=reporter
    )
    assert repeat_lines == []


def test_serve_loop_exception_reporter_reports_a_context_carrying_no_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """asyncio may hand the loop a context with no exception on it; that report still gets its line."""
    error_lines, debug_lines = _reported_serve_loop_lines(
        caplog, {"message": "Executing <Handle foo> took 0.5 seconds", "handle": "<Handle foo>"}
    )
    assert error_lines == ["Executing <Handle foo> took 0.5 seconds"]
    assert "handle: '<Handle foo>'" in debug_lines[0]


def test_serve_loop_exception_reporter_reports_other_timeouts(caplog: pytest.LogCaptureFixture) -> None:
    """Only the SSL-shutdown TimeoutError is suppressed; other timeouts must stay visible."""
    error_lines, _ = _reported_serve_loop_lines(
        caplog, {"message": "some other timeout", "exception": TimeoutError("read timed out")}
    )
    assert len(error_lines) == 1
    assert "some other timeout" in error_lines[0]


class _FastSSLShutdownEventLoop(_BoundedSSLShutdownEventLoop):
    """Serve loop with a sub-second TLS shutdown bound so the test stays fast."""

    ssl_shutdown_timeout_seconds: float = 0.4


async def _lifespan_only_asgi_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
    """Minimal ASGI app for serving-layer tests; no HTTP request is ever made."""
    assert scope["type"] == "lifespan"
    await receive()
    await send({"type": "lifespan.startup.complete"})
    await receive()
    await send({"type": "lifespan.shutdown.complete"})


def _run_tls_server_until_stopped(config: Config, stop_serving: threading.Event) -> None:
    """Serve the dummy app through the production serve loop until the event is set."""

    async def _stop_trigger() -> None:
        while not stop_serving.is_set():
            await asyncio.sleep(0.05)

    _run_serve_loop(
        _lifespan_only_asgi_app, config, loop_factory=_FastSSLShutdownEventLoop, shutdown_trigger=_stop_trigger
    )


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
    """End-to-end repro of the --use-http2 teardown noise (GitHub issue 2455).

    A TLS client completes a handshake and then goes silent, never answering
    the server's close_notify. The serve loop must force-close the connection
    within the bounded SSL shutdown wait (not asyncio's 30s default), and the
    escaping TimeoutError must be dropped instead of surfacing as an
    "Unhandled exception in client_connected_cb" traceback.
    """
    listen_socket = _bind_listen_socket("127.0.0.1", 0)
    listen_port = listen_socket.getsockname()[1]
    config = _build_hypercorn_config(listen_socket, ca=make_in_memory_test_ca())
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
