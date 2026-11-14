"""Plugin-side helpers for the desktop-client machine-sharing flow.

Sharing is machine-level in the self-hosted relay design: one share per
workspace host, one grants document covering the workspace plus optional
per-service scopes. The connector owns the share record + relay token
(`mngr imbue_cloud shares ...`); authorization lives in the workspace's own
grants file, which the in-workspace share-gateway re-reads on every request
(and consults only for a visitor who is not the owning account: the owner is
admitted by the broker's identity, never by a grant).

Publishing and granting are separate operations over the same two records.
Publish = connector ``shares create`` -> inject share.env into the workspace
(its share-gateway brings up caddy + frpc, and reads a grants document that
publish seeds when the workspace has none) -> the UI polls readiness by
probing the real hostname. Unpublish = clear share.env + connector ``shares
delete`` (the relay token dies, so the tunnel's next reconnect is rejected
even if the materials linger), leaving the grants document in place. Saving
grants rewrites that document alone, published or not; the gateway re-reads it
per request, so a save takes effect with no token rotation and no restart.

While the workspace is published, every save and every load of the share
panel ends in one connector call, the grants push (``shares push-grants``):
the connector records the document in its centralized grants table, mirrors
the granted accounts into the share's index and the owner's contacts, and
answers the share's status, the invitation outcomes, and the grantees'
identities. The sharing document the routes answer carries all of that, so
the panel reads nothing else.
"""

import socket
import time
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from typing import Any
from typing import Final

import httpx2
from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.profiling import get_profiler
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.api_models import SharingGrantList
from imbue.minds.desktop_client.api_models import SharingGrantsDocument
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.identity_records import IdentityCache
from imbue.minds.desktop_client.identity_records import IdentityRecord
from imbue.minds.desktop_client.identity_records import now_utc
from imbue.minds.desktop_client.identity_records import record_from_cli_identity
from imbue.minds.desktop_client.imbue_cloud_cli import ActiveShareCache
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudEmailNotVerifiedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudShareRefusedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import InvitationCliResult
from imbue.minds.desktop_client.imbue_cloud_cli import InvitationOutcomeCliEntry
from imbue.minds.desktop_client.imbue_cloud_cli import MobileAccessLinkCliResult
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ShareGrantsPushCliResult
from imbue.minds.desktop_client.provider_display import is_imbue_cloud_provider_name
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_grant_validation import GrantRefusal
from imbue.minds.desktop_client.share_grant_validation import normalized_grants_document
from imbue.minds.desktop_client.share_grant_validation import validate_grants_document
from imbue.minds.desktop_client.share_materials_injection import ShareAgentProbe
from imbue.minds.desktop_client.share_materials_injection import ShareGatewayStatus
from imbue.minds.desktop_client.share_materials_injection import ShareGatewayStatusCache
from imbue.minds.desktop_client.share_materials_injection import ShareGrantsUnreadableError
from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import ShareStateUnreadError
from imbue.minds.desktop_client.share_materials_injection import build_share_env_text
from imbue.minds.desktop_client.share_materials_injection import clear_share_publication_from_agent
from imbue.minds.desktop_client.share_materials_injection import provision_share_files_in_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_gateway_status_from_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_state_from_agent
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.share_materials_injection import render_share_identities_json
from imbue.minds.desktop_client.share_targets import WHOLE_MACHINE_SERVICE
from imbue.minds.desktop_client.share_targets import resolve_share_target_display_names
from imbue.minds.desktop_client.share_targets import resolve_share_target_labels
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.mngr.primitives import AgentId

# How long the readiness probe waits on a single fetch of the shared hostname
# before treating the share as not-ready-yet.
SHARE_READINESS_PROBE_TIMEOUT_SECONDS: Final[float] = 4.0

# How long one relay latency probe (a bare TCP connect to the relay's
# tunnel-control endpoint) may take before that relay is skipped.
RELAY_LATENCY_PROBE_TIMEOUT_SECONDS: Final[float] = 1.5

# The share states under which a share still exists for its owner: active
# (holding a max_shared_workspaces slot) or suspended (the connector's kill
# switch, which keeps the row). An inactive row is audit history at the
# connector, so there is nothing left to delete or list.
LISTED_SHARE_STATES: Final[frozenset[str]] = frozenset({"active", "suspended"})

# Signals in the plugin's JSON error body for the two failures a user can
# actually do something about. Matched on the message text because the plugin
# reports both under the same ``ImbueCloudAuthError`` class.
_EXPIRED_SESSION_SIGNALS: Final[tuple[str, ...]] = (
    "Session missing in db or has expired",
    "Refresh rejected by connector",
)
_UNVERIFIED_EMAIL_SIGNAL: Final[str] = "Email not verified"

# Workspaces created from a pre-share-gateway template (minds-v0.3.11 and
# older) have no service watching share.env, so a share enabled for them
# would go active on the connector and never become reachable. The fix is to
# update the workspace (update-self), then re-share -- which self-heals,
# since the gateway picks the materials up on its own.
# CLEANUP: drop this refusal (and the probe's has_gateway signal) once no
# supported workspaces predate the share gateway -- i.e. after the first
# post-v0.3.11 release is deployed and old workspaces have run update-self.
_PRE_GATEWAY_WORKSPACE_MESSAGE: Final[str] = (
    "This workspace's template predates the share gateway, so nothing in it would bring a share up."
)


def describe_connector_failure(exc: Exception) -> str:
    """Turn a connector failure into a sentence the user can act on.

    The plugin's own message is written for whoever is reading the logs
    ("Refresh rejected by connector: Session missing in db or has expired").
    The two failures a user can resolve get a plain sentence instead; anything
    else keeps the plugin's message, which still beats pointing at a log file.
    """
    match _account_failure_reason(exc):
        case SharingFailureReason.EMAIL_NOT_VERIFIED:
            return "Imbue Cloud has not verified this account's email address. Verify it, then retry."
        case SharingFailureReason.SIGN_IN_EXPIRED:
            return "Your Imbue Cloud session has expired. You may need to log out and log in again."
        case _:
            return str(exc)


class SharingFailureReason(UpperCaseStrEnum):
    """Why a sharing operation failed, as the share panel tells the user about it."""

    # The app has no Imbue Cloud tooling or config to share with.
    UNAVAILABLE = auto()
    # The workspace's machine is known to neither discovery nor the records.
    WORKSPACE_UNKNOWN = auto()
    # The list of signed-in accounts could not be read.
    ACCOUNTS_UNREADABLE = auto()
    # Nobody is signed in to Imbue Cloud.
    NOT_SIGNED_IN = auto()
    # The account that owns the workspace is not signed in here.
    OWNER_SIGNED_OUT = auto()
    # Someone is signed in, but the workspace is linked to no account.
    NOT_LINKED = auto()
    SIGN_IN_EXPIRED = auto()
    EMAIL_NOT_VERIFIED = auto()
    WAITLISTED = auto()
    SHARE_LIMIT_REACHED = auto()
    # Imbue Cloud failed or refused in a way no other reason names.
    CONNECTOR_FAILED = auto()
    # The check inside the workspace could not run (stopped, or not answering).
    WORKSPACE_UNREACHABLE = auto()
    # The check ran and found a workspace that predates sharing.
    WORKSPACE_TOO_OLD = auto()
    # The workspace's list of who it is shared with cannot be read back.
    GRANTS_UNREADABLE = auto()
    # Writing the share's files into the workspace failed.
    WRITE_FAILED = auto()
    # The share moved to a new address, but the workspace did not get it.
    ADDRESS_MOVE_FAILED = auto()
    # An invitation needs a published workspace.
    NOT_PUBLISHED = auto()
    # The grants document names entries that must not be granted.
    GRANTS_REFUSED = auto()
    # The workspace's template predates visiting agents.
    AGENTS_UNSUPPORTED = auto()
    # The workspace refused to invite an agent to what was asked (an app agents cannot be invited to).
    AGENT_INVITE_REFUSED = auto()
    # The visiting agent to revoke is not known to the workspace any more.
    AGENT_GONE = auto()
    # The workspace is published, but its share was never set up inside it.
    SHARE_NOT_SET_UP = auto()


class SharingError(RuntimeError):
    """Raised on a soft sharing failure.

    The message is the full account of what failed (shown behind the share
    panel's "See details"); ``reason`` is what the panel words its own
    sentence from.
    """

    def __init__(self, message: str, reason: SharingFailureReason) -> None:
        self.reason = reason
        super().__init__(message)


