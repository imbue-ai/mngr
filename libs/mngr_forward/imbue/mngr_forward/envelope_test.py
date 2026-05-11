import io
import json
import threading

import pytest

from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.data_types import ReverseTunnelEstablishedPayload
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.testing import TEST_AGENT_ID_1


@pytest.fixture
def buffer_writer() -> tuple[EnvelopeWriter, io.StringIO]:
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    return writer, buf


def _read_lines(buf: io.StringIO) -> list[dict]:
    text = buf.getvalue()
    return [json.loads(line) for line in text.splitlines() if line]


def test_emit_observe_pass_through(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_observe('{"type":"DISCOVERY_FULL","agents":[]}')
    [envelope] = _read_lines(buf)
    assert envelope["stream"] == "observe"
    assert "agent_id" not in envelope
    assert envelope["payload"]["type"] == "DISCOVERY_FULL"


def test_emit_event_includes_agent_id(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_event(TEST_AGENT_ID_1, '{"source":"services","service":"system_interface"}')
    [envelope] = _read_lines(buf)
    assert envelope["stream"] == "event"
    assert envelope["agent_id"] == str(TEST_AGENT_ID_1)
    assert envelope["payload"]["source"] == "services"


def test_emit_login_url(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_login_url("http://localhost:8421/login?one_time_code=abc")
    [envelope] = _read_lines(buf)
    assert envelope["stream"] == "forward"
    assert envelope["payload"]["type"] == "login_url"
    assert envelope["payload"]["url"].endswith("?one_time_code=abc")


def test_emit_listening(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_listening(host="127.0.0.1", port=ForwardPort(8421))
    [envelope] = _read_lines(buf)
    assert envelope["stream"] == "forward"
    assert envelope["payload"]["type"] == "listening"
    assert envelope["payload"]["port"] == 8421


def test_emit_reverse_tunnel_established(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_reverse_tunnel_established(
        ReverseTunnelEstablishedPayload(
            agent_id=TEST_AGENT_ID_1,
            remote_port=PositiveInt(12345),
            local_port=PositiveInt(8420),
            ssh_host="example.modal.run",
            ssh_port=PositiveInt(22),
        )
    )
    [envelope] = _read_lines(buf)
    assert envelope["stream"] == "forward"
    assert envelope["agent_id"] == str(TEST_AGENT_ID_1)
    assert envelope["payload"]["type"] == "reverse_tunnel_established"
    assert envelope["payload"]["remote_port"] == 12345


def test_concurrent_writes_do_not_interleave() -> None:
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)

    def worker(index: int) -> None:
        for _ in range(50):
            writer.emit_observe(json.dumps({"type": "test", "i": index}))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [line for line in buf.getvalue().splitlines() if line]
    assert len(lines) == 8 * 50
    # Every line must parse independently — no interleaved bytes mid-line.
    for line in lines:
        parsed = json.loads(line)
        assert parsed["stream"] == "observe"
        assert parsed["payload"]["type"] == "test"


def test_non_json_observe_line_is_passed_through(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_observe("not actually json")
    [envelope] = _read_lines(buf)
    assert envelope["payload"] == {"raw": "not actually json"}


def test_blank_lines_are_dropped(buffer_writer: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = buffer_writer
    writer.emit_observe("")
    writer.emit_observe("   ")
    writer.emit_event(TEST_AGENT_ID_1, "")
    assert buf.getvalue() == ""
