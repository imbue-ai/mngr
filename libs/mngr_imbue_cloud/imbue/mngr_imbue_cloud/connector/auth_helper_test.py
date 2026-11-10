from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.imbue_common.profiling import drain_events
from imbue.mngr_imbue_cloud.connector.auth_helper import force_refresh
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId

_ACCOUNT = ImbueCloudAccount("alice@imbue.com")


def _store_with_a_session(sessions_dir: Path) -> ImbueCloudSessionStore:
    store = ImbueCloudSessionStore(sessions_dir=sessions_dir)
    store.save(
        AuthSession(
            user_id=SuperTokensUserId("user-abc"),
            email=_ACCOUNT,
            access_token=SecretStr("stale-access-token"),
            refresh_token=SecretStr("rt-1"),
            access_token_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    return store


def test_force_refresh_maps_the_connectors_refused_refresh_to_the_auth_error(tmp_path: Path) -> None:
    """The connector answers a refused refresh with ``200 {"status": "ERROR"}``; that is the sign-out class."""
    refresh_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        refresh_paths.append(request.url.path)
        return httpx2.Response(200, json={"status": "ERROR", "message": "refresh token revoked"})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx2.MockTransport(handler))
    store = _store_with_a_session(tmp_path)

    with pytest.raises(ImbueCloudAuthError, match="Refresh rejected by connector: refresh token revoked"):
        force_refresh(store, client, _ACCOUNT)

    assert refresh_paths == ["/auth/session/refresh"]
    # The refusal rotated nothing: the stored session still carries the refused tokens.
    stored = store.load_by_account(_ACCOUNT)
    assert stored is not None
    assert stored.refresh_token is not None
    assert stored.refresh_token.get_secret_value() == "rt-1"
    assert stored.access_token.get_secret_value() == "stale-access-token"


def test_force_refresh_runs_inside_a_token_refresh_span_enclosing_the_connector_request(
    tmp_path: Path, collecting_profiler: None
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"status": "OK", "tokens": {"access_token": "at-2", "refresh_token": "rt-2"}})

    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"), transport=httpx2.MockTransport(handler))
    store = _store_with_a_session(tmp_path)

    refreshed = force_refresh(store, client, _ACCOUNT)

    assert refreshed.access_token.get_secret_value() == "at-2"
    spans: list[dict[str, Any]] = [event for event in drain_events() if event["ph"] == "X"]
    [request_span, refresh_span] = [
        span for span in spans if span["name"] in ("connector.request", "connector.token_refresh")
    ]
    assert request_span["name"] == "connector.request"
    assert request_span["args"]["path"] == "/auth/session/refresh"
    assert refresh_span["name"] == "connector.token_refresh"
    assert refresh_span["args"] == {}
    # The request span is nested inside the refresh span on the timeline.
    assert refresh_span["ts"] <= request_span["ts"]
    assert request_span["ts"] + request_span["dur"] <= refresh_span["ts"] + refresh_span["dur"]