class ShareQuotaExceededError(SharingError):
    """Raised when Imbue Cloud refuses a share because the account's plan allows no more shared workspaces.

    Kept apart from the other soft failures so the routes can answer with a
    structured code: the fix is on the shared-workspaces page, not a retry.
    ``limit`` and ``current`` are None when the refusal did not count them.
    """

    def __init__(self, message: str, limit: int | None, current: int | None) -> None:
        self.limit = limit
        self.current = current
        super().__init__(message, SharingFailureReason.SHARE_LIMIT_REACHED)


class GrantsRefusedError(SharingError):
    """Raised when a grants document names entries that must not be granted."""

    def __init__(self, refusals: Sequence[GrantRefusal]) -> None:
        self.refusals = tuple(refusals)
        super().__init__(" ".join(refusal.message for refusal in refusals), SharingFailureReason.GRANTS_REFUSED)


class ShareMaterialsWriteError(SharingError):
    """Raised when the connector share was registered but the workspace did not receive its materials.

    The connector side has already moved on (domain minted, relay token
    rotated) while the workspace still holds whatever it had before.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, SharingFailureReason.WRITE_FAILED)


class PublicationUnknownReason(UpperCaseStrEnum):
    """Why a workspace's sharing status could not be told, which is neither shared nor unshared."""

    # Imbue Cloud, or the list of signed-in accounts, could not be read.
    UNREADABLE = auto()
    # The account that owns the workspace is not signed in here.
    OWNER_SIGNED_OUT = auto()


class SharePublicationRead(FrozenModel):
    """What the connector says about a workspace's share: active, none, or not known."""

    share: ShareCliInfo | None = Field(description="The active share; None when there is none or it is not known")
    unknown_reason: PublicationUnknownReason | None = Field(
        description="Why the publication is not known; None when the share (or its absence) was read"
    )


def connector_failure_error(context: str, exc: ImbueCloudCliError) -> SharingError:
    """The sharing error for a failed connector call, its reason read off the plugin's failure.

    ``context`` says which call failed; the plugin's own text follows it in the
    message, for the panel's details.
    """
    message = f"{context}: {exc}"
    if isinstance(exc, ImbueCloudQuotaExceededCliError):
        # A waitlisted account is refused with the quota shape and a zero cap.
        if exc.limit == 0:
            return SharingError(message, SharingFailureReason.WAITLISTED)
        return ShareQuotaExceededError(
            message,
            limit=None if exc.limit is None else int(exc.limit),
            current=None if exc.current is None else int(exc.current),
        )
    return SharingError(message, _account_failure_reason(exc) or SharingFailureReason.CONNECTOR_FAILED)


def _account_failure_reason(exc: Exception) -> SharingFailureReason | None:
    """The account problem a connector failure names that the user can resolve, or None."""
    if isinstance(exc, ImbueCloudEmailNotVerifiedCliError):
        return SharingFailureReason.EMAIL_NOT_VERIFIED
    detail = str(exc)
    if any(signal in detail for signal in _EXPIRED_SESSION_SIGNALS):
        return SharingFailureReason.SIGN_IN_EXPIRED
    if _UNVERIFIED_EMAIL_SIGNAL in detail:
        return SharingFailureReason.EMAIL_NOT_VERIFIED
    return None


def resolve_account_email_for_workspace(
    session_store: MultiAccountSessionStore | None,
    agent_id: AgentId,
) -> str:
    """Return the email of the account that owns ``agent_id``.

    Raises :class:`SharingError` if no signed-in account is associated
    with the workspace -- without an account the plugin can't make
    authenticated calls to the connector and there's nothing useful for
    the route to do. Its reason tells apart an account list that could not
    be read, an owner who is signed out, nobody signed in, and a workspace
    linked to no account.
    """
    if session_store is None:
        raise SharingError(
            "Session store unavailable; sign in to publish this workspace.", SharingFailureReason.UNAVAILABLE
        )
    account = session_store.get_account_for_workspace(str(agent_id))
    if account is not None:
        return str(account.email)
    record_store = session_store.record_store
    owner = record_store.find_active_record(str(agent_id)) if record_store is not None else None
    if owner is not None:
        # The ownership lookup read the account list; its outcome says whether
        # the owner is signed out or the list simply could not be read.
        if session_store.is_last_identity_read_failed:
            raise SharingError(
                "Could not read the signed-in accounts to find this workspace's owner.",
                SharingFailureReason.ACCOUNTS_UNREADABLE,
            )
        raise SharingError(
            f"Workspace {agent_id} belongs to an account that is not signed in here.",
            SharingFailureReason.OWNER_SIGNED_OUT,
        )
    is_anyone_signed_in = session_store.is_any_signed_in()
    if session_store.is_last_identity_read_failed:
        raise SharingError("Could not read the signed-in accounts.", SharingFailureReason.ACCOUNTS_UNREADABLE)
    if not is_anyone_signed_in:
        raise SharingError("Nobody is signed in to Imbue Cloud.", SharingFailureReason.NOT_SIGNED_IN)
    raise SharingError(
        f"Workspace {agent_id} is not associated with any signed-in account; "
        "associate one from the workspace settings page first.",
        SharingFailureReason.NOT_LINKED,
    )


def resolve_agent_for_host(
    backend_resolver: BackendResolverInterface,
    host_id: str,
    session_store: MultiAccountSessionStore | None,
) -> AgentId:
    """Resolve a machine's ``host-<hex>`` coordinate to its (primary) agent id.

    Discovery is authoritative; the workspace record store covers a stopped
    (and so undiscovered) machine, whose share can still be inspected and
    revoked. Raises :class:`SharingError` when neither knows the host.
    """
    for agent_id in backend_resolver.list_known_workspace_ids():
        display_info = backend_resolver.get_agent_display_info(agent_id)
        if display_info is not None and str(display_info.host_id) == host_id:
            return agent_id
    record_store = session_store.record_store if session_store is not None else None
    if record_store is not None:
        for records in record_store.list_all_records().values():
            for record in records:
                if record.host_id == host_id and record.state == RECORD_STATE_ACTIVE and record.agent_id:
                    return AgentId(record.agent_id)
    raise SharingError(f"No workspace is known for machine '{host_id}'.", SharingFailureReason.WORKSPACE_UNKNOWN)


def split_relay_endpoint(endpoint: str) -> tuple[str, int] | None:
    """Split a relay ``host:port`` endpoint into (host, port), or None when malformed.

    Handles bracketed IPv6 literals (``[::1]:7000``) by unwrapping the
    brackets; an unbracketed IPv6 literal has no unambiguous port split and is
    refused.
    """
    raw_host, separator, port_text = endpoint.rpartition(":")
    if not separator or not raw_host:
        return None
    if raw_host.startswith("[") and raw_host.endswith("]"):
        host = raw_host[1:-1]
    elif ":" in raw_host:
        return None
    else:
        host = raw_host
    if not host:
        return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    return (host, port)


def _measure_tcp_connect_seconds(endpoint: str, timeout_seconds: float) -> float | None:
    """Time a bare TCP connect to a ``host:port`` endpoint; None when unparseable or unreachable."""
    with get_profiler().span("sharing.relay_probe", endpoint=endpoint) as span:
        host_and_port = split_relay_endpoint(endpoint)
        if host_and_port is None:
            logger.debug("Skipping unparseable relay endpoint: {}", endpoint)
            return None
        host, port = host_and_port
        started_at = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                pass
        except OSError as exc:
            logger.debug("Relay latency probe to {} failed: {}", endpoint, exc)
            span.add_args(is_reachable=False)
            return None
        span.add_args(is_reachable=True)
        return time.monotonic() - started_at


def pick_lowest_latency_relay_region(
    relay_endpoints_by_region: Mapping[str, tuple[str, ...]],
    # Injected for tests: measures one endpoint's connect time in seconds
    # (None = unreachable). Production callers pass _measure_tcp_connect_seconds.
    measure_connect_seconds: Callable[[str], float | None],
) -> str | None:
    """The region whose fastest relay answered a TCP connect quickest, or None when none did.

    A region is scored by its best endpoint (every relay in a region shares a
    datacenter, so any reachable one represents its proximity). With a single
    region (dev tiers) the measurement is skipped entirely -- there is nothing
    to choose between.
    """
    if len(relay_endpoints_by_region) <= 1:
        return next(iter(relay_endpoints_by_region), None)
    seconds_by_region: dict[str, float] = {}
    for region, endpoints in relay_endpoints_by_region.items():
        endpoint_seconds = [
            seconds for endpoint in endpoints if (seconds := measure_connect_seconds(endpoint)) is not None
        ]
        if endpoint_seconds:
            seconds_by_region[region] = min(endpoint_seconds)
    if not seconds_by_region:
        return None
    return min(seconds_by_region, key=lambda region: seconds_by_region[region])


