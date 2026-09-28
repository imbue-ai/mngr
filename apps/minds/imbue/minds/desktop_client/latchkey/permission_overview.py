"""Cross-workspace read/revoke view of predefined (catalog-backed) latchkey grants.

Backs the App-level settings "Permissions" section: it enumerates the
predefined-service permissions granted on every *active* workspace's host and
lets the user revoke them. Revocation removes the rule from that host's
``latchkey_permissions.json`` (through the gateway's bundled ``permissions``
extension, the single owner of on-disk permission writes); stored credentials
are left untouched, so a fresh grant does not force the user to re-authenticate.

Grants are per *account*: a host's rule key is ``<scope>:<account>`` (see
:mod:`imbue.mngr_latchkey.account_scopes`), so the Connectors page is organized
as one section per signed-in account rather than one per service. A section is
shown for every account latchkey has stored *and* for every account that still
appears in some host's rules without being connected (one whose credentials were
cleared outside the app, or one that was granted before it was ever connected),
so no grant is invisible and unrevocable.

Permissions are stored per host -- every agent on a host shares one
``latchkey_permissions.json`` (see :func:`permissions_path_for_host`). Minds
workspaces map 1:1 to hosts, so each column in the settings view is one
workspace, labelled by its primary agent's display name. Only non-destroyed
workspaces are shown (via
:meth:`BackendResolverInterface.list_active_workspace_ids`).

This module is deliberately read/revoke only: changing (broadening or
narrowing) an existing grant is done through the ordinary agent-driven
permission-request flow, not here.
"""

import threading
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

# The catch-all detent permission (matches every request under the scope) is
# shown to users as "all", mirroring the permission-request dialog.
_WILDCARD_DISPLAY_LABEL = "all"
_WILDCARD_DESCRIPTION = "Unrestricted access: any request to this service is permitted."

# File-sharing *and* cross-workspace-management grants share the domain-only
# ``latchkey-self`` scope with baseline / accounts permissions (the gateway
# unions per-feature permission schemas onto it rather than minting dedicated
# scopes). So the whole ``latchkey-self`` rule must never be deleted; each
# feature is read back by its permission-name prefix and revoked by removing
# only its own permission names from the rule. Public because the
# per-workspace toggle screen (:mod:`permission_toggles`) rewrites the same
# rule.
SELF_SCOPE = "latchkey-self"

# File-sharing grants: per-path permission schemas named
# ``minds-file-server-<access>-<absolute-path>`` (see the gateway's
# ``permission_requests.mjs``).
FILE_SHARING_PERMISSION_PREFIX = "minds-file-server-"
_FILE_SHARING_READ = "read"
_FILE_SHARING_WRITE = "write"
# User-facing labels: a write grant is a read+write superset, so it reads as
# "read and write"; a read-only grant reads as "read".
FILE_SHARING_READ_LABEL = "read"
FILE_SHARING_WRITE_LABEL = "read and write"

# Cross-workspace-management grants: verb permission schemas named
# ``minds-workspaces-<verb>`` (an all-workspaces grant) or
# ``minds-workspaces-<verb>-<target_agent_id>`` (a grant pinned to one target
# workspace). ``read`` / ``create`` are all-or-nothing; the rest are targeted.
WORKSPACE_PERMISSION_PREFIX = "minds-workspaces-"
_WORKSPACE_VERB_BY_PERMISSION = {verb.permission: verb for verb in WORKSPACE_VERBS}
_TARGETED_WORKSPACE_VERB_PERMISSIONS = tuple(verb.permission for verb in WORKSPACE_VERBS if verb.is_targeted)


class PermissionOverviewError(Exception):
    """Raised for caller-facing programming errors (e.g. revoking an unknown service)."""


class GrantedPermission(FrozenModel):
    """A single granted permission plus its plain-English description for a tooltip."""

    label: str = Field(description="User-facing label (the detent catch-all ``any`` is rendered as ``all``).")
    description: str = Field(
        default="",
        description="Plain-English summary shown as a tooltip; empty when the catalog has none.",
    )


class SharedPath(FrozenModel):
    """A single shared filesystem path and the access level granted on it."""

    path: str = Field(description="Absolute path shared with the agent.")
    access_label: str = Field(description="User-facing access level: ``read`` or ``read and write``.")


