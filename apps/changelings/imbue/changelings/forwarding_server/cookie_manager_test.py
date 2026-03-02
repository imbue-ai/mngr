from inline_snapshot import snapshot

from imbue.changelings.forwarding_server.cookie_manager import create_signed_cookie_value
from imbue.changelings.forwarding_server.cookie_manager import get_cookie_name_for_agent
from imbue.changelings.forwarding_server.cookie_manager import verify_signed_cookie_value
from imbue.changelings.primitives import CookieSigningKey
from imbue.mng.primitives import AgentId

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")


def test_get_cookie_name_for_agent_id() -> None:
    result = get_cookie_name_for_agent(_AGENT_A)
    assert result == snapshot("changeling_agent-00000000000000000000000000000001")


def test_get_cookie_name_for_different_agent_id() -> None:
    result = get_cookie_name_for_agent(_AGENT_B)
    assert result == snapshot("changeling_agent-00000000000000000000000000000002")


def test_create_and_verify_cookie_round_trip() -> None:
    key = CookieSigningKey("test-secret-key-83742")

    cookie_value = create_signed_cookie_value(
        agent_id=_AGENT_A,
        signing_key=key,
    )
    verified_id = verify_signed_cookie_value(
        cookie_value=cookie_value,
        signing_key=key,
    )

    assert verified_id == _AGENT_A


def test_verify_cookie_returns_none_for_wrong_key() -> None:
    correct_key = CookieSigningKey("correct-key-19283")
    wrong_key = CookieSigningKey("wrong-key-84729")

    cookie_value = create_signed_cookie_value(
        agent_id=_AGENT_A,
        signing_key=correct_key,
    )
    result = verify_signed_cookie_value(
        cookie_value=cookie_value,
        signing_key=wrong_key,
    )

    assert result is None


def test_verify_cookie_returns_none_for_tampered_value() -> None:
    key = CookieSigningKey("test-key-38472")
    result = verify_signed_cookie_value(
        cookie_value="tampered-garbage-value",
        signing_key=key,
    )
    assert result is None


def test_verify_cookie_returns_none_for_empty_value() -> None:
    key = CookieSigningKey("test-key-19384")
    result = verify_signed_cookie_value(
        cookie_value="",
        signing_key=key,
    )
    assert result is None


def test_create_cookie_produces_different_values_for_different_agents() -> None:
    key = CookieSigningKey("shared-key-82734")

    value_a = create_signed_cookie_value(
        agent_id=_AGENT_A,
        signing_key=key,
    )
    value_b = create_signed_cookie_value(
        agent_id=_AGENT_B,
        signing_key=key,
    )

    assert value_a != value_b
