import http.server
import json
import threading
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from inline_snapshot import snapshot
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.smoke_tests.accounts import ACCOUNT_DELETION_TOKEN_HEADER
from imbue.minds.smoke_tests.accounts import SMOKE_USER_AGENT
from imbue.minds.smoke_tests.accounts import active_records_refusal
from imbue.minds.smoke_tests.accounts import active_share_host_ids_of
from imbue.minds.smoke_tests.accounts import active_workspace_records_of
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
from imbue.minds.smoke_tests.accounts import require_account_holds_no_workspace
from imbue.minds.smoke_tests.accounts import retire_active_workspace_records
from imbue.minds.smoke_tests.accounts import smoke_headers
from imbue.minds.smoke_tests.accounts import workspace_host_db_ids_of
from imbue.minds.smoke_tests.accounts import workspace_records_of
from imbue.minds.smoke_tests.data_types import SmokeAccountCredentials
from imbue.minds.smoke_tests.primitives import PERSISTENT_ACCOUNT_EMAIL
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_ACCOUNT_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeAccountError
from imbue.minds.smoke_tests.primitives import SmokeAccountHoldsWorkspacesError
from imbue.minds.smoke_tests.wire_types import SmokeWorkspaceRecord
from imbue.minds.smoke_tests.wire_types import SmokeWorkspaceRecordState

_ACCESS_TOKEN = "access-token-5b1e"
_SIGNED_IN: tuple[int, object] = (200, {"status": "OK", "tokens": {"access_token": _ACCESS_TOKEN}})
_PERSISTENT_CREDENTIALS = SmokeAccountCredentials(
    email=NonEmptyStr(PERSISTENT_ACCOUNT_EMAIL), password=SecretStr("Smoke-persistent-1")
)
_LOCAL_AGENT_ID = "agent-7d0c3f5e9a1b4c2d8e6f0a1b2c3d4e5f"
_STUB_AGENT_ID = "agent-1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6"
_DESTROYED_AGENT_ID = "agent-9f8e7d6c5b4a43210fedcba987654321"


class _ReceivedRequest(FrozenModel):
    """One request the stand-in connector received."""

    method: str = Field(description="The request's HTTP method")
    path: str = Field(description="The request's path")
    user_agent: str = Field(description="Its User-Agent header")
    authorization: str = Field(description="Its Authorization header")


class _StandInConnector(http.server.ThreadingHTTPServer):
    """A loopback stand-in for the connector: answers each route with its canned JSON and keeps every request."""

    def __init__(self, answer_by_route: Mapping[str, tuple[int, object]]) -> None:
        super().__init__(("127.0.0.1", 0), _StandInConnectorHandler)
        self.answer_by_route = dict(answer_by_route)
        self.received: list[_ReceivedRequest] = []

    def url(self) -> AnyUrl:
        return AnyUrl(f"http://127.0.0.1:{self.server_address[1]}/")

    def received_routes(self) -> list[str]:
        return [f"{request.method} {request.path}" for request in self.received]


