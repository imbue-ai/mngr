import pytest

from imbue.app_sdk.errors import InvalidIdentityHeaderError
from imbue.app_sdk.identity import ANONYMOUS_OWNER
from imbue.app_sdk.identity import OTHER_ORIGIN_REFUSAL
from imbue.app_sdk.identity import RequestIdentity
from imbue.app_sdk.identity import RequestProvenance
from imbue.app_sdk.identity import UNTRUSTED_LOOPBACK_REFUSAL
from imbue.app_sdk.identity import describe_provenance_refusal
from imbue.app_sdk.identity import is_from_another_origin
from imbue.app_sdk.identity import is_loopback_host
from imbue.app_sdk.identity import is_owner_participant
from imbue.app_sdk.identity import parse_identity_header
from imbue.app_sdk.identity import participant_key_of
from imbue.app_sdk.identity import visiting_user_id


def test_a_missing_or_blank_header_reads_as_the_anonymous_owner() -> None:
    assert parse_identity_header(None) == ANONYMOUS_OWNER
    assert parse_identity_header("   ") == ANONYMOUS_OWNER


@pytest.mark.parametrize(
    "header", ["{not json", '{"owner": "yes"}', '{"owner": "false"}', "[false]", '{"user_id": "u"}']
)
def test_a_present_header_that_does_not_parse_is_refused_rather_than_read_as_the_owner(header: str) -> None:
    with pytest.raises(InvalidIdentityHeaderError):
        parse_identity_header(header)


def test_a_person_header_keeps_the_account_and_ignores_fields_it_does_not_know() -> None:
    identity = parse_identity_header(
        '{"owner":false,"user_id":"user-bob-4471","email":"bob@example.com","display_name":"Bob"}'
    )
    assert identity == RequestIdentity(
        owner=False, user_id="user-bob-4471", email="bob@example.com", is_header_present=True
    )


def test_marking_the_header_present_keeps_every_field_the_proxy_stamped() -> None:
    stamped = RequestIdentity(
        owner=False,
        user_id="user-bob-4471",
        email="bob@example.com",
        agent_id="va-0123456789abcdef",
        agent_name="Bob's agent",
        granter="user:8c0f",
    )

    parsed = parse_identity_header(stamped.model_dump_json())

    assert parsed.model_dump(exclude={"is_header_present"}) == stamped.model_dump(exclude={"is_header_present"})
    assert parsed.is_header_present is True


def test_only_a_header_a_proxy_readably_stamped_counts_as_present() -> None:
    """``is_header_present`` tells a page's request from an in-container caller's (a script's, the terminal's),
    which both read as the owner otherwise; what the header itself claims about it is overwritten."""
    assert parse_identity_header('{"owner":true}').is_header_present is True
    assert parse_identity_header('{"owner":true,"is_header_present":false}').is_header_present is True
    assert parse_identity_header(None).is_header_present is False


def test_a_visiting_agent_header_carries_the_grant_and_no_user() -> None:
    identity = parse_identity_header(
        '{"owner":false,"agent_id":"va-0123456789abcdef0123456789abcdef","agent_name":"Josh\'s Claude","granter":"user:8c0f"}'
    )
    assert identity.user_id is None
    assert identity.agent_id == "va-0123456789abcdef0123456789abcdef"
    assert identity.agent_name == "Josh's Claude"
    assert identity.granter == "user:8c0f"


@pytest.mark.parametrize(
    ("header", "expected_key"),
    [
        ('{"owner":true}', "owner"),
        ('{"owner":false}', "owner"),
        ('{"owner":true,"user_id":"user-owner-9c21","email":"owner@example.com"}', "user:user-owner-9c21"),
        ('{"owner":false,"user_id":"user-bob-4471","email":"bob@example.com"}', "user:user-bob-4471"),
        (
            '{"owner":false,"agent_id":"va-0123456789abcdef0123456789abcdef","agent_name":"Visitor","granter":"x"}',
            "agent:va-0123456789abcdef0123456789abcdef",
        ),
    ],
)
def test_the_participant_key_follows_the_header(header: str, expected_key: str) -> None:
    assert participant_key_of(parse_identity_header(header)) == expected_key


def test_the_owner_is_the_owner_under_either_key_and_never_a_visiting_agent() -> None:
    assert is_owner_participant(ANONYMOUS_OWNER) is True
    assert is_owner_participant(RequestIdentity(owner=True, user_id="user-owner-9c21", email="o@example.com")) is True
    assert is_owner_participant(RequestIdentity(owner=False, user_id="user-bob-4471", email="b@example.com")) is False
    # Keyed ``owner`` by participant_key_of, so the owner here too, whatever the flag says.
    assert is_owner_participant(RequestIdentity(owner=False)) is True
    assert is_owner_participant(RequestIdentity(owner=False, agent_id="va-0123", agent_name="V", granter="x")) is False