def _pick_preferred_relay_region(cli: ImbueCloudCli, account_email: str) -> str | None:
    """Best-effort: the lowest-latency relay region as measured from this machine.

    Used only for a first-time share of a local workspace (the workspace runs
    on this machine, so the desktop's own latency is the right proximity
    signal). Any failure degrades to None -- the connector then falls back to
    its default region.
    """
    try:
        relay_endpoints_by_region = cli.list_share_relays(account=account_email)
    except ImbueCloudCliError as exc:
        logger.debug("Could not list share relays for region picking: {}", exc)
        return None
    region = pick_lowest_latency_relay_region(
        relay_endpoints_by_region,
        lambda endpoint: _measure_tcp_connect_seconds(endpoint, RELAY_LATENCY_PROBE_TIMEOUT_SECONDS),
    )
    if region is not None:
        logger.debug("Picked relay region {} by connect latency", region)
    return region


_GRANT_LIST_KEYS: Final[tuple[str, ...]] = ("users", "emails", "email_domains")


@pure
def _granted_user_ids(grants: SharingGrantsDocument) -> list[str]:
    """Every user id granted anywhere in the document, deduplicated in first-seen order."""
    seen: dict[str, None] = {}
    for scope in (grants.workspace, *grants.services.values()):
        for user_id in scope.users:
            seen.setdefault(user_id, None)
    return list(seen)


def _fetch_identity_from_connector(
    cli: ImbueCloudCli, account_email: str | None, user_id: str
) -> IdentityRecord | None:
    """One ``users show`` lookup under the owner's account; None when the connector knows no such user."""
    if account_email is None:
        raise ImbueCloudCliError("No signed-in account to look identities up with")
    info = cli.show_user(account=account_email, user_id=user_id)
    return None if info is None else record_from_cli_identity(info)


def _resolve_grant_identities(
    user_ids: Sequence[str],
    identity_cache: IdentityCache | None,
    cli: ImbueCloudCli,
    account_email: str | None,
) -> dict[str, IdentityRecord]:
    """The identity record per granted user id, from the cache (fresh) or one connector lookup each.

    Best effort: an id the connector does not know, or a lookup that fails
    with nothing cached, is simply absent from the result; the share panel then
    renders the id itself.
    """
    with get_profiler().span("sharing.resolve_identities", user_count=len(user_ids)):
        if identity_cache is None:
            return {}
        identities: dict[str, IdentityRecord] = {}
        now = now_utc()
        for user_id in user_ids:
            try:
                record = identity_cache.get_or_fetch(
                    user_id, lambda lookup_id: _fetch_identity_from_connector(cli, account_email, lookup_id), now
                )
            except ImbueCloudCliError as exc:
                logger.debug("Could not resolve the identity of grantee {}: {}", user_id[:8], exc)
                continue
            if record is not None:
                identities[user_id] = record
        return identities


class _GrantsSync(FrozenModel):
    """What one push of the grants document to Imbue Cloud established."""

    is_synced: bool | None = Field(
        description="Whether the document reached the centralized grants table; None when nothing was pushed"
    )
    is_share_known: bool = Field(
        description=(
            "Whether the connector said what the share is: False when the push failed, or when an older "
            "connector answered without the share"
        )
    )
    share: ShareCliInfo | None = Field(
        description="The active share as the connector reports it; None when known to be unpublished, or not known"
    )
    outcomes: tuple[InvitationOutcomeCliEntry, ...] | None = Field(
        description="The granter-visible outcome per open user or email grant; None when the connector did not say"
    )
    identities: tuple[IdentityRecord, ...] | None = Field(
        description="The granted accounts' identity records; None when the connector did not say"
    )


_NOTHING_PUSHED: Final[_GrantsSync] = _GrantsSync(
    is_synced=None, is_share_known=False, share=None, outcomes=None, identities=None
)
_PUSH_FAILED: Final[_GrantsSync] = _GrantsSync(
    is_synced=False, is_share_known=False, share=None, outcomes=None, identities=None
)
# The connector's ``not_published`` refusal: the share is known to be unpublished.
_UNPUBLISHED: Final[_GrantsSync] = _GrantsSync(
    is_synced=None, is_share_known=True, share=None, outcomes=None, identities=None
)


def _sync_grants_with_connector(
    cli: ImbueCloudCli, account_email: str, host_id: str, grants: SharingGrantsDocument
) -> _GrantsSync:
    """Push the document to Imbue Cloud's centralized grants table, which invitations are made from.

    The one connector call of a save or a load while the workspace is
    published: the connector mirrors the granted accounts into the share's
    index and the owner's contacts, and answers the share's status, the
    outcomes, and the grantees' identities. The table never admits anyone, so
    a push that did not land changes no access: the document is reported as
    not yet synced, and the next save or load retries. A ``not_published``
    refusal is the connector saying the workspace is unpublished.
    """
    try:
        result = cli.push_share_grants(account=account_email, host_id=host_id, document=grants.model_dump(mode="json"))
    except ImbueCloudShareRefusedCliError as exc:
        if exc.code == "not_published":
            return _UNPUBLISHED
        logger.warning("Could not push the grants document for {} to Imbue Cloud: {}", host_id, exc)
        return _PUSH_FAILED
    except ImbueCloudCliError as exc:
        logger.warning("Could not push the grants document for {} to Imbue Cloud: {}", host_id, exc)
        return _PUSH_FAILED
    return _grants_sync_from_push(result)


def _grants_sync_from_push(result: ShareGrantsPushCliResult) -> _GrantsSync:
    # CLEANUP: drop the share-less branch (and the status read callers fall
    # back to) once every tier's connector answers the share on the push.
    return _GrantsSync(
        is_synced=True,
        is_share_known=result.share is not None,
        share=result.share,
        outcomes=result.outcomes,
        identities=(
            tuple(record_from_cli_identity(info) for info in result.identities)
            if result.identities is not None
            else None
        ),
    )


def _share_after_sync(sync: _GrantsSync, read_share: Callable[[], ShareCliInfo | None]) -> ShareCliInfo | None:
    """The share the push established, else what a status read says."""
    return sync.share if sync.is_share_known else read_share()


def _identities_after_sync(
    sync: _GrantsSync,
    identity_cache: IdentityCache | None,
    resolve_known_identities: Callable[[], dict[str, IdentityRecord]],
) -> dict[str, IdentityRecord]:
    """The granted accounts' records as the connector answered them, else as this desktop knows them.

    The connector's records go into the identity cache, so the unpublished
    reads (which push nothing) render the same names without a lookup.
    """
    if sync.identities is None:
        return resolve_known_identities()
    if identity_cache is not None:
        identity_cache.put_all(sync.identities, now_utc())
    return {record.user_id: record for record in sync.identities}


def _mark_shared_on_forward(
    agent_id: AgentId, forward_identity: ForwardIdentityPublisher | None, owner_account: AccountSession | None
) -> None:
    """Put the owner's account on the forward's local requests while the workspace is shared."""
    if forward_identity is not None and owner_account is not None:
        forward_identity.mark_shared(str(agent_id), str(owner_account.user_id))


def owner_account_for_workspace(
    session_store: MultiAccountSessionStore | None, agent_id: AgentId
) -> AccountSession | None:
    """The signed-in account associated with the workspace, or None when there is none."""
    return session_store.get_account_for_workspace(str(agent_id)) if session_store is not None else None


def require_client_env_config() -> ClientEnvConfig:
    """Resolve the client env config from the app state, or raise :class:`SharingError`.

    Reads ``get_state()``, so it MUST be called from a Flask app/request context
    (a request handler, or a worker that captured the app). Callers that run in a
    bare worker thread -- e.g. the post-create web-access enabler -- must instead
    capture the config in the request context and thread it in, since ``get_state()``
    falls back to ``current_app`` and raises "working outside of application context"
    off a request.
    """
    config = get_state().client_env_config
    if config is None:
        raise SharingError(
            "Client environment config is unavailable; cannot determine the connector URL.",
            SharingFailureReason.UNAVAILABLE,
        )
    return config


def _connector_base_url(client_env_config: ClientEnvConfig) -> str:
    return str(client_env_config.connector_url).rstrip("/")