# Label shown for a service's single unnamed "default" account (latchkey keys it
# by the empty string). Users never typed a name for it, so we show a neutral
# placeholder rather than an empty row.
_DEFAULT_ACCOUNT_LABEL = "Default account"


class ServiceAccount(FrozenModel):
    """One signed-in account for a service, shown under the service's Connectors header."""

    account: str = Field(
        description='Latchkey account key (an e-mail / handle; ``""`` for the unnamed default); the disconnect key.',
    )
    label: str = Field(description="User-facing account label (the default account reads as ``Default account``).")


def _granted_permissions(
    service_infos: Sequence[ServicePermissionInfo],
    granted: frozenset[str],
) -> tuple[GrantedPermission, ...]:
    """Map the granted permission schemas to labelled, described permissions in catalog order.

    Iterates the catalog's declared permission schemas across every scope the
    service owns (``any`` is index 0 of each), keeping only those actually
    granted and de-duplicating across scopes. Grants that are not in the
    catalog for the service are dropped (defence-in-depth against a hand-edited
    file), and the catch-all ``any`` is relabeled ``all`` with a generic
    description.
    """
    permissions: list[GrantedPermission] = []
    seen: set[str] = set()
    for info in service_infos:
        for schema in info.permission_schemas:
            if schema not in granted or schema in seen:
                continue
            seen.add(schema)
            if schema == WILDCARD_PERMISSION_NAME:
                permissions.append(GrantedPermission(label=_WILDCARD_DISPLAY_LABEL, description=_WILDCARD_DESCRIPTION))
            else:
                permissions.append(
                    GrantedPermission(label=schema, description=info.description_by_permission_name.get(schema, ""))
                )
    return tuple(permissions)


# Ceiling on how long the parallel browser-support probes may take to join.
# Must stay above latchkey's own ``services info`` timeout so a stalled probe
# resolves the way it does on its own (degrading to "unknown") instead of
# failing the whole settings page on the join.
_BROWSER_SUPPORT_PROBE_EXIT_TIMEOUT_SECONDS: Final[float] = 30.0


def probe_services_info(
    latchkey: Latchkey,
    service_names: Sequence[str],
) -> dict[str, LatchkeyServiceInfo]:
    """Read ``latchkey services info`` for several services at once.

    How a service is signed in to (``is_browser_auth_supported``) and what
    credentials it asks for (``set_credentials_example``) are only known from
    this call, and it is one subprocess per service, so the probes run on a
    thread each (they are independent, and each writes its own key) rather than
    adding up in series on a page's critical path. ``--offline`` keeps every
    probe a local lookup. A service whose probe did not report (``None``) is
    absent from the result, so callers decide what an unknown service
    defaults to instead of mistaking a guess for latchkey's answer.
    """
    service_info_by_name: dict[str, LatchkeyServiceInfo] = {}

    def _probe_one_service(service_name: str) -> None:
        info = latchkey.services_info(service_name, is_offline=True)
        if info is not None:
            service_info_by_name[service_name] = info

    concurrency_group = ConcurrencyGroup(
        name="connectors-browser-support",
        exit_timeout_seconds=_BROWSER_SUPPORT_PROBE_EXIT_TIMEOUT_SECONDS,
    )
    with concurrency_group:
        for service_name in service_names:
            concurrency_group.start_new_thread(
                target=_probe_one_service,
                args=(service_name,),
                name=f"browser-support-{service_name}",
            )
    return service_info_by_name


class ServiceSignInOptions(FrozenModel):
    """How a service can be signed in to, from its ``services info`` probe.

    The static half of that call: which auth options latchkey compiled in for
    the service, and the credential command it advertises. Unlike the accounts
    and credential status the same call returns, these are fixed properties of
    the latchkey binary, which is why they can be remembered.
    """

    is_browser_auth_supported: bool = Field(description="Whether latchkey can sign this service in through a browser.")
    set_credentials_example: str = Field(description="Advertised credential command, empty when it advertises none.")


# Remembered per service for the life of the process. The probe is one
# subprocess per service and the pane offers every catalog service, so probing
# on each payload build put a burst of them on every toggle write. Only a probe
# that reported is stored: one that timed out is retried on the next build
# rather than remembered as a guess about how the service connects.
_SIGN_IN_OPTIONS_BY_SERVICE: Final[dict[str, ServiceSignInOptions]] = {}
_SIGN_IN_OPTIONS_LOCK: Final[threading.Lock] = threading.Lock()


