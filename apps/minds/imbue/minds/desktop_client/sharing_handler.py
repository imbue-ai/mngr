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
"""

import socket
import time
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

import httpx
from loguru import logger

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
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.provider_display import is_imbue_cloud_provider_name
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_grant_validation import GrantRefusal
from imbue.minds.desktop_client.share_grant_validation import normalized_grants_document
from imbue.minds.desktop_client.share_grant_validation import validate_grants_document
from imbue.minds.desktop_client.share_materials_injection import ShareGatewayStatus
from imbue.minds.desktop_client.share_materials_injection import ShareGatewayStatusCache
from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import build_share_env_text
from imbue.minds.desktop_client.share_materials_injection import clear_share_publication_from_agent
from imbue.minds.desktop_client.share_materials_injection import probe_share_state_in_agent
from imbue.minds.desktop_client.share_materials_injection import provision_share_files_in_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_gateway_status_from_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_grants_from_agent
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.share_targets import WHOLE_MACHINE_SERVICE
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
    "This machine's workspace template is too old to support sharing. "
    'Ask the machine to update itself (send it "update yourself", which runs '
    "the update-self skill), then publish it again."
)


def describe_connector_failure(exc: Exception) -> str:
    """Turn a connector failure into a sentence the user can act on.

    The plugin's own message is written for whoever is reading the logs
    ("Refresh rejected by connector: Session missing in db or has expired").
    The two failures a user can resolve get a plain sentence instead; anything
    else keeps the plugin's message, which still beats pointing at a log file.
    """
    detail = str(exc)
    if any(signal in detail for signal in _EXPIRED_SESSION_SIGNALS):
        return "Your Imbue Cloud session has expired. You may need to log out and log in again."
    if _UNVERIFIED_EMAIL_SIGNAL in detail:
        return "Imbue Cloud has not verified this account's email address. Verify it, then retry."
    return detail


class SharingError(RuntimeError):
    """Raised on a soft sharing failure; carries a single user-presentable message."""


class GrantsRefusedError(SharingError):
    """Raised when a grants document names entries that must not be granted."""

    def __init__(self, refusals: Sequence[GrantRefusal]) -> None:
        self.refusals = tuple(refusals)
        super().__init__(" ".join(refusal.message for refusal in refusals))


class ShareMaterialsWriteError(SharingError):
    """Raised when the connector share was registered but the workspace did not receive its materials.

    The connector side has already moved on (domain minted, relay token
    rotated) while the workspace still holds whatever it had before.
    """


def resolve_account_email_for_workspace(
    session_store: MultiAccountSessionStore | None,
    agent_id: AgentId,
) -> str:
    """Return the email of the account that owns ``agent_id``.

    Raises :class:`SharingError` if no signed-in account is associated
    with the workspace -- without an account the plugin can't make
    authenticated calls to the connector and there's nothing useful for
    the route to do.
    """
    if session_store is None:
        raise SharingError("Session store unavailable; sign in to publish this workspace.")
    account = session_store.get_account_for_workspace(str(agent_id))
    if account is None:
        raise SharingError(
            f"Workspace {agent_id} is not associated with any signed-in account; "
            "associate one from the workspace settings page first."
        )
    return str(account.email)


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
    raise SharingError(f"No workspace is known for machine '{host_id}'.")


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
        return None
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


def _publish_grantees(cli: ImbueCloudCli, account_email: str, host_id: str, user_ids: Sequence[str]) -> None:
    """Mirror the user-id grants to the connector's index and the owner's contacts (best effort).

    Neither write affects who may visit -- the grants file inside the
    workspace stays the only authority -- so a failure is logged and the next
    grants save simply retries.
    """
    try:
        cli.set_share_grantees(account=account_email, host_id=host_id, grantee_user_ids=user_ids)
    except ImbueCloudCliError as exc:
        logger.warning("Could not update the share's grantee index for {}: {}", host_id, exc)
    for user_id in user_ids:
        try:
            cli.add_contact(account=account_email, user_id=user_id)
        except ImbueCloudCliError as exc:
            logger.warning("Could not add grantee {} to the owner's contacts: {}", user_id[:8], exc)


def _record_saved_grants(
    cli: ImbueCloudCli,
    account_email: str,
    host_id: str,
    agent_id: AgentId,
    user_ids: Sequence[str],
    forward_identity: ForwardIdentityPublisher | None,
    owner_account: AccountSession | None,
) -> None:
    """After a grants write landed: mirror the grantees to the connector, and put the owner's account on the forward's local requests."""
    _publish_grantees(cli, account_email, host_id, user_ids)
    if forward_identity is not None and owner_account is not None:
        forward_identity.mark_shared(str(agent_id), str(owner_account.user_id))


def _owner_account_for(session_store: MultiAccountSessionStore | None, agent_id: AgentId) -> AccountSession | None:
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
        raise SharingError("Client environment config is unavailable; cannot determine the connector URL.")
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
        raise SharingError("imbue_cloud CLI is not configured on this app.")
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
        owner_account=_owner_account_for(session_store, agent_id),
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
        raise SharingError("imbue_cloud CLI is not configured on this app.")
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
        owner_account=_owner_account_for(session_store, agent_id),
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
        raise SharingError("imbue_cloud CLI is not configured on this app.")
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
    # One exec answers everything the flow needs from the workspace: whether
    # the template ships the share gateway, whether share.env is present, and
    # the current grants document.
    try:
        probe = probe_share_state_in_agent(agent_address, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc
    if not probe.has_gateway:
        raise SharingError(_PRE_GATEWAY_WORKSPACE_MESSAGE)
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

    # Materials present means the workspace is published already or carries
    # stale ones from a share disabled elsewhere (re-provision). Only this path
    # needs the connector's status; the common publish-from-off path never reads
    # it -- create is the source of truth there.
    existing = _read_active_share(cli, account_email, host_id) if probe.has_share_env else None
    if existing is not None:
        share = existing
        if grants_toml is not None:
            _write_grants_document(agent_address, grants_toml, cli)
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
            preferred_region=_pick_preferred_relay_region(cli, account_email) if is_relay_region_measured else None,
            service_labels=service_labels,
            grants_toml_text=grants_toml,
        )

    # What the file holds now: what was just written, or the one already there.
    saved_grants = grants_to_store if grants_to_store is not None else (stored_grants or SharingGrantsDocument())
    user_ids = _granted_user_ids(saved_grants)
    _record_saved_grants(cli, account_email, host_id, agent_id, user_ids, forward_identity, owner_account)
    identities = _resolve_grant_identities(user_ids, identity_cache, cli, account_email)
    return _share_status_document(host_id, share, saved_grants, service_labels, identities)


def _save_grants_with_cli(
    host_id: str,
    agent_address: str,
    grants: SharingGrantsDocument,
    cli: ImbueCloudCli,
    account_email: str,
    service_labels: Mapping[str, str],
    identity_cache: IdentityCache | None,
) -> dict[str, Any]:
    # One read answers everything this save needs of the workspace: whether the
    # gateway is there to read what it writes, and which of the entries being
    # saved are new (only those face the rules).
    try:
        probe = probe_share_state_in_agent(agent_address, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc
    if not probe.has_gateway:
        raise SharingError(_PRE_GATEWAY_WORKSPACE_MESSAGE)
    stored_grants = _readable_grants_document(probe.grants_toml_text) or SharingGrantsDocument()
    refusals = validate_grants_document(grants, account_email, stored_grants)
    if refusals:
        raise GrantsRefusedError(refusals)
    saved_grants = normalized_grants_document(grants)
    # The connector is read BEFORE the write, and not again: a hiccup after the
    # document landed would otherwise be reported as a failed save.
    share = _read_active_share(cli, account_email, host_id) if probe.has_share_env else None
    _write_grants_document(agent_address, _render_grants_document_toml(saved_grants), cli)
    user_ids = _granted_user_ids(saved_grants)
    # The connector's grantee index only describes a live share, so an
    # unpublished workspace's save does not touch it; the next publish mirrors
    # whatever the document holds by then.
    if share is not None:
        _publish_grantees(cli, account_email, host_id, user_ids)
    identities = _resolve_grant_identities(user_ids, identity_cache, cli, account_email)
    return _share_status_document(host_id, share, saved_grants, service_labels, identities)


def _write_grants_document(agent_address: str, grants_toml: str, cli: ImbueCloudCli) -> None:
    try:
        provision_share_files_in_agent(agent_address, grants_toml, None, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc


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


def _read_active_share(cli: ImbueCloudCli, account_email: str, host_id: str) -> ShareCliInfo | None:
    """The machine's connector share when it is active, else None.

    Raises :class:`SharingError` when the connector cannot be reached: a
    workspace's publication state is never guessed.
    """
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not read the machine's sharing status: {describe_connector_failure(exc)}") from exc
    return share if share is not None and share.state == "active" else None


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
        raise SharingError(f"Could not publish this workspace: {describe_connector_failure(exc)}") from exc
    if share.relay_token is None:
        raise SharingError("The workspace was published but the connector did not return a relay token.")

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
        provision_share_files_in_agent(agent_address, grants_toml_text, share_env_text, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise ShareMaterialsWriteError(str(exc)) from exc
    return share


def migrate_stale_share(
    host_id: str,
    agent_id: AgentId,
    agent_address: str,
    # The active share as the connector reports it, still on the retired domain.
    stale_share: ShareCliInfo,
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
    try:
        probe = probe_share_state_in_agent(agent_address, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc
    if not probe.has_gateway:
        raise SharingError(_PRE_GATEWAY_WORKSPACE_MESSAGE)
    # The grants are read only for the grantee bookkeeping below; the move
    # rewrites share.env alone and leaves the document exactly as it is, so a
    # document nothing can parse costs the move nothing.
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
        )
    except ShareMaterialsWriteError as exc:
        # The connector no longer flags the share, so the panel's retry (a
        # plain read) will not inject again: only a re-publish does.
        raise SharingError(
            "Sharing was moved to a new address, but this machine did not receive the new share materials, so "
            f"its shared links stay down until publishing is turned off and on again for it: {exc}"
        ) from exc
    logger.info("Moved sharing for {} from {} to {}", host_id, stale_share.workspace_domain, share.workspace_domain)
    user_ids = _granted_user_ids(grants)
    _record_saved_grants(cli, account_email, host_id, agent_id, user_ids, forward_identity, owner_account)
    identities = _resolve_grant_identities(user_ids, identity_cache, cli, account_email)
    document = _share_status_document(host_id, share, grants, service_labels, identities)
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

    The create form's "enable web access" toggle: every row -- cloud and local
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
        owner_account=_owner_account_for(session_store, agent_id),
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
    no link yet). A share the connector flags ``needs_reshare`` (active on a
    content domain the tier moved away from) is repaired here, on the read, so
    opening the share panel is what moves it; that read raises
    :class:`SharingError` when the move fails.
    """
    share = get_active_share(host_id, backend_resolver, cli, session_store)
    # Resolution is repeated here (a cheap local lookup), but discovery is
    # concurrently updated, so the coordinate can become unresolvable between
    # the two calls; degrade to UNKNOWN grants rather than failing the whole
    # read. The grants must not degrade to "empty": the pane would render every
    # grantee as revoked, and a save from that state would replace a policy
    # nobody ever saw.
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    except SharingError as exc:
        logger.debug("Sharing grants read: {}", exc)
        return _unknown_grants_document(host_id, share, {})
    if cli is None:
        return _unknown_grants_document(host_id, share, {})
    service_labels = resolve_share_target_labels(backend_resolver, agent_id)
    if share is not None and share.needs_reshare:
        if client_env_config is None:
            raise SharingError("Client environment config is unavailable; cannot move sharing to its new address.")
        return migrate_stale_share(
            host_id,
            agent_id,
            build_agent_address(agent_id, backend_resolver),
            share,
            cli,
            resolve_account_email_for_workspace(session_store, agent_id),
            client_env_config,
            service_labels,
            identity_cache,
            forward_identity,
            _owner_account_for(session_store, agent_id),
        )
    try:
        grants_toml_text = read_share_grants_from_agent(
            build_agent_address(agent_id, backend_resolver), cli.mngr_caller
        )
    except ShareInjectionError as exc:
        logger.debug("Sharing grants read: {}", exc)
        return _unknown_grants_document(host_id, share, service_labels)
    # A document nothing can parse reads back as an empty one; ``grants: null``
    # is reserved for a read that never landed, above.
    grants = _readable_grants_document(grants_toml_text) or SharingGrantsDocument()
    user_ids = _granted_user_ids(grants)
    identities = _resolve_grant_identities(
        user_ids, identity_cache, cli, _account_email_or_none(session_store, agent_id)
    )
    return _share_status_document(host_id, share, grants, service_labels, identities)