def _is_imbue_cloud_agent(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> bool:
    """Whether the agent runs on an imbue_cloud (leased pool host) provider instance."""
    display_info = backend_resolver.get_agent_display_info(agent_id)
    provider_name = display_info.provider_name if display_info is not None else None
    return provider_name is not None and is_imbue_cloud_provider_name(provider_name)


def publish_workspace(host_id: str, backend_resolver: BackendResolverInterface) -> dict[str, Any]:
    """Publish one workspace: give it an address on the internet.

    Publishing admits nobody by itself -- the grants document is the only
    thing that admits. The full provisioning flow runs client-side for every
    row (connector ``shares create`` + materials injection over the user's own
    SSH) regardless of provider; the connector's server-side enable-sharing
    primitive is used only for web-created workspaces, which have no desktop
    client to inject from. An already-published workspace is a no-op that
    reports its current document. Returns the sharing-status document, which
    reports ``enabled`` true as soon as the connector share exists -- before
    the shared hostname answers.
    """
    state = get_state()
    cli: ImbueCloudCli | None = state.imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.", SharingFailureReason.UNAVAILABLE)
    session_store = state.session_store
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    service_labels = resolve_share_target_labels(backend_resolver, agent_id)
    # Resolved here (a request-context caller) rather than deep inside the
    # share flow, so the same helper can serve the post-create enabler, which
    # runs off a request context and must be handed the config explicitly.
    return _publish_workspace_with_cli(
        host_id,
        agent_id,
        build_agent_address(agent_id, backend_resolver),
        cli,
        account_email,
        require_client_env_config(),
        is_cloud_row=_is_imbue_cloud_agent(backend_resolver, agent_id),
        service_labels=service_labels,
        identity_cache=state.identity_cache,
        forward_identity=state.forward_identity,
        owner_account=owner_account_for_workspace(session_store, agent_id),
        grants=None,
    )


def publish_workspace_with_grants(
    host_id: str, grants: SharingGrantsDocument, backend_resolver: BackendResolverInterface
) -> dict[str, Any]:
    """Publish one workspace and store ``grants`` on it, as one operation.

    The document is checked against the one the workspace already holds before
    anything is created, and lands in the same write as share.env. Raises
    :class:`GrantsRefusedError` when it adds entries that must not be granted,
    in which case nothing is created and nothing is written.
    """
    state = get_state()
    cli: ImbueCloudCli | None = state.imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.", SharingFailureReason.UNAVAILABLE)
    session_store = state.session_store
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    return _publish_workspace_with_cli(
        host_id,
        agent_id,
        build_agent_address(agent_id, backend_resolver),
        cli,
        resolve_account_email_for_workspace(session_store, agent_id),
        require_client_env_config(),
        is_cloud_row=_is_imbue_cloud_agent(backend_resolver, agent_id),
        service_labels=resolve_share_target_labels(backend_resolver, agent_id),
        identity_cache=state.identity_cache,
        forward_identity=state.forward_identity,
        owner_account=owner_account_for_workspace(session_store, agent_id),
        grants=grants,
    )


def save_grants(
    host_id: str,
    grants: SharingGrantsDocument,
    backend_resolver: BackendResolverInterface,
) -> dict[str, Any]:
    """Replace one workspace's grants document, whether or not it is published.

    Raises :class:`GrantsRefusedError` when the document adds entries that must
    not be granted (nothing is written then), and :class:`SharingError` when
    the workspace cannot take the write. Returns the sharing-status document
    carrying the saved grants.
    """
    state = get_state()
    cli: ImbueCloudCli | None = state.imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.", SharingFailureReason.UNAVAILABLE)
    session_store = state.session_store
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    return _save_grants_with_cli(
        host_id,
        build_agent_address(agent_id, backend_resolver),
        grants,
        cli,
        resolve_account_email_for_workspace(session_store, agent_id),
        service_labels=resolve_share_target_labels(backend_resolver, agent_id),
        identity_cache=state.identity_cache,
    )


@pure
def _render_grants_document_toml(grants: SharingGrantsDocument) -> str:
    """The document in the TOML the workspace gateway reads."""
    return render_grants_toml(
        _grant_list_to_plain(grants.workspace),
        {name: _grant_list_to_plain(entry) for name, entry in grants.services.items()},
    )


@pure
def _grant_list_to_plain(entry: SharingGrantList) -> dict[str, list[str]]:
    return {"users": list(entry.users), "emails": list(entry.emails), "email_domains": list(entry.email_domains)}


def _publish_workspace_with_cli(
    host_id: str,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    cli: ImbueCloudCli,
    account_email: str,
    # Passed in (not resolved via ``get_state()`` here) so this can run off a
    # request context -- the post-create web-access enabler runs in a worker
    # thread where ``current_app`` is unbound.
    client_env_config: ClientEnvConfig,
    # True for imbue_cloud (leased pool host) rows. The bring-up path is the
    # same client-side one for every row; this only disables the relay-region
    # latency measurement, whose desktop-proximity signal is only meaningful
    # for a workspace that runs on this machine.
    is_cloud_row: bool,
    # The label per share target as known right now. The shell's is recorded
    # server-side as the share's entry origin, and the whole map rides the
    # returned document so the panel builds links from the same labels the
    # connector was told about, or shows a pending state for the ones it lacks.
    service_labels: Mapping[str, str],
    # The desktop's identity state: the cache renders user-id grants with a
    # name, the publisher puts the owner's account on the forward's local
    # requests while the workspace is shared. None disables either.
    identity_cache: IdentityCache | None,
    forward_identity: ForwardIdentityPublisher | None,
    owner_account: AccountSession | None,
    # A granter's document to store as part of this publish. None publishes
    # whatever the workspace already grants.
    grants: SharingGrantsDocument | None,
) -> dict[str, Any]:
    with get_profiler().span("sharing.publish", host_id=host_id, is_cloud_row=is_cloud_row) as span:
        # One exec answers everything the flow needs from the workspace: whether
        # the template ships the share gateway, whether share.env is present, and
        # the current grants document.
        probe = _probe_workspace_that_can_share(agent_address, cli)
        stored_grants = _readable_grants_document(probe.grants_toml_text)

        # What this publish writes, if anything. With no document given the
        # workspace keeps the one it holds -- unless nothing there can be parsed,
        # which counts as holding none: the gateway refuses every request when it
        # cannot read a document, so an empty one is written for it.
        if grants is not None:
            refusals = validate_grants_document(grants, account_email, stored_grants or SharingGrantsDocument())
            if refusals:
                raise GrantsRefusedError(refusals)
            grants_to_store: SharingGrantsDocument | None = normalized_grants_document(grants)
        elif stored_grants is None:
            grants_to_store = SharingGrantsDocument()
        else:
            grants_to_store = None
        grants_toml = _render_grants_document_toml(grants_to_store) if grants_to_store is not None else None
        # What the file holds after this publish: what is about to be written, or the one already there.
        saved_grants = grants_to_store if grants_to_store is not None else (stored_grants or SharingGrantsDocument())
        span.add_args(is_first_publish=not probe.has_share_env)
        user_ids = _granted_user_ids(saved_grants)
        identities = _resolve_grant_identities(user_ids, identity_cache, cli, account_email)
        identities_json = render_share_identities_json(identities) if grants_toml is not None else None

        # Materials present means the workspace is published already or carries
        # stale ones from a share disabled elsewhere (re-provision). Only this path
        # needs the connector's status; the common publish-from-off path never reads
        # it -- create is the source of truth there.
        existing = _read_active_share(cli, account_email, host_id) if probe.has_share_env else None
        if existing is not None:
            share = existing
            if grants_toml is not None:
                _write_grants_document(agent_address, grants_toml, identities_json, cli)
        else:
            # Local shares with no materials in the workspace pick the relay by
            # measured latency from here (the workspace runs on this machine, so
            # the desktop's latency is the right proximity signal). A cloud row
            # runs elsewhere, and a stale-materials re-provision is already placed,
            # so both skip the measurement. The preference is advisory anyway: the
            # connector honors it only for hosts it has no region record of.
            is_relay_region_measured = not is_cloud_row and not probe.has_share_env
            share = _create_share_and_write_materials(
                host_id,
                agent_id,
                agent_address,
                cli,
                account_email,
                client_env_config,
                preferred_region=_pick_preferred_relay_region(cli, account_email)
                if is_relay_region_measured
                else None,
                service_labels=service_labels,
                grants_toml_text=grants_toml,
                identities_json_text=identities_json,
            )

        _mark_shared_on_forward(agent_id, forward_identity, owner_account)
        # The create (or the status read above) is the authority on the share, not the push.
        sync = _sync_grants_with_connector(cli, account_email, host_id, saved_grants)
        return _share_status_document(
            host_id,
            share,
            saved_grants,
            service_labels,
            _identities_after_sync(sync, identity_cache, lambda: identities),
            sync.is_synced,
            sync.outcomes,
        )


def _save_grants_with_cli(
    host_id: str,
    agent_address: str,
    grants: SharingGrantsDocument,
    cli: ImbueCloudCli,
    account_email: str,
    service_labels: Mapping[str, str],
    identity_cache: IdentityCache | None,
) -> dict[str, Any]:
    with get_profiler().span("sharing.save_grants", host_id=host_id) as span:
        # One read answers everything this save needs of the workspace: whether the
        # gateway is there to read what it writes, and which of the entries being
        # saved are new (only those face the rules).
        probe = _probe_workspace_that_can_share(agent_address, cli)
        stored_grants = _readable_grants_document(probe.grants_toml_text) or SharingGrantsDocument()
        refusals = validate_grants_document(grants, account_email, stored_grants)
        if refusals:
            raise GrantsRefusedError(refusals)
        saved_grants = normalized_grants_document(grants)
        user_ids = _granted_user_ids(saved_grants)
        span.add_args(user_count=len(user_ids))
        identities = _resolve_grant_identities(user_ids, identity_cache, cli, account_email)
        _write_grants_document(
            agent_address, _render_grants_document_toml(saved_grants), render_share_identities_json(identities), cli
        )
        # An unpublished workspace's save never reaches the connector: the next
        # publish pushes whatever the document holds by then.
        if not probe.has_share_env:
            return _share_status_document(host_id, None, saved_grants, service_labels, identities, None, None)
        # Materials present means the workspace believes itself published; the push says what the share is.
        sync = _sync_grants_with_connector(cli, account_email, host_id, saved_grants)
        return _share_status_document(
            host_id,
            _share_after_sync(sync, lambda: _read_active_share(cli, account_email, host_id)),
            saved_grants,
            service_labels,
            _identities_after_sync(sync, identity_cache, lambda: identities),
            sync.is_synced,
            sync.outcomes,
        )


def _probe_share_state(agent_address: str, cli: ImbueCloudCli) -> ShareAgentProbe:
    """The workspace's share state; raises :class:`SharingError` when the probe could not run or read the grants."""
    try:
        return read_share_state_from_agent(agent_address, cli.mngr_caller)
    except ShareStateUnreadError as exc:
        raise SharingError(str(exc), SharingFailureReason.WORKSPACE_UNREACHABLE) from exc
    except ShareGrantsUnreadableError as exc:
        raise SharingError(str(exc), SharingFailureReason.GRANTS_UNREADABLE) from exc


def _probe_workspace_that_can_share(agent_address: str, cli: ImbueCloudCli) -> ShareAgentProbe:
    """The share state of a workspace that has the share gateway; refuses one too old to be shared."""
    probe = _probe_share_state(agent_address, cli)
    if not probe.has_gateway:
        raise SharingError(_PRE_GATEWAY_WORKSPACE_MESSAGE, SharingFailureReason.WORKSPACE_TOO_OLD)
    return probe


def _write_grants_document(
    agent_address: str, grants_toml: str, identities_json: str | None, cli: ImbueCloudCli
) -> None:
    try:
        provision_share_files_in_agent(agent_address, grants_toml, identities_json, None, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc), SharingFailureReason.WRITE_FAILED) from exc