def clear_service_sign_in_options_cache() -> None:
    """Forget how services connect, so the next probe asks latchkey again.

    The answers are fixed for a given latchkey binary, so the only things that
    invalidate them are that binary changing under a running process, and a
    test swapping in a different latchkey.
    """
    with _SIGN_IN_OPTIONS_LOCK:
        _SIGN_IN_OPTIONS_BY_SERVICE.clear()


def probe_service_sign_in_options(
    latchkey: Latchkey,
    service_names: Sequence[str],
) -> dict[str, ServiceSignInOptions]:
    """How each service connects, probing only the ones not already known.

    A service missing from the result is one whose probe did not report; the
    caller decides what that defaults to.
    """
    with _SIGN_IN_OPTIONS_LOCK:
        unknown = tuple(name for name in service_names if name not in _SIGN_IN_OPTIONS_BY_SERVICE)
    if unknown:
        probed = probe_services_info(latchkey, unknown)
        for service_name in unknown:
            if service_name not in probed:
                logger.debug("No services-info probe for {}; assuming its browser sign-in", service_name)
        with _SIGN_IN_OPTIONS_LOCK:
            for service_name, info in probed.items():
                _SIGN_IN_OPTIONS_BY_SERVICE[service_name] = ServiceSignInOptions(
                    is_browser_auth_supported=info.is_browser_auth_supported,
                    set_credentials_example=info.set_credentials_example or "",
                )
    with _SIGN_IN_OPTIONS_LOCK:
        return {
            service_name: _SIGN_IN_OPTIONS_BY_SERVICE[service_name]
            for service_name in service_names
            if service_name in _SIGN_IN_OPTIONS_BY_SERVICE
        }


def _sorted_accounts_by_label(accounts: Iterable[str]) -> tuple[str, ...]:
    """Sort account names for display: named ones alphabetically, the unnamed default last."""
    return tuple(sorted(accounts, key=lambda account: (account == DEFAULT_ACCOUNT, account.lower())))


def account_label(account: str) -> str:
    """Render a latchkey account key as a user-facing label (default account is unnamed)."""
    return _DEFAULT_ACCOUNT_LABEL if account == DEFAULT_ACCOUNT else account


def _service_accounts(accounts: Sequence[ServiceAccountCredential]) -> tuple[ServiceAccount, ...]:
    """Turn one service's stored accounts (from :meth:`Latchkey.auth_list`) into UI rows.

    Accounts are sorted for a stable UI, with the unnamed default account (if
    any) shown last.
    """
    return tuple(
        ServiceAccount(account=account.account, label=account_label(account.account))
        for account in sorted(accounts, key=lambda entry: (entry.account == DEFAULT_ACCOUNT, entry.account.lower()))
    )


def disconnect_account(latchkey: Latchkey, service_name: str, account: str) -> bool:
    """Clear one account's stored credentials for ``service_name``.

    Runs ``latchkey auth clear <service> --account <account>`` (the default
    account is addressed with the empty string). Returns ``True`` when the
    service has no stored accounts left afterwards, so the caller can trigger the
    "revoke all" cleanup. Raises :class:`PermissionOverviewError` if the clear
    command fails.
    """
    is_success, detail = latchkey.auth_clear(service_name, account=account)
    if not is_success:
        raise PermissionOverviewError(f"Could not disconnect account '{account or 'default'}': {detail}")
    remaining_info = latchkey.services_info(service_name, is_offline=True)
    # The clear itself already succeeded, so a follow-up probe that does not
    # answer reads as "none left": claiming leftover accounts would only
    # suppress the (idempotent) revoke-all cleanup.
    return remaining_info is None or len(remaining_info.accounts) == 0


