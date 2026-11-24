import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.minds.smoke_tests.accounts import ACCOUNT_DELETION_TOKEN_HEADER
from imbue.minds.smoke_tests.accounts import SMOKE_USER_AGENT
from imbue.minds.smoke_tests.accounts import active_share_host_ids_of
from imbue.minds.smoke_tests.accounts import held_workspaces_refusal
from imbue.minds.smoke_tests.accounts import is_connector_unavailable
from imbue.minds.smoke_tests.accounts import is_workspace_held_only_in_storage
from imbue.minds.smoke_tests.accounts import is_workspace_running
from imbue.minds.smoke_tests.accounts import is_workspace_stopped
from imbue.minds.smoke_tests.accounts import leased_host_db_id_for_host_id
from imbue.minds.smoke_tests.accounts import leased_template_ref_for_host_id
from imbue.minds.smoke_tests.accounts import origin_of
from imbue.minds.smoke_tests.accounts import parse_finalize_answer
from imbue.minds.smoke_tests.accounts import persistent_account_credentials_from_env_or_none
from imbue.minds.smoke_tests.accounts import smoke_headers
from imbue.minds.smoke_tests.accounts import workspace_host_db_ids_of
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


def test_the_smoke_label_carries_the_prefix_the_analytics_funnel_leaves_out() -> None:
    """apps/analytics drops the download events whose User-Agent starts with this prefix."""
    assert SMOKE_USER_AGENT.startswith("imbue-studio-smoke-test/")


def test_the_pool_row_ids_are_read_out_of_a_workspaces_listing_whatever_state_each_row_is_in() -> None:
    listing = [
        {"host_db_id": "row-1", "host_id": "host-a", "status": "running"},
        {"host_db_id": "row-2", "host_id": "host-b", "status": "stopped"},
        {"host_db_id": "row-3", "host_id": "host-c", "status": "stopping"},
    ]
    assert workspace_host_db_ids_of(listing) == ["row-1", "row-2", "row-3"]
    assert workspace_host_db_ids_of([]) == []


def test_a_workspace_reads_as_stopped_only_once_its_upload_has_landed() -> None:
    assert is_workspace_stopped({"status": "stopped", "vps_address": "203.0.113.7"}) is True
    assert is_workspace_stopped({"status": "stopping", "vps_address": "203.0.113.7"}) is False
    assert is_workspace_stopped({"status": "running", "vps_address": "203.0.113.7"}) is False
    assert is_workspace_stopped({}) is False


def test_a_workspace_reads_as_running_only_when_the_connector_says_so() -> None:
    assert is_workspace_running({"status": "running"}) is True
    assert is_workspace_running({"status": "starting"}) is False
    assert is_workspace_running({"status": "stopped"}) is False
    assert is_workspace_running({}) is False


def test_a_stopped_workspace_is_held_only_in_storage_once_it_has_given_up_its_server() -> None:
    # Stopped and still parked on its server: a start now would restart it in place.
    assert is_workspace_held_only_in_storage({"status": "stopped", "vps_address": "203.0.113.7"}) is False
    assert is_workspace_held_only_in_storage({"status": "stopped", "vps_address": None}) is True
    # A row with no address that is not stopped is mid-restore, not parked in storage.
    assert is_workspace_held_only_in_storage({"status": "starting", "vps_address": None}) is False
    assert is_workspace_held_only_in_storage({"status": "stopping", "vps_address": "203.0.113.7"}) is False


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


def test_the_template_tag_a_leased_row_was_baked_at_is_read_from_its_attributes() -> None:
    listing = [
        {
            "host_db_id": "row-1",
            "host_id": "host-aaaa",
            "attributes": {"repo_branch_or_tag": "minds-v0.10.1", "region": "US-EAST-VA"},
        },
        {"host_db_id": "row-2", "host_id": "host-bbbb", "attributes": {"region": "US-EAST-VA"}},
        {"host_db_id": "row-3", "host_id": "host-cccc"},
    ]
    assert leased_template_ref_for_host_id(listing, "host-aaaa") == "minds-v0.10.1"
    assert leased_template_ref_for_host_id(listing, "host-bbbb") is None
    assert leased_template_ref_for_host_id(listing, "host-cccc") is None
    assert leased_template_ref_for_host_id(listing, "host-dddd") is None


def test_a_poll_goes_on_past_a_connector_that_could_not_answer_but_not_past_a_refusal() -> None:
    assert is_connector_unavailable(500) is True
    assert is_connector_unavailable(502) is True
    assert is_connector_unavailable(503) is True
    # A refusal says something about the request or the workspace, so it is not waited out.
    assert is_connector_unavailable(401) is False
    assert is_connector_unavailable(403) is False
    assert is_connector_unavailable(404) is False
    assert is_connector_unavailable(200) is False


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


def test_an_account_that_still_holds_workspaces_is_refused_naming_each_pool_row() -> None:
    refusal = held_workspaces_refusal("studio-smoke-persistent@example.test", ["row-71203", "row-71204"])
    assert "studio-smoke-persistent@example.test still holds 2 workspace(s)" in refusal
    assert "row-71203, row-71204" in refusal
    assert "minds-smoke-teardown" in refusal