def _readable_grants_document(grants_toml_text: str | None) -> SharingGrantsDocument | None:
    """The document the workspace holds; None when it holds none, or one nothing can parse.

    An unparseable document is deliberately indistinguishable from an absent one
    (the parse logs a warning), so it is replaced rather than left to wedge the
    workspace.
    """
    if not grants_toml_text:
        return None
    parsed = _parse_grants_toml(grants_toml_text)
    if parsed is None:
        return None
    workspace_grants, service_grants = parsed
    return SharingGrantsDocument(
        workspace=SharingGrantList.model_validate(workspace_grants),
        services={name: SharingGrantList.model_validate(entry) for name, entry in service_grants.items()},
    )


def does_grants_document_admit_anyone(grants_toml_text: str | None) -> bool:
    """Whether a grants document read back from the workspace names any user, email, or domain, at any scope.

    An absent or unparseable document admits nobody, as :func:`_readable_grants_document` reads it.
    """
    grants = _readable_grants_document(grants_toml_text)
    if grants is None:
        return False
    return any(
        scope.users or scope.emails or scope.email_domains for scope in (grants.workspace, *grants.services.values())
    )


def _read_active_share(cli: ImbueCloudCli, account_email: str, host_id: str) -> ShareCliInfo | None:
    """The machine's connector share when it is active, else None.

    Raises :class:`SharingError` when the connector cannot be reached: a
    workspace's publication state is never guessed.
    """
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise connector_failure_error("Could not read the workspace's sharing status", exc) from exc
    return share if share is not None and share.state == "active" else None


def _read_share_publication_for_account(cli: ImbueCloudCli, account_email: str, host_id: str) -> SharePublicationRead:
    """The machine's active connector share, no share, or (a connector failure, logged) unknown."""
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to read share status for {}: {}", host_id, exc)
        return SharePublicationRead(share=None, unknown_reason=PublicationUnknownReason.UNREADABLE)
    if share is None or share.state != "active":
        return SharePublicationRead(share=None, unknown_reason=None)
    return SharePublicationRead(share=share, unknown_reason=None)


def _create_share_and_write_materials(
    host_id: str,
    agent_id: AgentId,
    agent_address: str,
    cli: ImbueCloudCli,
    account_email: str,
    client_env_config: ClientEnvConfig,
    preferred_region: str | None,
    service_labels: Mapping[str, str],
    # None leaves the grants document the workspace already holds untouched
    # (a stale-domain repair); otherwise it lands before share.env.
    grants_toml_text: str | None,
    # The grantees' identities, landing beside the grants document; None whenever that is None.
    identities_json_text: str | None,
) -> ShareCliInfo:
    """Register (or re-register) the share with the connector and inject its materials in one exec.

    The share create is where the connector mints the domain -- a fresh one
    for a first share, the stored one for a re-share, or a new one when the
    stored domain sits on a content domain the tier moved away from.
    """
    try:
        share = cli.create_share(
            account=account_email,
            host_id=host_id,
            # The chrome can only enter the workspace at <label>.<domain> (the
            # bare domain is unrouted on the relay); None when the shell has
            # not registered yet.
            entry_label=service_labels.get(WHOLE_MACHINE_SERVICE),
            preferred_region=preferred_region,
            workspace_id=str(agent_id),
        )
    except ImbueCloudCliError as exc:
        raise connector_failure_error("Could not publish this workspace", exc) from exc
    if share.relay_token is None:
        raise SharingError(
            "The workspace was published but the connector did not return a relay token.",
            SharingFailureReason.CONNECTOR_FAILED,
        )

    share_env_text = build_share_env_text(
        workspace_domain=share.workspace_domain,
        relay_token=share.relay_token.get_secret_value(),
        connector_url=_connector_base_url(client_env_config),
        broker_url=client_env_config.accounts_origin_url(),
        # The connector reports the tier's chrome origin on the create (its own
        # SHARE_CHROME_ORIGIN -- the same value web-created workspaces get), so
        # desktop shares admit the real /web chrome even on tiers where it
        # lives on a custom domain (deploy.toml [origins].chrome_origin). The
        # fallback covers an old connector or a tier with none configured:
        # there the chrome is path-served on the bare connector origin.
        chrome_origin=share.chrome_origin or _connector_base_url(client_env_config),
    )
    # Everything lands in one exec, share.env last -- the gateway brings the
    # stack up the moment it appears, so the grants must already be in place.
    try:
        provision_share_files_in_agent(
            agent_address, grants_toml_text, identities_json_text, share_env_text, cli.mngr_caller
        )
    except ShareInjectionError as exc:
        raise ShareMaterialsWriteError(str(exc)) from exc
    return share