class _StandInConnectorHandler(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        server = self.server
        assert isinstance(server, _StandInConnector)
        server.received.append(
            _ReceivedRequest(
                method=self.command,
                path=self.path,
                user_agent=self.headers.get("User-Agent", ""),
                authorization=self.headers.get("Authorization", ""),
            )
        )
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        status, payload = server.answer_by_route.get(f"{self.command} {self.path}", (404, {"detail": "Not Found"}))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _stand_in_connector(answer_by_route: Mapping[str, tuple[int, object]]) -> Generator[_StandInConnector, None, None]:
    server = _StandInConnector(answer_by_route)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _wire_record(agent_id: str, state: str, display_name: str, provider_kind: str) -> dict[str, Any]:
    """One record as the connector's ``_record_wire_response`` serializes it, secrets blob included."""
    return {
        "host_id": f"host-{agent_id.removeprefix('agent-')}",
        "agent_id": agent_id,
        "display_name": display_name,
        "color": None,
        "provider_kind": provider_kind,
        "hosting_device_id": "device-4c1d",
        "device_label": "a laptop",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": "c2VjcmV0cy1ibG9iLTkxYzQ=",
        "revision": 4,
        "created_at": "2026-10-15T17:02:11+00:00",
        "updated_at": "2026-10-15T17:02:11+00:00",
        "destroyed_at": "2026-10-16T09:00:00+00:00" if state == "destroyed" else None,
    }


def _records_listing(*records: Mapping[str, Any]) -> tuple[int, object]:
    return 200, {"records": list(records), "shared_agent_ids": [], "shared_with_me_matches": None}


_LOCAL_RECORD = _wire_record(_LOCAL_AGENT_ID, "active", "studio-smoke-discover-ab3e7a4d", "docker")
_STUB_RECORD = _wire_record(_STUB_AGENT_ID, "active", "studio-smoke-stub-0c7d", "imbue_cloud_studio-smoke-persistent")
_DESTROYED_RECORD = _wire_record(_DESTROYED_AGENT_ID, "destroyed", "studio-smoke-20261101t000000z-1f2e", "docker")


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


def test_a_records_listing_is_read_into_what_the_suite_names_a_record_by_and_nothing_else() -> None:
    records = workspace_records_of({"records": [_LOCAL_RECORD, _DESTROYED_RECORD], "shared_agent_ids": []})

    assert records == (
        SmokeWorkspaceRecord(
            agent_id=NonEmptyStr(_LOCAL_AGENT_ID),
            state=SmokeWorkspaceRecordState.ACTIVE,
            host_id=f"host-{_LOCAL_AGENT_ID.removeprefix('agent-')}",
            display_name="studio-smoke-discover-ab3e7a4d",
            provider_kind="docker",
        ),
        SmokeWorkspaceRecord(
            agent_id=NonEmptyStr(_DESTROYED_AGENT_ID),
            state=SmokeWorkspaceRecordState.DESTROYED,
            host_id=f"host-{_DESTROYED_AGENT_ID.removeprefix('agent-')}",
            display_name="studio-smoke-20261101t000000z-1f2e",
            provider_kind="docker",
        ),
    )
    assert "c2VjcmV0cy1ibG9iLTkxYzQ=" not in repr(records)


def test_an_account_with_no_records_at_all_reads_as_an_empty_listing() -> None:
    assert workspace_records_of({"records": []}) == ()


def test_only_active_records_count_as_workspaces_of_the_account() -> None:
    # A state this suite does not know is one the connector's count of active rows leaves out too.
    archived_record = {**_STUB_RECORD, "state": "archived"}
    records = workspace_records_of({"records": [_DESTROYED_RECORD, _LOCAL_RECORD, archived_record]})

    assert [str(record.agent_id) for record in active_workspace_records_of(records)] == [_LOCAL_AGENT_ID]
    assert active_workspace_records_of(workspace_records_of({"records": [_DESTROYED_RECORD]})) == ()


@pytest.mark.parametrize(
    ("unreadable_entry", "expected_problem"),
    [
        ({key: value for key, value in _LOCAL_RECORD.items() if key != "state"}, "state: Field required"),
        ({key: value for key, value in _LOCAL_RECORD.items() if key != "agent_id"}, "agent_id: Field required"),
        ({**_LOCAL_RECORD, "agent_id": ""}, "agent_id: String should have at least 1 character"),
        ("not-a-record", "entry: Input should be a valid dictionary"),
    ],
)
def test_a_record_that_cannot_be_read_fails_the_listing_rather_than_being_left_out_of_it(
    unreadable_entry: object, expected_problem: str
) -> None:
    with pytest.raises(SmokeAccountError) as raised:
        workspace_records_of({"records": [_DESTROYED_RECORD, unreadable_entry]})

    message = str(raised.value)
    assert "Record 2 of the 2 that GET /sync/records listed could not be read" in message
    assert expected_problem in message
    # Neither the refusal nor the parse error under it repeats the entry, which holds the record's secrets.
    assert "c2VjcmV0cy1ibG9iLTkxYzQ=" not in message
    assert "c2VjcmV0cy1ibG9iLTkxYzQ=" not in str(raised.value.__cause__)


@pytest.mark.parametrize("payload", [[_LOCAL_RECORD], {"shared_agent_ids": []}, {"records": None}])
def test_an_answer_with_no_list_of_records_is_refused_rather_than_read_as_an_empty_listing(payload: object) -> None:
    with pytest.raises(SmokeAccountError, match="no list of records"):
        workspace_records_of(payload)


def test_an_account_that_still_has_active_records_is_refused_naming_each() -> None:
    refusal = active_records_refusal(
        PERSISTENT_ACCOUNT_EMAIL, active_workspace_records_of(workspace_records_of({"records": [_LOCAL_RECORD]}))
    )

    assert refusal == snapshot(
        "studio-smoke-persistent@example.test still has 1 active workspace record(s): agent-7d0c3f5e9a1b4c2d8e6f0a1b2c3d4e5f ('studio-smoke-discover-ab3e7a4d', provider 'docker'). The connector counts each as a workspace of the account, and the app's first run creates a workspace only for an account that has none. Retire them first: `just minds-smoke-teardown <tier> <run-id>` retires whatever records the account still has active."
    )


def test_an_account_with_no_lease_and_only_destroyed_records_may_start_a_first_run() -> None:
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /workspaces": (200, []),
        "GET /sync/records": _records_listing(_DESTROYED_RECORD),
    }
    with _stand_in_connector(answers) as connector:
        require_account_holds_no_workspace(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)

    assert connector.received_routes() == ["POST /auth/signin", "GET /workspaces", "GET /sync/records"]
    assert {request.user_agent for request in connector.received} == {SMOKE_USER_AGENT}
    assert [request.authorization for request in connector.received[1:]] == [f"Bearer {_ACCESS_TOKEN}"] * 2


