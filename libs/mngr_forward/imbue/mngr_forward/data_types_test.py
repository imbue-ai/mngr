import json

from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.data_types import ForwardEnvelope
from imbue.mngr_forward.data_types import ListeningPayload
from imbue.mngr_forward.data_types import LoginUrlPayload
from imbue.mngr_forward.data_types import ReverseTunnelEstablishedPayload
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.testing import TEST_AGENT_ID_1


def test_login_url_payload_round_trip() -> None:
    payload = LoginUrlPayload(url="http://localhost:8421/login?one_time_code=x")
    serialized = payload.model_dump(mode="json")
    assert serialized["type"] == "login_url"
    parsed = LoginUrlPayload.model_validate(serialized)
    assert parsed == payload


def test_listening_payload_round_trip() -> None:
    payload = ListeningPayload(host="127.0.0.1", port=ForwardPort(8421))
    serialized = payload.model_dump(mode="json")
    assert serialized["type"] == "listening"
    assert serialized["host"] == "127.0.0.1"
    assert serialized["port"] == 8421


def test_reverse_tunnel_payload_round_trip() -> None:
    payload = ReverseTunnelEstablishedPayload(
        agent_id=TEST_AGENT_ID_1,
        remote_port=PositiveInt(12345),
        local_port=PositiveInt(8420),
        ssh_host="example.modal.run",
        ssh_port=PositiveInt(22),
    )
    serialized = payload.model_dump(mode="json")
    parsed = ReverseTunnelEstablishedPayload.model_validate(serialized)
    assert parsed == payload


def test_forward_envelope_with_optional_agent_id() -> None:
    envelope = ForwardEnvelope(
        stream="observe",
        payload={"type": "DISCOVERY_FULL", "agents": []},
    )
    serialized = envelope.model_dump(mode="json", exclude_none=True)
    assert "agent_id" not in serialized
    assert serialized["stream"] == "observe"

    envelope = ForwardEnvelope(
        stream="event",
        agent_id=TEST_AGENT_ID_1,
        payload={"source": "services"},
    )
    serialized = envelope.model_dump(mode="json", exclude_none=True)
    assert serialized["agent_id"] == str(TEST_AGENT_ID_1)


def test_forward_envelope_round_trip_through_json() -> None:
    envelope = ForwardEnvelope(
        stream="forward",
        agent_id=TEST_AGENT_ID_1,
        payload={"type": "reverse_tunnel_established", "remote_port": 1},
    )
    raw = json.dumps(envelope.model_dump(mode="json", exclude_none=True))
    parsed = ForwardEnvelope.model_validate(json.loads(raw))
    assert parsed == envelope
