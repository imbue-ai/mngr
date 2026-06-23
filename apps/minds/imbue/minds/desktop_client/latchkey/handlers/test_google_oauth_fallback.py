"""Acceptance test: end-to-end Google OAuth fallback ordering in the grant flow.

Drives ``LatchkeyPermissionGrantHandler.grant`` through the three Google paths
(Minds-OAuth success, Minds-fail -> self-setup, already-registered) with a
configured ``FakeLatchkey``, asserting the exact latchkey call ordering, the
resulting ``GrantResult``, and -- for the success path -- the on-disk
``latchkey_permissions.json`` the grant produced.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import GrantOutcome
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import MINDS_GOOGLE_OAUTH_CLIENT_ID
from imbue.mngr_latchkey.core import MINDS_GOOGLE_OAUTH_CLIENT_SECRET
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import FakeLatchkey

_GOOGLE_GMAIL_SERVICE_INFO = ServicePermissionInfo(
    name="google-gmail",
    scope="google-gmail-api",
    display_name="Gmail",
    permission_schemas=("any", "google-gmail-read", "google-gmail-send"),
)

# A minimal catalog satisfies the handler constructor; ``grant()`` resolves
# everything it needs from the passed ``service_info`` and never queries it.
_CATALOG_PAYLOAD: dict[str, object] = {
    "google-gmail": [
        {
            "scope": "google-gmail-api",
            "display_name": "Gmail",
            "permissions": [{"name": "google-gmail-read"}, {"name": "google-gmail-send"}],
        },
    ],
}

_PREPARE_CALL = ("auth_prepare", "google-gmail", MINDS_GOOGLE_OAUTH_CLIENT_ID, MINDS_GOOGLE_OAUTH_CLIENT_SECRET)


def _missing_browser_service_info() -> LatchkeyServiceInfo:
    return LatchkeyServiceInfo(
        credential_status=CredentialStatus.MISSING,
        auth_options=frozenset({LATCHKEY_AUTH_OPTION_BROWSER}),
        set_credentials_example=None,
    )


@pytest.fixture
def message_concurrency_group() -> Iterator[ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="google-oauth-acceptance")
    with cg:
        yield cg


def _build_handler(tmp_path: Path, fake: FakeLatchkey, cg: ConcurrencyGroup) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=fake,
        services_catalog=ServicesCatalog.from_catalog_payload(_CATALOG_PAYLOAD),
        mngr_message_sender=MngrMessageSender(mngr_caller=RecordingMngrCaller(), concurrency_group=cg),
        gateway_client=build_fake_gateway_client(),
    )


@pytest.mark.acceptance
def test_google_oauth_fallback_ordering_end_to_end(
    tmp_path: Path,
    message_concurrency_group: ConcurrencyGroup,
) -> None:
    cg = message_concurrency_group

    # Path A: nothing registered -> Minds prepare + consent screen succeeds.
    fake_a = FakeLatchkey(latchkey_directory=tmp_path)
    fake_a.configure_auth(service_info=_missing_browser_service_info(), registered_services=())
    handler_a = _build_handler(tmp_path, fake_a, cg)
    host_a = HostId()
    result_a = handler_a.grant(
        request_event_id="evt-a",
        agent_id=AgentId(),
        host_id=host_a,
        service_info=_GOOGLE_GMAIL_SERVICE_INFO,
        granted_permissions=("google-gmail-read",),
    )
    assert result_a.outcome == GrantOutcome.GRANTED
    assert fake_a.auth_calls == (
        ("auth_list",),
        _PREPARE_CALL,
        ("auth_browser_login", "google-gmail"),
    )
    # The grant effect landed in the host's permissions file.
    on_disk = json.loads(permissions_path_for_host(tmp_path / "mngr_latchkey", host_a).read_text())
    assert on_disk == {"rules": [{"google-gmail-api": ["google-gmail-read"]}]}

    # Path B: nothing registered, Minds attempt fails -> self-setup succeeds.
    fake_b = FakeLatchkey(latchkey_directory=tmp_path)
    fake_b.configure_auth(
        service_info=_missing_browser_service_info(),
        registered_services=(),
        browser_login_result=(False, "minds consent declined"),
        self_setup_result=(True, ""),
    )
    handler_b = _build_handler(tmp_path, fake_b, cg)
    result_b = handler_b.grant(
        request_event_id="evt-b",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_GOOGLE_GMAIL_SERVICE_INFO,
        granted_permissions=("google-gmail-read",),
    )
    assert result_b.outcome == GrantOutcome.GRANTED
    assert fake_b.auth_calls == (
        ("auth_list",),
        _PREPARE_CALL,
        ("auth_browser_login", "google-gmail"),
        ("auth_clear", "google-gmail"),
        ("auth_browser", "google-gmail"),
    )

    # Path C: a client is already registered -> reuse it, skip prepare entirely.
    fake_c = FakeLatchkey(latchkey_directory=tmp_path)
    fake_c.configure_auth(service_info=_missing_browser_service_info(), registered_services=("google-gmail",))
    handler_c = _build_handler(tmp_path, fake_c, cg)
    result_c = handler_c.grant(
        request_event_id="evt-c",
        agent_id=AgentId(),
        host_id=HostId(),
        service_info=_GOOGLE_GMAIL_SERVICE_INFO,
        granted_permissions=("google-gmail-read",),
    )
    assert result_c.outcome == GrantOutcome.GRANTED
    assert fake_c.auth_calls == (
        ("auth_list",),
        ("auth_browser_login", "google-gmail"),
    )