def migrate_stale_share(
    host_id: str,
    agent_id: AgentId,
    agent_address: str,
    # The active share as the connector reports it, still on the retired domain.
    stale_share: ShareCliInfo,
    # The workspace's share state, as the read that found the share stale saw it.
    probe: ShareAgentProbe,
    cli: ImbueCloudCli,
    account_email: str,
    client_env_config: ClientEnvConfig,
    service_labels: Mapping[str, str],
    identity_cache: IdentityCache | None,
    forward_identity: ForwardIdentityPublisher | None,
    owner_account: AccountSession | None,
) -> dict[str, Any]:
    """Move a share the tier left on a previous content domain onto the current one.

    A re-share that rewrites only ``share.env``: the connector mints the new
    domain, the workspace's gateway restarts on the new materials, and the
    grants document is left exactly as the workspace holds it, so nobody's
    access changes with the address. Raises :class:`SharingError` when the
    workspace cannot take the move; the panel then shows it with a retry.
    """
    if not probe.has_gateway:
        raise SharingError(_PRE_GATEWAY_WORKSPACE_MESSAGE, SharingFailureReason.WORKSPACE_TOO_OLD)
    # The move rewrites share.env alone and leaves the document exactly as it
    # is, so a document nothing can parse costs the move nothing; it is read
    # for the push that answers what the panel shows of its grantees.
    grants = _readable_grants_document(probe.grants_toml_text) or SharingGrantsDocument()
    try:
        share = _create_share_and_write_materials(
            host_id,
            agent_id,
            agent_address,
            cli,
            account_email,
            client_env_config,
            preferred_region=None,
            service_labels=service_labels,
            grants_toml_text=None,
            identities_json_text=None,
        )
    except ShareMaterialsWriteError as exc:
        # The connector no longer flags the share, so the panel's retry (a
        # plain read) will not inject again: only a re-publish does.
        raise SharingError(
            "Sharing was moved to a new address, but this workspace did not receive the new share materials, so "
            f"its shared links stay down until publishing is turned off and on again for it: {exc}",
            SharingFailureReason.ADDRESS_MOVE_FAILED,
        ) from exc
    logger.info("Moved sharing for {} from {} to {}", host_id, stale_share.workspace_domain, share.workspace_domain)
    _mark_shared_on_forward(agent_id, forward_identity, owner_account)
    sync = _sync_grants_with_connector(cli, account_email, host_id, grants)
    identities = _identities_after_sync(
        sync,
        identity_cache,
        lambda: _resolve_grant_identities(_granted_user_ids(grants), identity_cache, cli, account_email),
    )
    document = _share_status_document(
        host_id, share, grants, service_labels, identities, sync.is_synced, sync.outcomes
    )
    document["migrated_domain_from"] = stale_share.workspace_domain
    return document


def enable_web_access_for_workspace(
    agent_id: AgentId,
    host_id: str,
    is_cloud_row: bool,
    cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore | None,
    backend_resolver: BackendResolverInterface,
    # Captured by the caller in a request context and threaded in: this runs
    # in the post-create worker thread, where ``get_state()`` cannot resolve
    # ``current_app``.
    client_env_config: ClientEnvConfig,
    identity_cache: IdentityCache | None,
    forward_identity: ForwardIdentityPublisher | None,
) -> None:
    """Publish a just-created workspace so it is reachable from /web.

    The create form's "Enable sharing and web access" toggle: every row -- cloud and local
    alike -- runs the desktop publish flow, which grants nobody. The owner can
    open what they just created regardless: the in-workspace gateway admits the
    owning account without consulting the grants document at all. (The
    connector's server-side enable-sharing primitive is used only for
    web-created workspaces, which have no desktop to inject from.) Raises
    :class:`SharingError` when the workspace has no associated account or the
    bring-up fails.
    """
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    service_labels = resolve_share_target_labels(backend_resolver, agent_id)
    _publish_workspace_with_cli(
        host_id,
        agent_id,
        build_agent_address(agent_id, backend_resolver),
        cli,
        account_email,
        client_env_config,
        is_cloud_row=is_cloud_row,
        service_labels=service_labels,
        identity_cache=identity_cache,
        forward_identity=forward_identity,
        owner_account=owner_account_for_workspace(session_store, agent_id),
        grants=None,
    )


def _share_status_document(
    host_id: str,
    # None when the workspace is unpublished: it has no address, but its grants
    # document is still what the panel lists.
    share: ShareCliInfo | None,
    grants: SharingGrantsDocument,
    service_labels: Mapping[str, str],
    identities: Mapping[str, IdentityRecord],
    # Whether the document reached Imbue Cloud's centralized grants table;
    # None while unpublished (nothing is pushed) or when no push was attempted.
    grants_synced: bool | None,
    # The granter-visible outcome per open user or email grant, as the push
    # answered it; None when nothing was pushed or the connector did not say.
    invitation_outcomes: Sequence[InvitationOutcomeCliEntry] | None,
) -> dict[str, Any]:
    workspace_domain = share.workspace_domain if share is not None else None
    return {
        "host_id": host_id,
        "enabled": share is not None and share.state == "active",
        "workspace_domain": workspace_domain,
        # The bare domain is deliberately unrouted on a share; a target's link
        # is https://<service_labels[target]>.<workspace_domain>/.
        "url": f"https://{workspace_domain}/" if workspace_domain else None,
        "region": share.region if share is not None else None,
        "last_tunnel_login_at": share.last_tunnel_login_at if share is not None else None,
        "cert_not_after": share.cert_not_after if share is not None else None,
        "service_labels": dict(service_labels),
        "grants": grants.model_dump(mode="json"),
        # The record per granted user id the desktop knows, so the share panel
        # renders a name, email, and profile picture instead of a bare id.
        "identities": {user_id: record.model_dump(mode="json") for user_id, record in identities.items()},
        "grants_synced": grants_synced,
        "invitation_outcomes": (
            [entry.model_dump(mode="json") for entry in invitation_outcomes]
            if invitation_outcomes is not None
            else None
        ),
    }


def _empty_grant_list() -> dict[str, list[str]]:
    return {"users": [], "emails": [], "email_domains": []}


def _parse_grant_list(value: object) -> dict[str, list[str]]:
    """Coerce one grants scope read back from the workspace into ``{users, emails, email_domains}``.

    ``users`` round-trips verbatim: the gateway writes user ids into it when
    it upgrades an invite, and a save that dropped them would revoke access.
    """
    if not isinstance(value, dict):
        return _empty_grant_list()
    entries: dict[str, object] = {str(key): entry for key, entry in value.items()}
    parsed: dict[str, list[str]] = {}
    for key in _GRANT_LIST_KEYS:
        raw = entries.get(key)
        parsed[key] = [str(entry) for entry in raw] if isinstance(raw, list) else []
    return parsed


def _parse_grants_toml(
    grants_toml_text: str,
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]] | None:
    """Parse a grants document read back from the workspace; None when malformed."""
    try:
        raw = tomllib.loads(grants_toml_text)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Malformed grants document read back from the workspace: {}", exc)
        return None

    workspace_grants = _parse_grant_list(raw.get("workspace"))
    raw_services = raw.get("services", {})
    service_grants = (
        {str(name): _parse_grant_list(value) for name, value in raw_services.items()}
        if isinstance(raw_services, dict)
        else {}
    )
    return workspace_grants, service_grants