def _account_email_or_none(session_store: MultiAccountSessionStore | None, agent_id: AgentId) -> str | None:
    """The owning account's email, or None (identity lookups are skipped) when no signed-in account owns the workspace."""
    try:
        return resolve_account_email_for_workspace(session_store, agent_id)
    except SharingError as exc:
        logger.debug("Skipping grantee identity lookups for {}: {}", agent_id, exc)
        return None


def _unknown_grants_document(
    host_id: str, share: ShareCliInfo | None, service_labels: Mapping[str, str]
) -> dict[str, Any]:
    """The machine's document with ``grants: None``: the read never landed (not "the workspace grants nobody")."""
    document = _share_status_document(host_id, share, SharingGrantsDocument(), service_labels, {})
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
    if cli is None:
        return None
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
        account_email = resolve_account_email_for_workspace(session_store, agent_id)
    except SharingError as exc:
        logger.debug("Sharing status: {}", exc)
        return None
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to read share status for {}: {}", host_id, exc)
        return None
    if share is None or share.state != "active":
        return None
    return share


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
        raise SharingError("imbue_cloud CLI is not configured.")
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    clear_share_publication_from_agent(build_agent_address(agent_id, backend_resolver), cli.mngr_caller)
    if forward_identity is not None:
        forward_identity.mark_unshared(str(agent_id))
    try:
        existing = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not read the machine's sharing status: {describe_connector_failure(exc)}") from exc
    if existing is not None and existing.state == "active":
        try:
            cli.delete_share(account=account_email, host_id=host_id)
        except ImbueCloudCliError as exc:
            raise SharingError(f"Could not unpublish this workspace: {describe_connector_failure(exc)}") from exc
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


def probe_share_readiness(http_client: httpx.Client, probe_host: str) -> bool:
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
    try:
        http_client.get(f"https://{probe_host}/", timeout=SHARE_READINESS_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.debug("Probed share host {} but it is not ready yet: {}", probe_host, exc)
        return False
    return True
