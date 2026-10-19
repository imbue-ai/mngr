"""The desktop's TURN broker (``specs/turn-relay/spec.md``, section 6).

A TURN credential is a bearer token for a general-purpose relay, so it never
goes inside a workspace: the Screen service asks this desktop through the
latchkey ``minds-api-proxy`` (``GET /api/v1/agents/<id>/ice-servers``), and
the desktop mints one from the connector (``GET /turn/ice-servers``) for the
signed-in account that owns the workspace, caching one credential per account
and refreshing it at half life. A refusal (plan, quota, rate limit) or an
unreachable connector leaves the viewer with host candidates, which is correct
for a workspace reached on the same machine.
"""

import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.api_models import IceServer
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.errors import MindError
from imbue.mngr_imbue_cloud.config import get_active_profile_dir
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.connector.client import CLIENT_ID_HEADER
from imbue.mngr_imbue_cloud.connector.client import get_client_identifier
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.wire import WireModel

# The connector route that mints a credential for the signed-in session.
TURN_ICE_SERVERS_PATH: Final[str] = "/turn/ice-servers"
# One mint is one HTTPS round trip to Cloudflare behind the connector.
TURN_MINT_TIMEOUT_SECONDS: Final[float] = 15.0
# The connector's refusals: the plan gate and the quota (403) and the per-account rate limit (429).
_REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({403, 429})


class TurnCredentialError(MindError):
    """Base for a failure to mint a relay credential."""


class TurnCredentialRefusedError(TurnCredentialError):
    """The connector refused the mint on purpose: the plan cannot share, the quota is spent, or the account is rate-limited."""


class TurnCredentialUnavailableError(TurnCredentialError):
    """No credential could be minted for a reason that may pass: no session, an unreachable connector, a Cloudflare failure."""


class TurnCredential(FrozenModel):
    """One minted relay credential: the RTCIceServer list as the connector answered it, and its lifetime."""

    ice_servers: tuple[IceServer, ...] = Field(description="The connector's RTCIceServer list, unchanged")
    issued_at: datetime = Field(description="When this desktop minted it (UTC)")
    expires_at: datetime = Field(description="When the relay stops honouring it (UTC)")


class _ConnectorIceServersBody(WireModel):
    """The connector's ``GET /turn/ice-servers`` answer."""

    ice_servers: list[IceServer]
    expires_at: datetime


class _ConnectorRefusalDetail(WireModel):
    """The structured ``detail`` of a connector refusal (``raise_as_http``), as much of it as the log line needs."""

    code: str | None = None
    message: str | None = None


class _ConnectorRefusalBody(WireModel):
    """A connector error body: FastAPI's ``{"detail": ...}`` envelope, the detail a string or a structured object."""

    detail: _ConnectorRefusalDetail | str | None = None


@pure
def is_past_half_life(credential: TurnCredential, now: datetime) -> bool:
    """Whether a cached credential is due for its refresh: half its lifetime has passed."""
    half_life = (credential.expires_at - credential.issued_at) / 2
    return now >= credential.issued_at + half_life


@pure
def credential_from_connector_response(status_code: int, body_text: str, now: datetime) -> TurnCredential:
    """The credential a connector answer carries; raises the refusal or unavailability it reports instead."""
    if status_code == 200:
        try:
            body = _ConnectorIceServersBody.model_validate_json(body_text)
        except ValidationError as e:
            raise TurnCredentialUnavailableError(f"the connector answered an unreadable credential: {e}") from e
        if body.expires_at <= now:
            raise TurnCredentialUnavailableError("the connector answered an already expired credential")
        return TurnCredential(ice_servers=tuple(body.ice_servers), issued_at=now, expires_at=body.expires_at)
    if status_code in _REFUSAL_STATUSES:
        raise TurnCredentialRefusedError(f"the connector refused (HTTP {status_code}): {_describe_refusal(body_text)}")
    raise TurnCredentialUnavailableError(f"the connector could not mint a credential (HTTP {status_code})")


def _describe_refusal(body_text: str) -> str:
    """The refusal's ``code`` (``quota_exceeded``, ``turn_quota_exceeded``, ``turn_rate_limited``), else its message."""
    try:
        body = _ConnectorRefusalBody.model_validate_json(body_text)
    except ValidationError:
        return body_text[:200]
    if isinstance(body.detail, _ConnectorRefusalDetail):
        return body.detail.code or body.detail.message or body_text[:200]
    return body.detail or body_text[:200]


class TurnCredentialClientInterface(MutableModel, ABC):
    """Mints relay credentials for a signed-in account."""

    @abstractmethod
    def mint_credential(self, user_id: str, workspace_id: str | None) -> TurnCredential:
        """Mint one credential for the account, attributed to the workspace host ``workspace_id`` when known.

        Raises TurnCredentialRefusedError or TurnCredentialUnavailableError.
        """