def get_sharing(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
    identity_cache: IdentityCache | None,
    # Needed only to repair a share left on a retired content domain (the
    # connector and broker URLs stamped into the new share.env); None makes
    # such a repair fail with the config error the enable path reports.
    client_env_config: ClientEnvConfig | None,
    forward_identity: ForwardIdentityPublisher | None,
) -> dict[str, Any]:
    """Return the machine's sharing document: publication status + the grants read from the workspace.

    The grants are read whether or not the workspace is published: unpublishing
    keeps the document, so the panel goes on listing it (and removing from it)
    while off. The document also carries the current origin label per share
    target, from which the panel builds every link (a target absent from it has
    no link yet). One exec reads the workspace's share state; while it holds
    share materials, one grants push is the only connector call, and it answers
    the share, the invitation outcomes, and the grantees' identities (spec P7:
    every load pushes). A share the connector flags ``needs_reshare`` (active on
    a content domain the tier moved away from) is repaired here, on the read,
    so opening the share panel is what moves it; that read raises
    :class:`SharingError` when the move fails. ``publication_unknown_reason``
    is set when the share could not be told apart from no share (see
    :func:`read_share_publication`); ``enabled`` is then false but not known.
    """
    with get_profiler().span("sharing.get_sharing", host_id=host_id) as span:
        # Discovery is concurrently updated, so the coordinate can be unresolvable;
        # degrade to UNKNOWN grants rather than failing the whole read. The grants
        # must not degrade to "empty": the pane would render every grantee as
        # revoked, and a save from that state would replace a policy nobody ever saw.
        try:
            agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
        except SharingError as exc:
            logger.debug("Sharing grants read: {}", exc)
            return _with_publication_unknown_reason(
                _unknown_grants_document(host_id, None, {}), _publication_unknown_reason_for(exc.reason)
            )
        if cli is None:
            return _with_publication_unknown_reason(
                _unknown_grants_document(host_id, None, {}), PublicationUnknownReason.UNREADABLE
            )
        service_labels = resolve_share_target_labels(backend_resolver, agent_id)
        agent_address = build_agent_address(agent_id, backend_resolver)
        account_email: str | None = None
        publication_without_account = SharePublicationRead(share=None, unknown_reason=None)
        try:
            account_email = resolve_account_email_for_workspace(session_store, agent_id)
        except SharingError as exc:
            logger.debug("No account to ask Imbue Cloud about {} with: {}", agent_id, exc)
            publication_without_account = SharePublicationRead(
                share=None, unknown_reason=_publication_unknown_reason_for(exc.reason)
            )
        try:
            probe = read_share_state_from_agent(agent_address, cli.mngr_caller)
        except ShareInjectionError as exc:
            logger.debug("Sharing grants read: {}", exc)
            publication_without_grants = (
                _read_share_publication_for_account(cli, account_email, host_id)
                if account_email is not None
                else publication_without_account
            )
            return _with_publication_unknown_reason(
                _unknown_grants_document(host_id, publication_without_grants.share, service_labels),
                publication_without_grants.unknown_reason,
            )
        # A document nothing can parse reads back as an empty one; ``grants: null``
        # is reserved for a read that never landed, above.
        grants = _readable_grants_document(probe.grants_toml_text) or SharingGrantsDocument()
        if account_email is None:
            # Without a signed-in account there is nothing to ask the connector with.
            return _with_publication_unknown_reason(
                _share_status_document(host_id, None, grants, service_labels, {}, None, None),
                publication_without_account.unknown_reason,
            )
        # Materials present means the workspace believes itself published, so the
        # push says what the share is. Without them the status read does, since a
        # share can be active on the connector with the materials gone (a publish
        # that failed between the create and the injection); such a share still
        # gets its document pushed.
        if probe.has_share_env:
            sync = _sync_grants_with_connector(cli, account_email, host_id, grants)
            publication = (
                SharePublicationRead(share=sync.share, unknown_reason=None)
                if sync.is_share_known
                else _read_share_publication_for_account(cli, account_email, host_id)
            )
        else:
            publication = _read_share_publication_for_account(cli, account_email, host_id)
            sync = (
                _sync_grants_with_connector(cli, account_email, host_id, grants)
                if publication.share is not None
                else _NOTHING_PUSHED
            )
        share = publication.share
        span.add_args(is_published=share is not None)
        if share is not None and share.needs_reshare:
            if client_env_config is None:
                raise SharingError(
                    "Client environment config is unavailable; cannot move sharing to its new address.",
                    SharingFailureReason.UNAVAILABLE,
                )
            return migrate_stale_share(
                host_id,
                agent_id,
                agent_address,
                share,
                probe,
                cli,
                account_email,
                client_env_config,
                service_labels,
                identity_cache,
                forward_identity,
                owner_account_for_workspace(session_store, agent_id),
            )
        identities = _identities_after_sync(
            sync,
            identity_cache,
            lambda: _resolve_grant_identities(_granted_user_ids(grants), identity_cache, cli, account_email),
        )
        return _with_publication_unknown_reason(
            _share_status_document(host_id, share, grants, service_labels, identities, sync.is_synced, sync.outcomes),
            publication.unknown_reason,
        )


def _with_publication_unknown_reason(
    document: dict[str, Any], unknown_reason: PublicationUnknownReason | None
) -> dict[str, Any]:
    """The sharing document, saying why its publication is not known (None when it is): an unknown share is not drawn as unshared."""
    document["publication_unknown_reason"] = unknown_reason
    return document


def _unknown_grants_document(
    host_id: str, share: ShareCliInfo | None, service_labels: Mapping[str, str]
) -> dict[str, Any]:
    """The machine's document with ``grants: None``: the read never landed (not "the workspace grants nobody")."""
    document = _share_status_document(host_id, share, SharingGrantsDocument(), service_labels, {}, None, None)
    document["grants"] = None
    return document


def resolve_share_target_labels_for_host(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    host_id: str,
) -> dict[str, str]:
    """The label per share target of the machine's workspace; empty when the machine is unknown.

    Empty means no target has a link that can be shown yet (the workspace's
    service registrations have not reached this client, or the machine is not
    discovered at all), never that the share is unlabeled.
    """
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    except SharingError as exc:
        logger.debug("Cannot resolve share target labels for {}: {}", host_id, exc)
        return {}
    return resolve_share_target_labels(backend_resolver, agent_id)


def get_active_share_cached(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
    cache: ActiveShareCache,
) -> ShareCliInfo | None:
    """:func:`get_active_share` behind the short-TTL cache (the readiness poll's read path)."""
    cached = cache.get(host_id)
    if cached is not None:
        return cached.value
    share = get_active_share(host_id, backend_resolver, cli, session_store)
    cache.put(host_id, share)
    return share