def parse_file_sharing_permission(permission_name: str) -> tuple[str, str] | None:
    """Split a ``minds-file-server-<access>-<path>`` name into ``(access, path)``.

    Returns ``None`` for any permission name that is not a well-formed
    file-sharing schema (so unrelated ``latchkey-self`` permissions -- baseline,
    accounts, workspace verbs -- are ignored). The access mode is the token
    before the first ``-`` after the prefix; the remainder (which starts with
    ``/``) is the absolute path.
    """
    if not permission_name.startswith(FILE_SHARING_PERMISSION_PREFIX):
        return None
    remainder = permission_name[len(FILE_SHARING_PERMISSION_PREFIX) :]
    access, separator, path = remainder.partition("-")
    if not separator or access not in (_FILE_SHARING_READ, _FILE_SHARING_WRITE) or not path:
        return None
    return access, path


# -- Cross-workspace management ("workspace") grants ---------------------------


def parse_workspace_permission(permission_name: str) -> tuple[str, str | None] | None:
    """Split a ``minds-workspaces-*`` permission into ``(verb_permission, target)``.

    ``target`` is ``None`` for an all-workspaces grant (a broad verb name) and the
    target workspace agent id for a per-target grant. Returns ``None`` for any name
    that is not a well-formed workspace verb, so unrelated ``latchkey-self``
    permissions (baseline / accounts / file-sharing) are ignored. Matching is by
    the known verb names (not naive ``-`` splitting) because verb names such as
    ``minds-workspaces-backups-export`` themselves contain hyphens.
    """
    if not permission_name.startswith(WORKSPACE_PERMISSION_PREFIX):
        return None
    if permission_name in _WORKSPACE_VERB_BY_PERMISSION:
        return permission_name, None
    for verb_permission in _TARGETED_WORKSPACE_VERB_PERMISSIONS:
        prefix = f"{verb_permission}-"
        if permission_name.startswith(prefix):
            target = permission_name[len(prefix) :]
            if target:
                return verb_permission, target
    return None


def resolve_target_workspace_name(backend_resolver: BackendResolverInterface, target_workspace_id: str) -> str:
    """Resolve a target workspace agent id to a display name, falling back to the raw id."""
    try:
        parsed = AgentId(target_workspace_id)
    except ValueError:
        return target_workspace_id
    name = backend_resolver.get_workspace_name(parsed)
    if name:
        return name
    info = backend_resolver.get_agent_display_info(parsed)
    return info.agent_name if info is not None else target_workspace_id


def resolve_workspace_host_id(
    backend_resolver: BackendResolverInterface,
    workspace_agent_id: str,
) -> HostId | None:
    """Resolve a workspace agent id to its :class:`HostId`, or ``None`` if unknown."""
    try:
        parsed = AgentId(workspace_agent_id)
    except ValueError:
        return None
    info = backend_resolver.get_agent_display_info(parsed)
    if info is None:
        return None
    try:
        return HostId(info.host_id)
    except ValueError:
        return None


def _revoke_service_account_at_path(
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    permissions_file_path: Path,
    service_name: str,
    account: str,
) -> None:
    """Delete every rule of ``permissions_file_path`` that grants ``account`` of ``service_name``.

    The rules to delete are the ones :meth:`ServicesCatalog.list_service_account_grants`
    resolves to this (service, account) pair, so the keys come from the file
    itself instead of being reconstructed from a naming convention. Other
    accounts of the same service, and every other rule, are untouched. The
    generated schema behind each deleted key is left in the file: it is inert
    once unreferenced, and a later re-grant overwrites it by name.
    """
    config = gateway_client.get_permissions_config(permissions_file_path)
    for grant in services_catalog.list_service_account_grants(config):
        if grant.service_name == service_name and grant.account == account:
            gateway_client.delete_permission_rule(permissions_file_path, grant.rule_key)


def revoke_service_account_for_workspace(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
    service_name: str,
    account: str,
    push_permissions_to_machine: Callable[[str], None],
) -> None:
    """Remove one account's grants for ``service_name`` from the given workspace's host file.

    The edited policy is then pushed to the workspace's own machine, and this
    does not return until it lands there.

    Raises :class:`PermissionOverviewError` for an unknown service or an
    unresolvable workspace (the caller maps these to a 400 / 503), and
    :class:`MachineOperationError` when the machine would not take the policy.
    """
    if not services_catalog.get(service_name):
        raise PermissionOverviewError(f"Unknown service '{service_name}'.")
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionOverviewError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot revoke.",
        )
    _revoke_service_account_at_path(
        gateway_client,
        services_catalog,
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        service_name,
        account,
    )
    push_permissions_to_machine(workspace_agent_id)