def test_only_a_signed_in_visitor_has_a_visiting_user_id() -> None:
    assert visiting_user_id(ANONYMOUS_OWNER) is None
    assert visiting_user_id(RequestIdentity(owner=True, user_id="user-owner-9c21", email="o@example.com")) is None
    assert (
        visiting_user_id(RequestIdentity(owner=False, user_id="user-bob-4471", email="b@example.com"))
        == "user-bob-4471"
    )


def test_a_host_names_loopback_by_address_or_as_localhost_with_or_without_a_port() -> None:
    for host in ("127.0.0.1", "127.0.0.1:8090", "localhost", "localhost:8000", "[::1]:8090", "[::1]"):
        assert is_loopback_host(host) is True, host
    for host in ("agent-1.localhost:8421", "evil.example:8090", "", "10.0.0.7:8090", "[::1"):
        assert is_loopback_host(host) is False, host


@pytest.mark.parametrize(
    ("method", "fetch_site", "fetch_mode", "is_other"),
    [
        pytest.param("POST", None, None, False, id="no-fetch-metadata"),
        pytest.param("POST", "same-origin", "cors", False, id="same-origin"),
        pytest.param("GET", "none", "navigate", False, id="user-typed"),
        pytest.param("GET", "cross-site", "navigate", False, id="cross-site-navigation"),
        pytest.param("HEAD", "same-site", "navigate", False, id="same-site-head-navigation"),
        pytest.param("POST", "cross-site", "navigate", True, id="cross-site-form-navigation"),
        pytest.param("POST", "cross-site", "no-cors", True, id="cross-site-no-cors"),
        pytest.param("GET", "cross-site", "cors", True, id="cross-site-fetch"),
        pytest.param("GET", "same-site", "websocket", True, id="same-site-websocket"),
        pytest.param("POST", "same-site", None, True, id="same-site-without-a-mode"),
    ],
)
def test_a_browser_marks_a_page_of_another_origin_except_on_a_get_or_head_navigation(
    method: str, fetch_site: str | None, fetch_mode: str | None, is_other: bool
) -> None:
    assert is_from_another_origin(method, fetch_site, fetch_mode) is is_other


def _provenance(**overrides: object) -> RequestProvenance:
    fields: dict[str, object] = {
        "method": "POST",
        "identity_header": None,
        "fetch_site": None,
        "fetch_mode": None,
        "host": "127.0.0.1:8090",
        "remote_address": "127.0.0.1",
    }
    fields.update(overrides)
    return RequestProvenance.model_validate(fields)


def test_a_header_less_request_is_admitted_from_loopback_alone_under_a_loopback_host() -> None:
    assert describe_provenance_refusal(_provenance()) is None
    assert describe_provenance_refusal(_provenance(host="localhost:8090", remote_address="::1")) is None
    assert describe_provenance_refusal(_provenance(host="rebound.example:8090")) == UNTRUSTED_LOOPBACK_REFUSAL
    assert describe_provenance_refusal(_provenance(remote_address="10.0.0.7")) == UNTRUSTED_LOOPBACK_REFUSAL
    assert describe_provenance_refusal(_provenance(remote_address=None)) == UNTRUSTED_LOOPBACK_REFUSAL


def test_a_request_a_proxy_stamped_is_read_by_its_header_wherever_it_came_from() -> None:
    stamped = _provenance(identity_header='{"owner":true}', host="screen-x.agent-1.localhost:8421")
    assert describe_provenance_refusal(stamped) is None
    assert describe_provenance_refusal(_provenance(identity_header="   ")) is None
    assert (
        describe_provenance_refusal(_provenance(identity_header="   ", host="rebound.example:8090"))
        == UNTRUSTED_LOOPBACK_REFUSAL
    )


def test_a_page_of_another_origin_is_refused_whatever_it_carries() -> None:
    cross_site = _provenance(fetch_site="cross-site", fetch_mode="cors", identity_header='{"owner":true}')
    assert describe_provenance_refusal(cross_site) == OTHER_ORIGIN_REFUSAL
    assert (
        describe_provenance_refusal(_provenance(fetch_site="same-site", fetch_mode="no-cors")) == OTHER_ORIGIN_REFUSAL
    )
    framed = _provenance(method="GET", fetch_site="same-site", fetch_mode="navigate")
    assert describe_provenance_refusal(framed) is None
    own_page = _provenance(fetch_site="same-origin", fetch_mode="cors")
    assert describe_provenance_refusal(own_page) is None