class ConnectorTurnCredentialClient(TurnCredentialClientInterface):
    """Mints through the connector's ``GET /turn/ice-servers`` with the account's session token.

    The token is the plugin's (``mngr imbue_cloud``'s session store under the active mngr profile);
    one near its expiry is refreshed through the plugin's own ``auth refresh`` first, so the connector
    sees the live session rather than a stale one.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    connector_url: AnyUrl = Field(frozen=True, description="Base URL of the remote_service_connector")
    mngr_host_dir: Path = Field(frozen=True, description="The mngr host dir whose active profile holds the sessions")
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        default=None, frozen=True, description="Refreshes a near-expiry session token; None leaves the token as it is"
    )
    timeout_seconds: float = Field(default=TURN_MINT_TIMEOUT_SECONDS, frozen=True, description="Per-mint timeout")
    transport: httpx.BaseTransport | None = Field(
        default=None, frozen=True, description="httpx transport override; tests inject an httpx.MockTransport"
    )

    def mint_credential(self, user_id: str, workspace_id: str | None) -> TurnCredential:
        session = self._session_with_live_token(user_id)
        identifier = get_client_identifier()
        headers = {
            CLIENT_ID_HEADER: identifier,
            "User-Agent": identifier,
            "Authorization": f"Bearer {session.access_token.get_secret_value()}",
        }
        url = str(self.connector_url).rstrip("/") + TURN_ICE_SERVERS_PATH
        # The connector records the workspace against the mint (its ledger's attribution);
        # the account stays the customIdentifier it meters by.
        params = {} if workspace_id is None else {"workspace_id": workspace_id}
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout_seconds) as client:
                response = client.get(url, headers=headers, params=params)
        except httpx.HTTPError as e:
            raise TurnCredentialUnavailableError(f"could not reach the connector: {e}") from e
        return credential_from_connector_response(response.status_code, response.text, datetime.now(timezone.utc))

    def _session_with_live_token(self, user_id: str) -> AuthSession:
        """The account's session, refreshed first when its token is about to expire; raises when there is none."""
        store = self._session_store()
        typed_user_id = SuperTokensUserId(user_id)
        session = store.load_by_user_id(typed_user_id)
        if session is None:
            raise TurnCredentialUnavailableError(f"no session is signed in for account {user_id}")
        if self.imbue_cloud_cli is None or not store.is_access_token_near_expiry(session):
            return session
        try:
            self.imbue_cloud_cli.auth_refresh(str(session.email))
        except ImbueCloudCliError as e:
            raise TurnCredentialUnavailableError(f"could not refresh the session for account {user_id}: {e}") from e
        refreshed = store.load_by_user_id(typed_user_id)
        if refreshed is None:
            raise TurnCredentialUnavailableError(f"the session for account {user_id} went away while refreshing")
        return refreshed

    def _session_store(self) -> ImbueCloudSessionStore:
        try:
            profile_dir = get_active_profile_dir(self.mngr_host_dir)
        except ImbueCloudError as e:
            raise TurnCredentialUnavailableError(f"no mngr profile holds a session yet: {e}") from e
        return ImbueCloudSessionStore(sessions_dir=get_sessions_dir(profile_dir))


class TurnCredentialBroker(MutableModel):
    """Caches one credential per account and refreshes it at half life.

    A mint that fails leaves the cached credential in service until it expires, so a connector hiccup
    or a rate limit never cuts a stream that already has its relay; a refusal with nothing cached
    answers None, which the route turns into the empty list the viewer treats as normal.
    """

    client: TurnCredentialClientInterface = Field(frozen=True, description="Where credentials are minted")
    now_fn: Callable[[], datetime] = Field(
        default=lambda: datetime.now(timezone.utc), frozen=True, description="The clock; tests inject a manual one"
    )

    _credential_by_user_id: dict[str, TurnCredential] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def credential_for_account(self, user_id: str, workspace_id: str | None) -> TurnCredential | None:
        """The account's credential, minted or refreshed as its half life asks; None when none can be had.

        ``workspace_id`` (the host of the workspace asking) is attribution for the mint this call makes,
        if it makes one; the cache stays per account, so a credential serves every workspace of the account.
        """
        now = self.now_fn()
        with self._lock:
            cached = self._credential_by_user_id.get(user_id)
            if cached is not None and not is_past_half_life(cached, now):
                return cached
            fresh = self._mint_or_none(user_id, workspace_id)
            if fresh is not None:
                self._credential_by_user_id[user_id] = fresh
                return fresh
            if cached is not None and cached.expires_at > now:
                logger.debug(
                    "Serving account {}'s cached relay credential (expires at {}) since the refresh failed",
                    user_id,
                    cached.expires_at.isoformat(),
                )
                return cached
            self._credential_by_user_id.pop(user_id, None)
            return None

    def _mint_or_none(self, user_id: str, workspace_id: str | None) -> TurnCredential | None:
        try:
            minted = self.client.mint_credential(user_id, workspace_id)
        except TurnCredentialRefusedError as e:
            logger.warning(
                "Connector refused a relay credential for account {} (workspace {}): {}", user_id, workspace_id, e
            )
            return None
        except TurnCredentialUnavailableError as e:
            logger.warning(
                "Could not mint a relay credential for account {} (workspace {}): {}", user_id, workspace_id, e
            )
            return None
        logger.debug(
            "Minted a relay credential for account {} (workspace {}), expiring at {}",
            user_id,
            workspace_id,
            minted.expires_at.isoformat(),
        )
        return minted