def test_an_account_whose_active_record_no_lease_backs_may_not_start_a_first_run() -> None:
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /workspaces": (200, []),
        "GET /sync/records": _records_listing(_DESTROYED_RECORD, _LOCAL_RECORD, _STUB_RECORD),
    }
    with _stand_in_connector(answers) as connector:
        with pytest.raises(SmokeAccountHoldsWorkspacesError) as raised:
            require_account_holds_no_workspace(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)

    message = str(raised.value)
    assert f"{PERSISTENT_ACCOUNT_EMAIL} still has 2 active workspace record(s)" in message
    assert f"{_LOCAL_AGENT_ID} ('studio-smoke-discover-ab3e7a4d', provider 'docker')" in message
    assert f"{_STUB_AGENT_ID} ('studio-smoke-stub-0c7d', provider 'imbue_cloud_studio-smoke-persistent')" in message
    assert _DESTROYED_AGENT_ID not in message
    assert "minds-smoke-teardown" in message


def test_an_account_that_holds_a_lease_is_refused_for_the_lease_before_its_records_are_read() -> None:
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /workspaces": (200, [{"host_db_id": "row-71203", "host_id": "host-a", "status": "stopped"}]),
        "GET /sync/records": _records_listing(_STUB_RECORD),
    }
    with _stand_in_connector(answers) as connector:
        with pytest.raises(SmokeAccountHoldsWorkspacesError) as raised:
            require_account_holds_no_workspace(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)

    assert str(raised.value) == held_workspaces_refusal(PERSISTENT_ACCOUNT_EMAIL, ["row-71203"])
    assert connector.received_routes() == ["POST /auth/signin", "GET /workspaces"]


def test_a_records_listing_the_connector_refuses_fails_the_check_rather_than_passing_it() -> None:
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /workspaces": (200, []),
        "GET /sync/records": (503, {"detail": "auth_upstream_unavailable"}),
    }
    with _stand_in_connector(answers) as connector:
        with pytest.raises(SmokeAccountError, match="GET /sync/records answered 503"):
            require_account_holds_no_workspace(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)


