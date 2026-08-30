"""Plugin-side helpers for the desktop-client machine-sharing flow.

Sharing is machine-level in the self-hosted relay design: one share per
workspace host, one grants document covering the workspace plus optional
per-service scopes. The connector owns the share record + relay token
(`mngr imbue_cloud shares ...`); authorization lives in the workspace's own
grants file, which the in-workspace share-gateway re-reads on every request.

Enable = connector ``shares create`` -> inject the grants document + share
materials into the workspace (its share-gateway brings up caddy + frpc) ->
the UI polls readiness by probing the real hostname. Disable = clear the
materials + connector ``shares delete`` (the relay token dies, so the tunnel's
next reconnect is rejected even if the materials linger).
"""

import socket
import time
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Final

import httpx
from loguru import logger

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ActiveShareCache
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import build_share_env_text
from imbue.minds.desktop_client.share_materials_injection import clear_share_materials_from_agent
from imbue.minds.desktop_client.share_materials_injection import probe_share_state_in_agent
from imbue.minds.desktop_client.share_materials_injection import provision_share_files_in_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_grants_from_agent
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
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


class EmptyGrantsError(SharingError):
    """Raised when a grants document names no grantee at all: a request-validation failure, not an upstream fault."""


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
        raise SharingError("Session store unavailable; sign in to enable sharing.")
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


def _grants_have_any_grantee(workspace_grants: dict[str, list[str]], service_grants: dict[str, Any]) -> bool:
    if workspace_grants.get("emails") or workspace_grants.get("email_domains"):
        return True
    for grants in service_grants.values():
        if grants.get("emails") or grants.get("email_domains"):
            return True
    return False


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
    if not provider_name:
        return False
    return provider_name == "imbue_cloud" or provider_name.startswith("imbue_cloud_")


def enable_sharing(
    host_id: str,
    workspace_grants: dict[str, list[str]],
    service_grants: dict[str, dict[str, list[str]]],
    backend_resolver: BackendResolverInterface,
) -> dict[str, Any]:
    """Enable (or update) sharing for one machine with the given grants document.

    When the machine is already actively shared, only the grants file is
    rewritten (no token rotation, no tunnel restart -- the gateway re-reads
    grants per request). Otherwise the full provisioning flow runs
    client-side for every row -- connector ``shares create`` + materials
    injection over the user's own SSH -- regardless of provider; the
    connector's server-side enable-sharing primitive is used only for
    web-created workspaces, which have no desktop client to inject from.
    Returns the sharing-status document, which reports ``enabled`` true as
    soon as the connector share exists; the UI separately polls the
    readiness endpoint for end-to-end liveness of the shared hostname.
    """
    if not _grants_have_any_grantee(workspace_grants, service_grants):
        raise EmptyGrantsError("Sharing requires at least one email or email domain to grant access to.")
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.")
    session_store = get_state().session_store
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    # Resolved here (a request-context caller) rather than deep inside the
    # share flow, so the same helper can serve the post-create enabler, which
    # runs off a request context and must be handed the config explicitly.
    return _enable_sharing_with_cli(
        host_id,
        agent_id,
        workspace_grants,
        service_grants,
        cli,
        account_email,
        require_client_env_config(),
        is_cloud_row=_is_imbue_cloud_agent(backend_resolver, agent_id),
        entry_label=_resolve_entry_label(backend_resolver, agent_id),
    )


