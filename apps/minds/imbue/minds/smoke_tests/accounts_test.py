import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.minds.smoke_tests.accounts import ACCOUNT_DELETION_TOKEN_HEADER
from imbue.minds.smoke_tests.accounts import SMOKE_USER_AGENT
from imbue.minds.smoke_tests.accounts import active_share_host_ids_of
from imbue.minds.smoke_tests.accounts import leased_host_db_id_for_host_id
from imbue.minds.smoke_tests.accounts import leased_host_db_ids_of
from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.accounts import parse_finalize_answer
from imbue.minds.smoke_tests.accounts import persistent_account_credentials_from_env_or_none
from imbue.minds.smoke_tests.accounts import smoke_headers
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeAccountError


def test_origin_of_drops_the_trailing_slash_so_a_route_can_be_appended() -> None:
    assert origin_of(AnyUrl("https://connector.example.com/")) == "https://connector.example.com"
    assert origin_of(AnyUrl("https://connector.example.com")) == "https://connector.example.com"


def test_every_request_is_labelled_as_the_smoke_suite() -> None:
    assert smoke_headers(None) == {"User-Agent": SMOKE_USER_AGENT}
    with_bearer = smoke_headers(SecretStr("token-value"))
    assert with_bearer["User-Agent"] == SMOKE_USER_AGENT
    assert with_bearer["Authorization"] == "Bearer token-value"


def test_the_pool_row_ids_are_read_out_of_a_hosts_listing() -> None:
    listing = [
        {"host_db_id": "row-1", "host_id": "host-a", "host_name": "one"},
        {"host_db_id": "row-2", "host_id": "host-b", "host_name": "two"},
    ]
    assert leased_host_db_ids_of(listing) == ["row-1", "row-2"]


def test_only_shares_that_are_still_on_are_read_out_of_a_shares_payload() -> None:
    payload = {
        "shares": [
            {"host_id": "host-a", "state": "active", "needs_reshare": False},
            {"host_id": "host-b", "state": "inactive", "needs_reshare": False},
            {"host_id": "host-c", "state": "suspended"},
        ]
    }
    assert active_share_host_ids_of(payload) == ["host-a"]


def test_an_account_with_no_shares_at_all_reads_as_none_to_turn_off() -> None:
    assert active_share_host_ids_of({"shares": []}) == []


def test_the_persistent_account_is_read_from_the_environment_only_when_both_halves_are_there() -> None:
    assert persistent_account_credentials_from_env_or_none({SMOKE_ACCOUNT_EMAIL_ENV_VAR: "a@example.test"}) is None
    assert persistent_account_credentials_from_env_or_none({}) is None

    credentials = persistent_account_credentials_from_env_or_none(
        {SMOKE_ACCOUNT_EMAIL_ENV_VAR: "studio-smoke-persistent@example.test", SMOKE_ACCOUNT_PASSWORD_ENV_VAR: "pw"}
    )

    assert credentials is not None
    assert str(credentials.email) == "studio-smoke-persistent@example.test"
    assert credentials.password.get_secret_value() == "pw"


def test_a_leased_row_is_found_by_its_host_coordinate() -> None:
    listing = [
        {"host_db_id": "row-1", "host_id": "host-aaaa", "host_name": "studio-smoke-run-1"},
        {"host_db_id": "row-2", "host_id": "host-bbbb", "host_name": "other"},
    ]
    assert leased_host_db_id_for_host_id(listing, "host-bbbb") == "row-2"
    assert leased_host_db_id_for_host_id(listing, "host-cccc") is None
    assert leased_host_db_id_for_host_id([], "host-aaaa") is None


def test_the_deletion_token_header_is_the_one_the_connector_names() -> None:
    assert ACCOUNT_DELETION_TOKEN_HEADER == "X-Imbue-Deletion-Token"


def test_a_deletion_is_read_as_done_only_when_the_account_was_finalized() -> None:
    # ``rows_deleted`` is per table, the shape ``account_deletion_steps.FinalizeReport`` declares.
    finalized = parse_finalize_answer(
        {
            "user_id": "u1",
            "finalized_at": "2026-10-22T00:00:00+00:00",
            "rows_deleted": {"account_entitlements": 1, "shares": 0},
            "litellm_user_deleted": True,
        }
    )
    assert str(finalized.user_id) == "u1"
    assert finalized.rows_deleted_by_table == {"account_entitlements": 1, "shares": 0}
    assert finalized.is_litellm_user_deleted is True

    # An answer with no finalized_at never counts as a completed deletion.
    with pytest.raises(SmokeAccountError):
        parse_finalize_answer({"user_id": "u1", "rows_deleted": {}})

    # A scalar would mean the route's contract moved under us; refuse rather than guess.
    with pytest.raises(SmokeAccountError):
        parse_finalize_answer({"user_id": "u1", "finalized_at": "2026-10-22T00:00:00+00:00", "rows_deleted": 3})