def get_active_share(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> ShareCliInfo | None:
    """The machine's active connector share, or None (unshared, unresolvable, or connector error).

    Reads only the connector-side share status -- no exec into the workspace --
    so polling callers (the readiness probe) stay cheap on remote hosts.
    """
    return read_share_publication(host_id, backend_resolver, cli, session_store).share


def read_share_publication(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> SharePublicationRead:
    """The machine's active connector share, no share, or why neither could be told.

    A workspace linked to no account, or with nobody signed in, has no share
    anyone here could have made, so it reads as unshared; an owner who is
    signed out, an account list or a connector that could not be read, leave
    the share unknown.
    """
    if cli is None:
        return SharePublicationRead(share=None, unknown_reason=PublicationUnknownReason.UNREADABLE)
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
        account_email = resolve_account_email_for_workspace(session_store, agent_id)
    except SharingError as exc:
        logger.debug("Sharing status: {}", exc)
        return SharePublicationRead(share=None, unknown_reason=_publication_unknown_reason_for(exc.reason))
    return _read_share_publication_for_account(cli, account_email, host_id)


@pure
def _publication_unknown_reason_for(reason: SharingFailureReason) -> PublicationUnknownReason | None:
    """What a failure to find the share's account leaves the publication as: unknown, or (None) plainly unshared."""
    match reason:
        case SharingFailureReason.NOT_LINKED | SharingFailureReason.NOT_SIGNED_IN:
            return None
        case SharingFailureReason.OWNER_SIGNED_OUT:
            return PublicationUnknownReason.OWNER_SIGNED_OUT
        case _:
            return PublicationUnknownReason.UNREADABLE


def get_share_gateway_status_cached(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
    cache: ShareGatewayStatusCache,
) -> ShareGatewayStatus | None:
    """The workspace gateway's own bring-up status (why the share is not live yet), behind the short-TTL cache.

    None when the machine cannot be resolved, the read fails, or the
    workspace reports nothing -- the readiness poll then simply carries no
    explanation.
    """
    cached = cache.get(host_id)
    if cached is not None:
        return cached.value
    if cli is None:
        return None
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    except SharingError as exc:
        logger.debug("Cannot read the share gateway status for {} yet: {}", host_id, exc)
        return None
    status = read_share_gateway_status_from_agent(build_agent_address(agent_id, backend_resolver), cli.mngr_caller)
    cache.put(host_id, status)
    return status


def unpublish_workspace(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
    forward_identity: ForwardIdentityPublisher | None,
) -> None:
    """Take a workspace off the internet: clear share.env, then delete the connector share.

    The grants document stays where it is, so the panel goes on showing the
    list while publishing is off and the next publish admits the same people
    without them being re-added. Idempotent: an already-unpublished workspace
    is a success. Raises :class:`SharingError` on a missing CLI, no associated
    account, or a connector error.
    """
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured.", SharingFailureReason.UNAVAILABLE)
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    clear_share_publication_from_agent(build_agent_address(agent_id, backend_resolver), cli.mngr_caller)
    if forward_identity is not None:
        forward_identity.mark_unshared(str(agent_id))
    try:
        existing = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise connector_failure_error("Could not read the workspace's sharing status", exc) from exc
    if existing is not None and existing.state in LISTED_SHARE_STATES:
        try:
            cli.delete_share(account=account_email, host_id=host_id)
        except ImbueCloudCliError as exc:
            raise connector_failure_error("Could not unpublish this workspace", exc) from exc
    # Only now does a sync pass agree that the workspace is unshared: a pass
    # that listed the share before the delete would re-add the entry
    # `mark_unshared` removed and leave it there until the next tick.
    if forward_identity is not None:
        forward_identity.request_sync()


def delete_share_for_host(cli: ImbueCloudCli | None, account_email: str, host_id: str) -> None:
    """Delete the account's machine share for ``host_id``, if it has an active one.

    A share left behind keeps a relay hostname reserved and counts against the
    account's shared-machine quota, which would become a ceiling on machines
    ever created rather than on live ones.

    Never raises: this runs on teardown paths (unlinking, destroy
    finalization) where a connector hiccup must not block retiring the
    workspace. A share that survives is litter; a workspace that cannot be
    retired is a stuck UI.
    """
    if cli is None or not host_id.startswith("host-"):
        return
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
        if share is not None and share.state == "active":
            cli.delete_share(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to delete the machine share for {}: {}", host_id, exc)


def _workspace_display_name(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str | None:
    """The workspace's own display name, never the agent's internal name (``system-services`` on every workspace)."""
    return backend_resolver.get_workspace_name(agent_id) or None


def _push_current_document(cli: ImbueCloudCli, account_email: str, host_id: str, agent_address: str) -> None:
    """Push the document the workspace holds right now, so an invitation refused as out of date can be tried again."""
    probe = _probe_share_state(agent_address, cli)
    _sync_grants_with_connector(
        cli, account_email, host_id, _readable_grants_document(probe.grants_toml_text) or SharingGrantsDocument()
    )


class _InvitationAttempt(FrozenModel):
    """Everything one invitation asks of the connector, so a retry sends exactly the same thing."""

    account_email: str
    host_id: str
    user_id: str | None
    email: str | None
    app: str | None
    link: str | None
    workspace_name: str | None
    app_display_name: str | None


def invite_grantee_for_workspace(
    host_id: str,
    user_id: str | None,
    email: str | None,
    # The share target the row is on; None for the whole workspace.
    app: str | None,
    backend_resolver: BackendResolverInterface,
) -> InvitationCliResult:
    """Invite one grant's grantee through the connector and return what the granter may learn.

    The invitation carries the target's own link, built from the labels the
    workspace has registered, the workspace's display name, and the name the
    share panel shows for the app, so that the mail names it the way the panel
    does rather than by the service name behind its hostname. A refusal as
    ``grants_out_of_date`` is answered by pushing the workspace's document and
    trying once more; every other refusal propagates as
    :class:`ImbueCloudShareRefusedCliError` for the route to report by code.
    Raises :class:`SharingError` when the workspace is unpublished or the
    connector cannot be reached.
    """
    with get_profiler().span("sharing.invite", host_id=host_id):
        state = get_state()
        cli: ImbueCloudCli | None = state.imbue_cloud_cli
        if cli is None:
            raise SharingError("imbue_cloud CLI is not configured on this app.", SharingFailureReason.UNAVAILABLE)
        session_store = state.session_store
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
        account_email = resolve_account_email_for_workspace(session_store, agent_id)
        share = _read_active_share(cli, account_email, host_id)
        if share is None:
            raise SharingError(
                "Publish this workspace before inviting anyone: the invitation carries its link.",
                SharingFailureReason.NOT_PUBLISHED,
            )
        target = app if app else WHOLE_MACHINE_SERVICE
        label = resolve_share_target_labels(backend_resolver, agent_id).get(target)
        attempt = _InvitationAttempt(
            account_email=account_email,
            host_id=host_id,
            user_id=user_id,
            email=email,
            app=None if target == WHOLE_MACHINE_SERVICE else target,
            link=f"https://{label}.{share.workspace_domain}/" if label else None,
            workspace_name=_workspace_display_name(backend_resolver, agent_id),
            app_display_name=resolve_share_target_display_names(backend_resolver, agent_id).get(target),
        )
        return _invite_with_one_retry(cli, attempt, build_agent_address(agent_id, backend_resolver))


def _invite_with_one_retry(cli: ImbueCloudCli, attempt: _InvitationAttempt, agent_address: str) -> InvitationCliResult:
    """Invite; a refusal as ``grants_out_of_date`` pushes the workspace's document and tries once more."""
    try:
        return _invite_through_cli(cli, attempt)
    except ImbueCloudShareRefusedCliError as exc:
        if exc.code != "grants_out_of_date":
            raise
        _push_current_document(cli, attempt.account_email, attempt.host_id, agent_address)
        try:
            with get_profiler().span("sharing.invite_retry", host_id=attempt.host_id):
                return _invite_through_cli(cli, attempt)
        except ImbueCloudShareRefusedCliError:
            raise
        except ImbueCloudCliError as retry_exc:
            raise connector_failure_error("Could not invite", retry_exc) from retry_exc
    except ImbueCloudCliError as exc:
        raise connector_failure_error("Could not invite", exc) from exc


def _invite_through_cli(cli: ImbueCloudCli, attempt: _InvitationAttempt) -> InvitationCliResult:
    return cli.invite_grantee(
        account=attempt.account_email,
        host_id=attempt.host_id,
        user_id=attempt.user_id,
        email=attempt.email,
        app=attempt.app,
        link=attempt.link,
        workspace_name=attempt.workspace_name,
        app_display_name=attempt.app_display_name,
    )


def send_mobile_access_link_for_workspace(
    host_id: str, backend_resolver: BackendResolverInterface
) -> MobileAccessLinkCliResult:
    """Have Imbue Cloud email the signed-in account the link to this workspace, and report how it went.

    The message carries the whole workspace's link, built from the labels the
    workspace has registered, and the workspace's display name. Where it goes
    is not ours to choose: the connector reads the address off the session, so
    this can only ever reach the account that asked.

    A workspace with no live share is not refused here: the connector owns that
    judgment and answers it as a structured refusal, so the desktop never has
    to describe the share's state in its own words.
    Raises :class:`ImbueCloudShareRefusedCliError` for the connector's
    structured refusals, and :class:`SharingError` when the connector cannot be
    reached.
    """
    state = get_state()
    cli: ImbueCloudCli | None = state.imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.", SharingFailureReason.UNAVAILABLE)
    agent_id = resolve_agent_for_host(backend_resolver, host_id, state.session_store)
    account_email = resolve_account_email_for_workspace(state.session_store, agent_id)
    share = _read_active_share(cli, account_email, host_id)
    label = resolve_share_target_labels(backend_resolver, agent_id).get(WHOLE_MACHINE_SERVICE)
    link = f"https://{label}.{share.workspace_domain}/" if share is not None and label else None
    try:
        return cli.send_mobile_access_link(
            account=account_email,
            host_id=host_id,
            link=link,
            workspace_name=_workspace_display_name(backend_resolver, agent_id),
        )
    except ImbueCloudShareRefusedCliError:
        raise
    except ImbueCloudCliError as exc:
        raise connector_failure_error("Could not email the link", exc) from exc


def list_invitation_outcomes_for_workspace(
    host_id: str, backend_resolver: BackendResolverInterface
) -> list[InvitationOutcomeCliEntry]:
    """What the granter may learn about each open user or email grant of the workspace; empty while unpublished."""
    state = get_state()
    cli: ImbueCloudCli | None = state.imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.", SharingFailureReason.UNAVAILABLE)
    session_store = state.session_store
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    try:
        return cli.list_invitation_outcomes(account=account_email, host_id=host_id)
    except ImbueCloudShareRefusedCliError as exc:
        # An unpublished workspace has nothing to report: no invitation can be
        # made until it is published again.
        if exc.code == "not_published":
            return []
        raise
    except ImbueCloudCliError as exc:
        raise connector_failure_error("Could not read the invitation outcomes", exc) from exc


def probe_share_readiness(http_client: httpx2.Client, probe_host: str) -> bool:
    """Report whether the shared hostname is live end to end.

    ``probe_host`` must be a ROUTABLE share origin -- a ``<label>.<machine
    domain>`` host, typically the shell's ``system_interface`` label origin.
    The bare machine domain is deliberately not probeable: only explicit
    ``<label>.<machine domain>`` origins are claimed on the relay and served by
    caddy, so the bare domain never routes.

    Reaching the workspace's gateway means DNS, the relay's SNI splice, the
    tunnel, caddy's TLS termination with a real certificate, and the gateway
    itself all work -- any HTTP response (the broker redirect for an
    unauthenticated visit, a 403, anything) counts as ready. Transport errors
    (DNS, TLS, connection) mean not-ready-yet. The host is derived from the
    connector's share record + the workspace's own service labels, never from
    caller input.
    """
    with get_profiler().span("sharing.readiness_probe", probe_host=probe_host) as span:
        try:
            http_client.get(f"https://{probe_host}/", timeout=SHARE_READINESS_PROBE_TIMEOUT_SECONDS)
        except httpx2.HTTPError as exc:
            logger.debug("Probed share host {} but it is not ready yet: {}", probe_host, exc)
            span.add_args(is_ready=False)
            return False
        span.add_args(is_ready=True)
        return True