def test_a_teardown_retires_every_record_still_active_and_leaves_the_tombstones_alone() -> None:
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /sync/records": _records_listing(_DESTROYED_RECORD, _LOCAL_RECORD, _STUB_RECORD),
        f"POST /sync/records/by-workspace/{_LOCAL_AGENT_ID}/retire": (200, {"status": "retired"}),
        f"POST /sync/records/by-workspace/{_STUB_AGENT_ID}/retire": (200, {"status": "deleted"}),
    }
    with _stand_in_connector(answers) as connector:
        retired = retire_active_workspace_records(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)

    assert retired == [f"{_LOCAL_AGENT_ID}=retired", f"{_STUB_AGENT_ID}=deleted"]
    assert connector.received_routes() == [
        "POST /auth/signin",
        "GET /sync/records",
        f"POST /sync/records/by-workspace/{_LOCAL_AGENT_ID}/retire",
        f"POST /sync/records/by-workspace/{_STUB_AGENT_ID}/retire",
    ]
    assert {request.user_agent for request in connector.received} == {SMOKE_USER_AGENT}
    assert [request.authorization for request in connector.received[1:]] == [f"Bearer {_ACCESS_TOKEN}"] * 3


def test_a_teardown_of_an_account_with_no_active_record_retires_nothing() -> None:
    answers = {"POST /auth/signin": _SIGNED_IN, "GET /sync/records": _records_listing(_DESTROYED_RECORD)}
    with _stand_in_connector(answers) as connector:
        retired = retire_active_workspace_records(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)

    assert retired == []
    assert connector.received_routes() == ["POST /auth/signin", "GET /sync/records"]


def test_a_record_the_connector_will_not_retire_fails_the_teardown_with_its_reason_after_the_rest_are_retired() -> (
    None
):
    lease_active = {
        "detail": {
            "code": "lease_active",
            "message": f"workspace record {_STUB_AGENT_ID} still holds a cloud lease; destroy the workspace "
            "instead of removing its record",
        }
    }
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /sync/records": _records_listing(_STUB_RECORD, _LOCAL_RECORD),
        f"POST /sync/records/by-workspace/{_STUB_AGENT_ID}/retire": (409, lease_active),
        f"POST /sync/records/by-workspace/{_LOCAL_AGENT_ID}/retire": (200, {"status": "retired"}),
    }
    with _stand_in_connector(answers) as connector:
        with pytest.raises(SmokeAccountError) as raised:
            retire_active_workspace_records(connector_url=connector.url(), credentials=_PERSISTENT_CREDENTIALS)

    message = str(raised.value)
    assert "Could not retire 1 active workspace record(s)" in message
    assert f"{_STUB_AGENT_ID} ('studio-smoke-stub-0c7d', provider 'imbue_cloud_studio-smoke-persistent')" in message
    assert "answered 409" in message
    assert "lease_active" in message
    assert f"Retired 1 other(s) [{_LOCAL_AGENT_ID}=retired]" in message
    assert connector.received_routes()[-1] == f"POST /sync/records/by-workspace/{_LOCAL_AGENT_ID}/retire"


def test_a_teardown_never_retires_the_records_of_an_address_that_is_not_a_smoke_account_s() -> None:
    credentials = SmokeAccountCredentials(email=NonEmptyStr("someone@imbue.com"), password=SecretStr("pw"))
    answers = {
        "POST /auth/signin": _SIGNED_IN,
        "GET /sync/records": _records_listing(_LOCAL_RECORD),
        f"POST /sync/records/by-workspace/{_LOCAL_AGENT_ID}/retire": (200, {"status": "retired"}),
    }
    with _stand_in_connector(answers) as connector:
        with pytest.raises(SmokeAccountError, match="someone@imbue.com: not a smoke address"):
            retire_active_workspace_records(connector_url=connector.url(), credentials=credentials)

    assert connector.received == []