def _resolve_entry_label(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str | None:
    """The workspace shell's origin label, recorded server-side as the chrome's entry origin."""
    labels = backend_resolver.list_service_labels_for_agent(agent_id)
    shell_label = next((label for name, label in labels.items() if str(name) == _SHELL_SERVICE_NAME), None)
    return shell_label or None


def _enable_sharing_with_cli(
    host_id: str,
    agent_id: AgentId,
    workspace_grants: dict[str, list[str]],
    service_grants: dict[str, dict[str, list[str]]],
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
    entry_label: str | None = None,
) -> dict[str, Any]:
    grants_toml = render_grants_toml(workspace_grants, service_grants)

    # One exec answers everything the flow needs from the workspace: whether
    # the template ships the share gateway, whether share.env is present, and
    # the current grants document.
    try:
        probe = probe_share_state_in_agent(agent_id, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc

    # Workspaces created from a pre-share-gateway template (minds-v0.3.11 and
    # older) have no service watching share.env, so a share enabled for them
    # would go active on the connector and never become reachable. Refuse up
    # front with the fix: update the workspace (update-self), then re-share --
    # which self-heals, since the gateway picks the materials up on its own.
    # CLEANUP: drop this guard (and the probe's has_gateway signal) once no
    # supported workspaces predate the share gateway -- i.e. after the first
    # post-v0.3.11 release is deployed and old workspaces have run update-self.
    if not probe.has_gateway:
        raise SharingError(
            "This machine's workspace template is too old to support sharing. "
            'Ask the machine to update itself (send it "update yourself", which runs '
            "the update-self skill), then enable sharing again."
        )

    if probe.has_share_env:
        # Materials are present, so this is either a grants-only update (share
        # active server-side: no token rotation, the gateway picks the new
        # grants up on its next request) or stale materials from a share since
        # disabled elsewhere (fall through to full re-provisioning). Only this
        # rare path needs the connector's status; the common enable-from-off
        # path never reads it -- create is the source of truth there.
        try:
            existing = cli.get_share_status(account=account_email, host_id=host_id)
        except ImbueCloudCliError as exc:
            raise SharingError(
                f"Could not read the machine's sharing status: {describe_connector_failure(exc)}"
            ) from exc
        if existing is not None and existing.state == "active":
            # Before replacing the whole document, make sure the current one is
            # readable: a save built against a policy that could not be read
            # would silently erase whatever the unreadable file really granted.
            # (Should never happen -- writes are atomic and serialized -- but
            # the failure mode is permanent data loss, so it is checked anyway.)
            if probe.grants_toml_text is not None and _parse_grants_toml(probe.grants_toml_text) is None:
                raise SharingError(
                    "The machine's current sharing permissions file is unreadable, so this change "
                    "was not saved (saving would erase whoever it currently grants). To reset it, "
                    "disable sharing for this machine and enable it again."
                )
            try:
                # The owner-email file is refreshed alongside, so a share
                # enabled before that feature existed gains it on a grants edit.
                provision_share_files_in_agent(agent_id, grants_toml, account_email, None, cli.mngr_caller)
            except ShareInjectionError as exc:
                raise SharingError(str(exc)) from exc
            return _share_status_document(host_id, existing, workspace_grants, service_grants)

    # Local shares with no materials in the workspace pick the relay by
    # measured latency from here (the workspace runs on this machine, so the
    # desktop's latency is the right proximity signal). A cloud row runs
    # elsewhere, and a stale-materials re-provision is already placed, so both
    # skip the measurement. A re-share after a disable measures again -- the
    # preference is advisory: the connector honors it only for hosts it has no
    # region record of, so an existing share always keeps its region.
    is_relay_region_measured = not is_cloud_row and not probe.has_share_env
    preferred_region = _pick_preferred_relay_region(cli, account_email) if is_relay_region_measured else None
    try:
        share = cli.create_share(
            account=account_email,
            host_id=host_id,
            entry_label=entry_label,
            preferred_region=preferred_region,
            workspace_id=str(agent_id),
        )
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not enable sharing: {describe_connector_failure(exc)}") from exc
    if share.relay_token is None:
        raise SharingError("Sharing enabled but the connector did not return a relay token.")

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
        provision_share_files_in_agent(agent_id, grants_toml, account_email, share_env_text, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc
    return _share_status_document(host_id, share, workspace_grants, service_grants)


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
) -> None:
    """Bring sharing up for a just-created workspace so it is reachable from /web.

    The create form's "enable web access" toggle: every row -- cloud and local
    alike -- runs the desktop share flow with the owning account as the sole
    grantee. (The connector's server-side enable-sharing primitive is used
    only for web-created workspaces, which have no desktop to inject from.)
    Raises :class:`SharingError` when the workspace has no associated account
    or the share bring-up fails.
    """
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    owner_grants = {"emails": [account_email], "email_domains": []}
    _enable_sharing_with_cli(
        host_id,
        agent_id,
        owner_grants,
        {},
        cli,
        account_email,
        client_env_config,
        is_cloud_row=is_cloud_row,
        # The chrome can only enter the workspace at <label>.<domain> (the
        # bare domain is unrouted on the relay), so record the shell label
        # like the settings-page enable does; None when not registered yet.
        entry_label=_resolve_entry_label(backend_resolver, agent_id),
    )


def _share_status_document(
    host_id: str,
    share: ShareCliInfo,
    workspace_grants: dict[str, list[str]],
    service_grants: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "enabled": share.state == "active",
        "workspace_domain": share.workspace_domain,
        "url": f"https://{share.workspace_domain}/" if share.workspace_domain else None,
        "region": share.region,
        "last_tunnel_login_at": share.last_tunnel_login_at,
        "cert_not_after": share.cert_not_after,
        "grants": {"workspace": workspace_grants, "services": service_grants},
    }


def _parse_grant_list(value: object) -> dict[str, list[str]]:
    """Coerce one grants scope read back from the workspace into ``{emails, email_domains}``."""
    if not isinstance(value, dict):
        return {"emails": [], "email_domains": []}
    entries: dict[str, object] = {str(key): entry for key, entry in value.items()}
    emails = entries.get("emails")
    email_domains = entries.get("email_domains")
    return {
        "emails": [str(email) for email in emails] if isinstance(emails, list) else [],
        "email_domains": [str(domain) for domain in email_domains] if isinstance(email_domains, list) else [],
    }


def _parse_grants_toml(
    grants_toml_text: str,
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]] | None:
    """Parse a grants document read back from the workspace; None when malformed.

    Malformed must stay distinguishable from empty: a malformed read rendered
    as "no grants" would show every grantee as revoked, and the next
    whole-document save would then permanently erase grants nobody ever saw.
    """
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
) -> dict[str, Any]:
    """Return the machine's sharing document: enabled/domain/status + the grants read from the workspace."""
    empty_grants: dict[str, list[str]] = {"emails": [], "email_domains": []}
    disabled: dict[str, Any] = {
        "host_id": host_id,
        "enabled": False,
        "workspace_domain": None,
        "url": None,
        "region": None,
        "last_tunnel_login_at": None,
        "cert_not_after": None,
        "grants": {"workspace": empty_grants, "services": {}},
    }
    share = get_active_share(host_id, backend_resolver, cli, session_store)
    if cli is None or share is None:
        return disabled

    # Resolution is repeated here (a cheap local lookup), but discovery is
    # concurrently updated, so the coordinate can become unresolvable between
    # the two calls; degrade to the connector-confirmed share with UNKNOWN
    # grants rather than failing the whole read. The grants must not degrade
    # to "empty": the pane would render every grantee as revoked, and an
    # Enable/edit from that state would replace a policy nobody ever saw.
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
        grants_toml_text = read_share_grants_from_agent(agent_id, cli.mngr_caller)
    except (SharingError, ShareInjectionError) as exc:
        logger.debug("Sharing grants read: {}", exc)
        document = _share_status_document(host_id, share, empty_grants, {})
        document["grants"] = None
        return document
    parsed_grants = _parse_grants_toml(grants_toml_text) if grants_toml_text else (empty_grants, {})
    if parsed_grants is None:
        # Malformed reads back as UNKNOWN (grants: null), the same as a read
        # that never landed: the pane then blocks edits instead of rendering
        # an empty policy that the next save would publish over the real one.
        document = _share_status_document(host_id, share, empty_grants, {})
        document["grants"] = None
        return document
    workspace_grants, service_grants = parsed_grants
    return _share_status_document(host_id, share, workspace_grants, service_grants)


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
        return cached.share
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


def disable_sharing(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> None:
    """Disable sharing for a machine: clear the workspace materials, then delete the connector share.

    Idempotent: an already-unshared machine is a success. Raises
    :class:`SharingError` on a missing CLI, no associated account, or a
    connector error.
    """
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured.")
    agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    clear_share_materials_from_agent(agent_id, cli.mngr_caller)
    try:
        existing = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not read the machine's sharing status: {describe_connector_failure(exc)}") from exc
    if existing is None or existing.state != "active":
        return
    try:
        cli.delete_share(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not stop sharing: {describe_connector_failure(exc)}") from exc


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


# The workspace shell service; its label origin is the routable entry point of
# a whole-machine share (the bare machine domain does not route on a share).
_SHELL_SERVICE_NAME: Final[str] = "system_interface"


def resolve_share_probe_host(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    host_id: str,
    workspace_domain: str,
) -> str | None:
    """The routable origin host to probe for share readiness: the shell's label origin.

    Returns ``<system_interface label>.<workspace_domain>``, or None when the
    machine or its shell label is not known yet (so the share is not ready to
    probe). The bare workspace domain is never probeable -- it does not route on
    a share (only explicit ``<label>.<domain>`` origins are claimed on the relay
    and served by caddy).
    """
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id, session_store)
    except SharingError as exc:
        logger.debug("Cannot resolve a share probe host for {} yet: {}", host_id, exc)
        return None
    shell_label = _resolve_entry_label(backend_resolver, agent_id)
    if shell_label is None:
        return None
    return f"{shell_label}.{workspace_domain}"


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
