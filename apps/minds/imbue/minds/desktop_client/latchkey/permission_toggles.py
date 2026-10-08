"""Per-workspace toggle view and editing of latchkey permissions.

Backs the workspace options panel's "Permissions" tab: one screen, scoped to a
single workspace's host, where every grantable permission renders as a toggle.
Flipping a toggle recomputes the *complete* permission set for the affected
rule from the new toggle states and writes it back through the gateway's
``permissions`` extension (the single owner of on-disk permission writes) --
the endpoint always receives the full set, never a diff.

Three families of toggles, mirroring the permission-request types:

* **Connector toggles** -- catalog-backed third-party service permissions,
  granted per ``(scope, account)`` rule (see
  :mod:`imbue.mngr_latchkey.account_scopes`). Toggling writes the rule with
  :func:`build_account_grant`, so the generated per-account schema always
  travels with it; turning the last permission off deletes the rule.
* **File-sharing toggles** -- the ``minds-file-server-*`` permission names on
  the shared ``latchkey-self`` rule. The rule also carries baseline / accounts
  / workspace permissions, so a rewrite always preserves every name this
  module does not own.
* **Machine (cross-workspace) toggles** -- the ``minds-workspaces-*`` verb
  names on the same ``latchkey-self`` rule, preserved the same way.

**Desktop egress** is a fourth family, per service rather than per account: a
remote workspace's requests to a service can be sent out through the user's
computers (see :mod:`imbue.mngr_latchkey.desktop_egress`). The machine's rules
file gives each service its route, which names each computer it goes through;
a device-gated grant in the host's permissions file names one computer and is
what makes that computer accept the requests of a route, and every write of a
route rewrites the grants to match, so they are never edited on their own. A
rules file written before routes says only that a service was turned on, and
its route is then read off the grants (:func:`resolve_desktop_egress_routes`).

``Add connection`` is the last write: it connects a service that has no
account yet (or a further account of one that has). Most services are connected
by latchkey's browser sign-in, which the settings page's route already runs;
the rest -- AWS, Coolify, ... -- are connected by
:func:`connect_service_with_credentials`, which fills the values the user typed
into the service's own credential command (see
:mod:`imbue.mngr_latchkey.credential_commands`) and runs it. Which of the two a
service takes travels with it as :class:`ServiceSignIn`, so the pane never
offers a sign-in that cannot happen.

Enabling a ``latchkey-self`` toggle needs the permission's schema definition to
already exist in the host file (per-path / per-verb schemas are minted by the
gateway when a request is approved; revoking leaves them behind precisely so
the permission can be re-enabled here). A name whose schema is gone cannot be
re-enabled -- detent fails the entire check when a referenced schema is
unknown. Rows exist for the names the file knows about at all: the granted
ones plus the ones whose schema survives a revoke. So a name with neither is
not offered, and a name that is granted with no schema left behind it carries
``can_enable=False`` -- it can still be turned off, but only the agent
re-requesting brings it back.

The desktop client is the only writer: page JS posts one toggle flip, the
route recomputes the full set server-side (so a buggy client can never clobber
unrelated ``latchkey-self`` baselines), and the gateway merges the write.
"""

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.machine_latchkey import is_machine_store_of_its_own
from imbue.minds.desktop_client.latchkey.permission_overview import FILE_SHARING_READ_LABEL
from imbue.minds.desktop_client.latchkey.permission_overview import FILE_SHARING_WRITE_LABEL
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.permission_overview import ServiceSignInOptions
from imbue.minds.desktop_client.latchkey.permission_overview import account_label
from imbue.minds.desktop_client.latchkey.permission_overview import parse_workspace_permission
from imbue.minds.desktop_client.latchkey.permission_overview import probe_service_sign_in_options
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_target_workspace_name
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_workspace_host_id
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.account_scopes import list_account_grants
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import read_registered_services
from imbue.mngr_latchkey.credential_commands import CredentialCommandError
from imbue.mngr_latchkey.credential_commands import CredentialCommandParameter
from imbue.mngr_latchkey.credential_commands import ParsedCredentialCommand
from imbue.mngr_latchkey.credential_commands import build_credential_command_argv
from imbue.mngr_latchkey.credential_commands import describe_credential_command_failure
from imbue.mngr_latchkey.credential_commands import parse_credential_command_example
from imbue.mngr_latchkey.credential_commands import set_credentials_example_for
from imbue.mngr_latchkey.custom_services import custom_service_credential_header
from imbue.mngr_latchkey.desktop_egress import DesktopEgressError
from imbue.mngr_latchkey.desktop_egress import DesktopEgressGrant
from imbue.mngr_latchkey.desktop_egress import DesktopEgressMode
from imbue.mngr_latchkey.desktop_egress import DesktopEgressRoute
from imbue.mngr_latchkey.desktop_egress import DesktopEgressRules
from imbue.mngr_latchkey.desktop_egress import SELF_HOP
from imbue.mngr_latchkey.desktop_egress import SELF_ONLY_DESKTOP_EGRESS_ROUTE
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_grant
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_route
from imbue.mngr_latchkey.desktop_egress import desktop_egress_forwarders
from imbue.mngr_latchkey.desktop_egress import desktop_egress_mode_for_route
from imbue.mngr_latchkey.desktop_egress import desktop_egress_route_for_service
from imbue.mngr_latchkey.desktop_egress import desktop_egress_route_from_grants
from imbue.mngr_latchkey.desktop_egress import list_desktop_egress_grants
from imbue.mngr_latchkey.desktop_egress import parse_desktop_egress_rules
from imbue.mngr_latchkey.desktop_egress import serialize_desktop_egress_rules
from imbue.mngr_latchkey.file_sharing import FileSharingAccess
from imbue.mngr_latchkey.file_sharing import LegacyFileSharingGrant
from imbue.mngr_latchkey.file_sharing import legacy_file_sharing_permission_name
from imbue.mngr_latchkey.file_sharing import parse_file_sharing_permission
from imbue.mngr_latchkey.file_sharing import parse_legacy_file_sharing_permission
from imbue.mngr_latchkey.remote.credentials import has_machine_of_its_own
from imbue.mngr_latchkey.remote.credentials import read_host_desktop_egress_rules
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import desktop_egress_rules_path_for_host
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

# Group used for the ``<service>-read-all`` / ``<service>-write-all`` pair (and
# bare ``read`` / ``write`` scopes like github-git), always rendered first.
_FULL_ACCESS_HEADING: Final[str] = "Full access"
_FULL_ACCESS_GROUP_KEY: Final[str] = "aa-full-access"
# Group used for the injected detent catch-all ``any``, always rendered last.
_EXTRAS_HEADING: Final[str] = "Extras"
_EXTRAS_GROUP_KEY: Final[str] = "zz-extras"

# Leading verb tokens recognized in verb-first permission names
# (``github-read-repos``, ``google-gmail-send-messages``,
# ``notion-mcp-create-pages``). A name starting with one of these reads as
# "<Verb> <object>" and groups by the object.
_LEADING_VERBS: Final[frozenset[str]] = frozenset(
    {
        "read",
        "write",
        "send",
        "get",
        "list",
        "search",
        "create",
        "update",
        "delete",
        "manage",
        "query",
        "fetch",
        "move",
        "duplicate",
        "check",
    }
)


class PermissionToggleError(Exception):
    """Raised for caller-facing problems (unknown scope/permission, unresolvable workspace)."""


class ClassifiedPermission(FrozenModel):
    """A catalog permission paired with the label :func:`classify_permission` derived for it."""

    permission: str = Field(description="Detent permission schema name (the stored / wire value).")
    label: str = Field(description="User-facing row label derived from the permission name.")


class PermissionAreaGroup(FrozenModel):
    """One heading's worth of a scope's permissions, in the order the UI renders them."""

    heading: str = Field(description="Group heading rendered as a section eyebrow.")
    is_extras: bool = Field(description="Whether this is the trailing wildcard group.")
    permissions: tuple[ClassifiedPermission, ...] = Field(description="Permissions in catalog order.")


class PermissionToggle(FrozenModel):
    """One toggleable catalog permission and its current state on the host."""

    permission: str = Field(description="Detent permission schema name (the stored / wire value).")
    label: str = Field(description="User-facing row label derived from the permission name.")
    description: str = Field(
        default="",
        description="Plain-English summary from the catalog; empty when the catalog has none.",
    )
    is_granted: bool = Field(description="Whether the host's rule currently grants this permission.")


class PermissionToggleGroup(FrozenModel):
    """A titled group of toggles (``Full access``, ``Messages``, ...), mirroring the design's sections."""

    heading: str = Field(description="Group heading rendered as a section eyebrow.")
    toggles: tuple[PermissionToggle, ...] = Field(description="Toggle rows in catalog order.")


class ScopeTogglePanel(FrozenModel):
    """The grouped toggles of one detent scope a service exposes."""

    scope: str = Field(description="Detent scope schema name; the rule key half of the grant.")
    heading: str = Field(description="Scope display name, shown as a divider when a service has several scopes.")
    groups: tuple[PermissionToggleGroup, ...] = Field(description="Toggle groups, full access first, extras last.")


class ServiceSignIn(FrozenModel):
    """How the *next* account of a service is connected from ``Add connection``.

    Latchkey signs most services in through a browser; the rest (AWS, Coolify,
    ...) are connected by typing in the credentials their own
    ``setCredentialsExample`` asks for.
    """

    is_browser_supported: bool = Field(
        description="Whether connecting runs latchkey's browser sign-in rather than asking for credentials.",
    )
    credential_parameters: tuple[CredentialCommandParameter, ...] = Field(
        description=(
            "One labeled input per value the service's credential command needs. Empty when the browser "
            "sign-in does the work, and when latchkey's example cannot be turned into inputs at all -- in "
            "which case there is no way to connect the service from here."
        ),
    )
    is_account_name_required: bool = Field(
        description=(
            "Whether credentials typed in here also need a name for the account they create. The first "
            "account of a service is latchkey's unnamed default; every later one is named by the user."
        ),
    )


class DesktopEgressSetting(FrozenModel):
    """Where one service's requests from this workspace leave from."""

    is_supported: bool = Field(
        description=(
            "Whether the setting can be offered: the app knows its device id, and the workspace has a "
            "machine of its own."
        ),
    )
    mode: DesktopEgressMode = Field(description="What the switch shows for ``route``.")
    route: tuple[str, ...] = Field(
        description=(
            "The hops of the service's route, in the order the machine tries them, according to this "
            "computer's copy of the machine's rules file."
        ),
    )


class WorkspaceDesktop(FrozenModel):
    """One of the user's desktops a route of this workspace's machine can name."""

    device_id: str = Field(description="The desktop's device id; the hop that names it in a route.")
    is_this_computer: bool = Field(description="Whether it is the computer this app runs on.")


class ConnectionPanel(FrozenModel):
    """One left-nav connection entry: a (service, account) pair with its toggle panels."""

    service_name: str = Field(description="Raw catalog service name (e.g. ``slack``); the revoke-all key.")
    display_name: str = Field(description="Human-readable service label.")
    account: str = Field(
        description='Latchkey account key (``""`` for the unnamed default); the rule-key half of grants.',
    )
    account_label: str = Field(description="User-facing account label (the default account reads as such).")
    is_connected: bool = Field(
        description=(
            "Whether latchkey stores credentials for the account. ``False`` for an account that only "
            "appears in the host's rules; its grants still render so they can be toggled off."
        ),
    )
    show_account_label: bool = Field(
        description="Whether the nav entry needs the account label to disambiguate (service has several accounts).",
    )
    granted_count: int = Field(description="Total permissions currently granted across the service's scopes.")
    scopes: tuple[ScopeTogglePanel, ...] = Field(description="One toggle panel per scope the service exposes.")
    sign_in: ServiceSignIn = Field(
        description="How the service's *next* account is connected; identical across its accounts.",
    )
    desktop_egress: DesktopEgressSetting = Field(
        description="The service's desktop egress setting; identical across its accounts.",
    )


class AvailableConnection(FrozenModel):
    """A catalog service with no signed-in account yet, offered by ``Add connection``."""

    service_name: str = Field(description="Raw catalog service name; posted to the add-account route.")
    display_name: str = Field(description="Human-readable service label.")
    sign_in: ServiceSignIn = Field(description="How connecting this service establishes its credentials.")


class SelfPermissionToggle(FrozenModel):
    """One ``latchkey-self`` toggle row (a shared path or a cross-workspace verb)."""

    permission: str = Field(description="Full detent permission name on the ``latchkey-self`` rule.")
    label: str = Field(description="Primary row label (the shared path, or the verb display name).")
    detail: str = Field(
        default="",
        description="Secondary label (access level for paths; target workspace for targeted verbs).",
    )
    description: str = Field(default="", description="Plain-English summary shown under the label.")
    is_granted: bool = Field(description="Whether the ``latchkey-self`` rule currently carries the name.")
    can_enable: bool = Field(
        description=(
            "Whether the toggle can be turned on: the permission's schema definition still exists in "
            "the host file. When ``False`` the agent must request the grant again."
        ),
    )


class WorkspacePermissionsView(FrozenModel):
    """Everything the Permissions tab renders for one workspace."""

    host_id: str = Field(description="Host whose permissions file the toggles edit.")
    connections: tuple[ConnectionPanel, ...] = Field(description="Connected / granted (service, account) pairs.")
    available_connections: tuple[AvailableConnection, ...] = Field(
        description="Catalog services with no account yet, offered by Add connection.",
    )
    file_sharing_toggles: tuple[SelfPermissionToggle, ...] = Field(description="Shared-path toggle rows.")
    workspace_toggles: tuple[SelfPermissionToggle, ...] = Field(description="Cross-workspace verb toggle rows.")
    desktops: tuple[WorkspaceDesktop, ...] = Field(
        description=(
            "The desktops a desktop egress route can name: this computer first, then the others the "
            "workspace's routes already name. Empty when desktop egress is unsupported."
        ),
    )
    is_credential_store_shared: bool = Field(
        description=(
            "Whether this machine's credentials live in this computer's store, which every local "
            "machine reads -- so signing an account out here signs it out of all of them. False "
            "when the machine holds its own credentials, where a sign-out reaches only it."
        ),
    )


@pure
def _humanize_tokens(tokens: Sequence[str]) -> str:
    """Join name tokens into a lowercase phrase (``("chat", "read")`` is handled by the caller)."""
    return " ".join(tokens)


@pure
def _strip_service_prefix(permission: str, prefixes: Sequence[str]) -> str:
    """Strip the longest matching ``<prefix>-`` from a permission name (or return it unchanged)."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if permission.startswith(f"{prefix}-"):
            return permission[len(prefix) + 1 :]
    return permission


@pure
def classify_permission(permission: str, scope: str, service_name: str) -> tuple[str, str, str]:
    """Derive ``(group_key, group_heading, label)`` for a catalog permission name.

    Permission names across the catalog follow a handful of conventions --
    ``slack-chat-read`` (verb-last), ``github-read-repos`` (verb-first),
    ``<service>-read-all`` / ``-write-all`` (whole-scope), bare ``aws-s3``
    (no verb) -- so the mapping is heuristic: names are grouped by the object
    they act on and labelled "<Verb> <object>". Unrecognized shapes fall back
    to a group of their own, so nothing is ever dropped. The detent catch-all
    ``any`` always lands in the trailing Extras group.
    """
    if permission == WILDCARD_PERMISSION_NAME:
        return _EXTRAS_GROUP_KEY, _EXTRAS_HEADING, "Everything (unrestricted)"
    remainder = _strip_service_prefix(permission, (scope, service_name))
    if remainder in ("read-all", "read"):
        return _FULL_ACCESS_GROUP_KEY, _FULL_ACCESS_HEADING, "Read everything"
    if remainder in ("write-all", "write"):
        return _FULL_ACCESS_GROUP_KEY, _FULL_ACCESS_HEADING, "Change everything"
    if remainder in ("everything", "all"):
        return _FULL_ACCESS_GROUP_KEY, _FULL_ACCESS_HEADING, "Everything"
    tokens = [token for token in remainder.split("-") if token]
    if not tokens:
        return remainder, remainder, permission
    if len(tokens) > 1 and tokens[-1] == "read":
        subject = _humanize_tokens(tokens[:-1])
        return subject, subject.capitalize(), f"Read {subject}"
    if len(tokens) > 1 and tokens[-1] == "write":
        subject = _humanize_tokens(tokens[:-1])
        return subject, subject.capitalize(), f"Manage {subject}"
    if len(tokens) > 1 and tokens[0] in _LEADING_VERBS:
        subject = _humanize_tokens(tokens[1:])
        return subject, subject.capitalize(), f"{tokens[0].capitalize()} {subject}"
    subject = _humanize_tokens(tokens)
    return subject, subject.capitalize(), subject.capitalize()


@pure
def group_permissions_by_area(info: ServicePermissionInfo) -> tuple[PermissionAreaGroup, ...]:
    """Group one scope's grantable permissions under the headings the UI renders.

    Iterates the catalog's declared order (``any`` is index 0 but always lands
    in the trailing Extras group); group order is full access first, then first
    appearance, extras last. Shared by the Permissions tab and the grant
    dialog so the same service reads the same way in both places.
    """
    permissions_by_group: dict[str, tuple[str, list[ClassifiedPermission]]] = {}
    for permission in info.permission_schemas:
        group_key, heading, label = classify_permission(permission, info.scope, info.name)
        _, classified = permissions_by_group.setdefault(group_key, (heading, []))
        classified.append(ClassifiedPermission(permission=permission, label=label))
    ordered_keys = sorted(
        permissions_by_group,
        key=lambda key: (
            key != _FULL_ACCESS_GROUP_KEY,
            key == _EXTRAS_GROUP_KEY,
            tuple(permissions_by_group).index(key),
        ),
    )
    return tuple(
        PermissionAreaGroup(
            heading=permissions_by_group[key][0],
            is_extras=key == _EXTRAS_GROUP_KEY,
            permissions=tuple(permissions_by_group[key][1]),
        )
        for key in ordered_keys
    )


def _build_scope_panel(info: ServicePermissionInfo, granted: frozenset[str]) -> ScopeTogglePanel:
    """Render one scope's grouped permissions as toggle rows carrying their host state."""
    groups = tuple(
        PermissionToggleGroup(
            heading=group.heading,
            toggles=tuple(
                PermissionToggle(
                    permission=classified.permission,
                    label=classified.label,
                    description=info.description_by_permission_name.get(classified.permission, ""),
                    is_granted=classified.permission in granted,
                )
                for classified in group.permissions
            ),
        )
        for group in group_permissions_by_area(info)
    )
    return ScopeTogglePanel(scope=info.scope, heading=info.display_name, groups=groups)


def _granted_by_scope_account(
    services_catalog: ServicesCatalog,
    config: LatchkeyPermissionsConfig,
) -> dict[tuple[str, str], frozenset[str]]:
    """Map every catalog-service grant in the file to ``(scope, account) -> permissions``."""
    granted: dict[tuple[str, str], frozenset[str]] = {}
    for grant in services_catalog.list_service_account_grants(config):
        key = (grant.scope, grant.account)
        granted[key] = granted.get(key, frozenset()) | frozenset(grant.permissions)
    return granted


def _parse_service_credential_command(
    service_name: str, set_credentials_example: str, credential_header: str | None
) -> ParsedCredentialCommand:
    """The command a service's credentials are collected and stored with.

    A custom service registered with a header of its own is collected and stored
    under that header; every other service uses its reported example, falling back
    to the generic bearer-token invocation when it suggested none. Raises
    :class:`CredentialCommandError` when what is left carries no ``<placeholder>``
    to fill in (or is not a latchkey command at all), which is the one case where
    there is nothing to collect.
    """
    return parse_credential_command_example(
        set_credentials_example_for(service_name, set_credentials_example or None, credential_header)
    )


def _build_service_sign_in(
    service_name: str,
    service_info: ServiceSignInOptions | None,
    is_account_stored: bool,
    credential_header: str | None,
) -> ServiceSignIn:
    """Work out how a service's next account gets connected.

    ``service_info`` is ``None`` for a service whose probe did not report,
    which leaves the browser sign-in offered -- the same thing latchkey's own
    ``is_browser_auth_supported`` does when it has no auth options to go on,
    and the behaviour every service had before credentials could be typed in.
    """
    if service_info is None or service_info.is_browser_auth_supported:
        return ServiceSignIn(is_browser_supported=True, credential_parameters=(), is_account_name_required=False)
    try:
        parameters = _parse_service_credential_command(
            service_name, service_info.set_credentials_example, credential_header
        ).parameters
    except CredentialCommandError as e:
        # No form can be offered for this one; the pane says so on the row.
        logger.warning("Cannot offer a credential form for {}: {}", service_name, e)
        parameters = ()
    return ServiceSignIn(
        is_browser_supported=False,
        credential_parameters=parameters,
        is_account_name_required=is_account_stored,
    )


def _sorted_panel_accounts(stored: Sequence[str], granted: frozenset[str]) -> tuple[str, ...]:
    """Order a service's accounts for the nav: stored ones first (as reported), granted-only after."""
    extra = sorted(
        (account for account in granted if account not in stored),
        key=lambda account: (account == DEFAULT_ACCOUNT, account.lower()),
    )
    return tuple(stored) + tuple(extra)


@pure
def describe_why_desktop_egress_is_unsupported(device_id: str, is_machine_of_its_own: bool) -> str | None:
    """The reason a workspace's desktop egress toggles cannot be offered, or ``None`` when they can."""
    if not device_id:
        return (
            "Imbue Studio does not know this computer's device id, so it cannot send requests through this computer."
        )
    if not is_machine_of_its_own:
        return (
            "This workspace does not run on a machine of its own that Imbue Studio has set up from this computer, "
            "so its requests cannot be sent through this computer."
        )
    return None


@pure
def build_desktop_egress_setting(
    device_id: str,
    is_machine_of_its_own: bool,
    # The service's route according to this computer's copy of the machine's rules file.
    route: DesktopEgressRoute,
) -> DesktopEgressSetting:
    """Decide whether one service's setting is offered, and what it shows."""
    if describe_why_desktop_egress_is_unsupported(device_id, is_machine_of_its_own) is not None:
        return DesktopEgressSetting(
            is_supported=False, mode=DesktopEgressMode.OFF, route=SELF_ONLY_DESKTOP_EGRESS_ROUTE.hops
        )
    return DesktopEgressSetting(
        is_supported=True, mode=desktop_egress_mode_for_route(route, device_id), route=route.hops
    )


class DesktopEgressGrantChanges(FrozenModel):
    """The writes that bring a permissions file's desktop egress grants in line with the routes."""

    grants_to_write: tuple[tuple[str, tuple[str, ...], dict[str, JsonValue]], ...] = Field(
        description="One ``(rule_key, permissions, schemas)`` triple per grant a route needs and the file lacks.",
    )
    rule_keys_to_delete: tuple[str, ...] = Field(
        description="The keys of the grants no route needs any more.",
    )


@pure
def plan_desktop_egress_grant_changes(
    config: LatchkeyPermissionsConfig,
    route_by_service_name: Mapping[str, DesktopEgressRoute],
    infos_by_service_name: Mapping[str, Sequence[ServicePermissionInfo]],
) -> DesktopEgressGrantChanges:
    """Work out which grants to write and which to delete so that ``config`` holds what the routes need.

    A route needs one grant per scope of its service for each desktop it sends
    requests to (:func:`desktop_egress_forwarders`). A grant on a scope of no
    service the catalog here knows is left alone: it may back the route of a
    service that only another of the user's computers has registered.

    Raises :class:`DesktopEgressError` when a needed grant cannot be built.
    """
    needed_grant_by_rule_key: dict[str, tuple[str, tuple[str, ...], dict[str, JsonValue]]] = {}
    for service_name, route in route_by_service_name.items():
        for info in infos_by_service_name.get(service_name, ()):
            for forwarder_device_id in desktop_egress_forwarders(route):
                grant = build_desktop_egress_grant(info.scope, forwarder_device_id, info.scope_schema)
                needed_grant_by_rule_key[grant[0]] = grant
    existing_grants = list_desktop_egress_grants(config)
    existing_rule_keys = frozenset(grant.rule_key for grant in existing_grants)
    catalog_scopes = frozenset(info.scope for infos in infos_by_service_name.values() for info in infos)
    return DesktopEgressGrantChanges(
        grants_to_write=tuple(
            grant for rule_key, grant in needed_grant_by_rule_key.items() if rule_key not in existing_rule_keys
        ),
        rule_keys_to_delete=tuple(
            grant.rule_key
            for grant in existing_grants
            if grant.scope in catalog_scopes and grant.rule_key not in needed_grant_by_rule_key
        ),
    )


def _has_machine_of_its_own(data_dir: Path, host_id: HostId) -> bool:
    try:
        return has_machine_of_its_own(data_dir, host_id)
    except LatchkeyStoreError as e:
        raise PermissionToggleError(f"Could not open the machine store of host '{host_id}': {e}") from e


@pure
def resolve_desktop_egress_routes(
    rules: DesktopEgressRules,
    # Every desktop egress grant in the host's permissions file, in file order.
    grants: Sequence[DesktopEgressGrant],
    infos_by_service_name: Mapping[str, Sequence[ServicePermissionInfo]],
) -> dict[str, DesktopEgressRoute]:
    """The route of every service a rules file sends anywhere but the machine.

    A service a file written before routes turned on has no route in the file;
    its route follows from the grants. One the catalog here does not know has
    no scopes to look grants up by, so it is left at the machine itself.
    """
    route_by_service_name = dict(rules.route_by_service_name)
    # CLEANUP: drop this loop with ``DesktopEgressRules.legacy_enabled_service_names``, once no machine
    # holds a rules file written before routes.
    for service_name in rules.legacy_enabled_service_names:
        infos = infos_by_service_name.get(service_name, ())
        if not infos:
            continue
        route = desktop_egress_route_from_grants(tuple(info.scope for info in infos), grants)
        if route != SELF_ONLY_DESKTOP_EGRESS_ROUTE:
            route_by_service_name[service_name] = route
    return route_by_service_name


def _load_desktop_egress_rules_copy(
    data_dir: Path,
    host_id: HostId,
    # The host's permissions file.
    config: LatchkeyPermissionsConfig,
    infos_by_service_name: Mapping[str, Sequence[ServicePermissionInfo]],
) -> dict[str, DesktopEgressRoute]:
    """The routes this computer's copy of the machine's rules file gives, none when it has no copy.

    Raises :class:`PermissionToggleError` for a copy that cannot be read or is not a rules file.
    """
    try:
        rules_json = read_host_desktop_egress_rules(data_dir, host_id)
    except LatchkeyStoreError as e:
        raise PermissionToggleError(f"Could not read the desktop egress rules of host '{host_id}': {e}") from e
    if rules_json is None:
        return {}
    try:
        rules = parse_desktop_egress_rules(rules_json)
    except DesktopEgressError as e:
        raise PermissionToggleError(f"Could not parse the desktop egress rules of host '{host_id}': {e}") from e
    return resolve_desktop_egress_routes(rules, list_desktop_egress_grants(config), infos_by_service_name)


def read_workspace_desktop_egress_routes(
    data_dir: Path,
    host_id: HostId,
    # The host's permissions file.
    config: LatchkeyPermissionsConfig,
    infos_by_service_name: Mapping[str, Sequence[ServicePermissionInfo]],
) -> dict[str, DesktopEgressRoute]:
    """The routes this computer's copy of the machine's rules file gives, none when it has no copy or cannot use it."""
    try:
        return _load_desktop_egress_rules_copy(data_dir, host_id, config, infos_by_service_name)
    except PermissionToggleError as e:
        logger.warning("{}; treating nothing as routed", e)
        return {}


@pure
def list_workspace_desktops(
    # This install's device id; empty when the app was built without one.
    device_id: str,
    # Every route of the workspace's machine.
    route_by_service_name: Mapping[str, DesktopEgressRoute],
) -> tuple[WorkspaceDesktop, ...]:
    """The desktops a route of this workspace's machine can name: this computer, then the others its routes name.

    The app has no list of the user's computers, so another one can be offered
    only once a route names it, which that computer itself does first.
    """
    other_device_ids = dict.fromkeys(
        hop
        for service_name in sorted(route_by_service_name)
        for hop in route_by_service_name[service_name].hops
        if hop != SELF_HOP and hop != device_id
    )
    this_computer = (WorkspaceDesktop(device_id=device_id, is_this_computer=True),) if device_id else ()
    return this_computer + tuple(
        WorkspaceDesktop(device_id=other_device_id, is_this_computer=False) for other_device_id in other_device_ids
    )


def describe_why_workspace_desktop_egress_is_unsupported(
    data_dir: Path, host_id: HostId, device_id: str
) -> str | None:
    """Why no route but the machine itself can be set for this host's services, or ``None`` when one can.

    Raises :class:`PermissionToggleError` when the host's machine store cannot be opened.
    """
    return describe_why_desktop_egress_is_unsupported(device_id, _has_machine_of_its_own(data_dir, host_id))


def build_workspace_permissions_view(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    machine_latchkey: Latchkey,
    workspace_agent_id: str,
    # This install's device id; empty when the app was built without one.
    device_id: str,
) -> WorkspacePermissionsView:
    """Assemble the Permissions tab's full view for one workspace.

    ``latchkey`` is the desktop's, which owns the permission files of every
    machine; ``machine_latchkey`` is the store holding *this* machine's
    credentials (see
    :mod:`imbue.minds.desktop_client.latchkey.machine_latchkey`), which is what
    the connections are read from -- a machine's connectors are the accounts
    connected for it, not the ones connected on the user's computer.

    Reads the workspace host's permissions file once (through the gateway
    extension) and one ``latchkey auth list --offline`` snapshot. A connection
    panel exists for every (service, account) pair that either has stored
    credentials or still appears in the host's rules, so no grant is invisible;
    services with no account at all are offered under Add connection. The nav
    reads services alphabetically, and within one service the accounts in
    :func:`_sorted_panel_accounts`' order. Every
    offered service carries how its next account is connected
    (:func:`_build_service_sign_in`), so the pane never offers a browser sign-in
    for a service latchkey cannot sign in to. Every connection also carries its
    service's desktop egress setting (:func:`build_desktop_egress_setting`),
    read from this computer's copy of the machine's rules file
    (:func:`read_workspace_desktop_egress_routes`).

    Raises :class:`PermissionToggleError` for an unresolvable workspace and
    lets :class:`LatchkeyGatewayClientError` propagate when the gateway cannot
    be reached (the route renders the unavailable state for both).
    """
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot load permissions.",
        )
    config = gateway_client.get_permissions_config(permissions_path_for_host(latchkey.plugin_data_dir, host_id))
    granted = _granted_by_scope_account(services_catalog, config)
    desktop_egress_route_by_service_name = read_workspace_desktop_egress_routes(
        latchkey.plugin_data_dir, host_id, config, services_catalog.as_mapping()
    )
    is_machine_of_its_own = _has_machine_of_its_own(latchkey.plugin_data_dir, host_id)
    accounts_by_service = machine_latchkey.auth_list(is_offline=True)
    # Every catalog service is offered somewhere in the pane -- under Add
    # connection when it has no account, under Add another account when it has
    # one -- so how each one connects has to be known here. The answers are
    # remembered per process, so this costs a burst of probes once rather than
    # on every toggle write (each of which rebuilds this payload). They are
    # probed against the desktop's store because they are properties of the
    # latchkey binary and the service catalog, identical for every machine.
    service_info_by_name = probe_service_sign_in_options(
        latchkey,
        tuple(service_name for service_name, infos in services_catalog.as_mapping().items() if infos),
    )
    # Read once for the whole pane: a custom service's credential header lives on the
    # desktop's registration, not in the probe, and the loop below asks for every service.
    registered_services = read_registered_services(latchkey.latchkey_directory)

    # Each panel carries its position within its own service, so the nav's
    # final sort orders services alphabetically without flattening the account
    # order _sorted_panel_accounts chose inside each one.
    connections: list[tuple[str, int, ConnectionPanel]] = []
    available: list[AvailableConnection] = []
    for service_name, infos in services_catalog.as_mapping().items():
        if not infos:
            continue
        display_name = infos[0].service_display_name
        stored = tuple(entry.account for entry in accounts_by_service.get(service_name, ()))
        granted_accounts = frozenset(
            account for (scope, account) in granted if any(info.scope == scope for info in infos)
        )
        panel_accounts = _sorted_panel_accounts(stored, granted_accounts)
        service_info = service_info_by_name.get(service_name)
        sign_in = _build_service_sign_in(
            service_name,
            service_info,
            is_account_stored=bool(stored),
            credential_header=custom_service_credential_header(registered_services, service_name),
        )
        if not panel_accounts:
            available.append(
                AvailableConnection(service_name=service_name, display_name=display_name, sign_in=sign_in)
            )
            continue
        desktop_egress = build_desktop_egress_setting(
            device_id=device_id,
            is_machine_of_its_own=is_machine_of_its_own,
            route=desktop_egress_route_for_service(desktop_egress_route_by_service_name, service_name),
        )
        for position, account in enumerate(panel_accounts):
            scopes = tuple(_build_scope_panel(info, granted.get((info.scope, account), frozenset())) for info in infos)
            connections.append(
                (
                    display_name.lower(),
                    position,
                    ConnectionPanel(
                        service_name=service_name,
                        display_name=display_name,
                        account=account,
                        account_label=account_label(account),
                        is_connected=account in stored,
                        show_account_label=len(panel_accounts) > 1,
                        granted_count=sum(len(granted.get((info.scope, account), frozenset())) for info in infos),
                        scopes=scopes,
                        sign_in=sign_in,
                        desktop_egress=desktop_egress,
                    ),
                )
            )

    return WorkspacePermissionsView(
        host_id=str(host_id),
        connections=tuple(panel for _, _, panel in sorted(connections, key=lambda row: (row[0], row[1]))),
        available_connections=tuple(sorted(available, key=lambda entry: entry.display_name.lower())),
        file_sharing_toggles=build_file_sharing_toggles(config, device_id),
        workspace_toggles=build_workspace_toggles(backend_resolver, config),
        desktops=(
            list_workspace_desktops(device_id, desktop_egress_route_by_service_name)
            if describe_why_desktop_egress_is_unsupported(device_id, is_machine_of_its_own) is None
            else ()
        ),
        is_credential_store_shared=not is_machine_store_of_its_own(latchkey, machine_latchkey),
    )


@pure
def _self_rule_permissions(config: LatchkeyPermissionsConfig) -> tuple[str, ...]:
    """Every permission name on the ``latchkey-self`` rule, in file order (duplicate keys unioned)."""
    merged: list[str] = []
    for rule in config.rules:
        for permission in rule.get(SELF_SCOPE, []):
            if permission not in merged:
                merged.append(permission)
    return tuple(merged)


@pure
def build_file_sharing_toggles(
    config: LatchkeyPermissionsConfig,
    # This install's device id; empty when the app was built without one, which then lists no paths of its own.
    device_id: str,
) -> tuple[SelfPermissionToggle, ...]:
    """Build the Local files toggle rows: this desktop's granted paths plus its revoked-but-restorable ones.

    Candidates are the union of the ``minds-file-server-*`` names on the
    ``latchkey-self`` rule (granted) and the same-shaped names in the file's
    ``schemas`` object -- revocation leaves the per-path schema behind, which is
    exactly what makes an off toggle re-enableable. Only grants of this
    desktop's files are rows: a remote workspace's policy also carries what the
    user's other desktops shared with it, which this one neither serves nor can
    sync. Sorted by path, read before write.

    A grant from before grants named a desktop is a row as well while it is
    granted, because this desktop serves the path it names at the device-less
    URL. It cannot be turned back on once revoked.
    """
    granted = frozenset(_self_rule_permissions(config))
    rows: list[tuple[str, FileSharingAccess, SelfPermissionToggle]] = []
    for name in granted | frozenset(config.schemas):
        grant = parse_file_sharing_permission(name)
        if grant is None or grant.device_id != device_id:
            continue
        rows.append(
            (
                grant.path,
                grant.access,
                SelfPermissionToggle(
                    permission=name,
                    label=grant.path,
                    detail=_file_sharing_access_label(grant.access),
                    is_granted=name in granted,
                    can_enable=name in config.schemas,
                ),
            )
        )
    # CLEANUP: drop this loop once no policy carries a device-less file-sharing grant, i.e. once a
    # permissions migration has deleted them.
    for name in granted:
        legacy_grant = parse_legacy_file_sharing_permission(name)
        if legacy_grant is None:
            continue
        rows.append(
            (
                legacy_grant.path,
                legacy_grant.access,
                SelfPermissionToggle(
                    permission=name,
                    label=legacy_grant.path,
                    detail=_file_sharing_access_label(legacy_grant.access),
                    is_granted=True,
                    can_enable=False,
                ),
            )
        )
    return tuple(toggle for _, _, toggle in sorted(rows, key=lambda row: (row[0], row[1])))


@pure
def _file_sharing_access_label(access: FileSharingAccess) -> str:
    return FILE_SHARING_WRITE_LABEL if access == FileSharingAccess.WRITE else FILE_SHARING_READ_LABEL


def build_workspace_toggles(
    backend_resolver: BackendResolverInterface,
    config: LatchkeyPermissionsConfig,
) -> tuple[SelfPermissionToggle, ...]:
    """Build the Other machines toggle rows: granted verbs plus revoked-but-restorable ones.

    Same union-of-rule-and-schemas construction as
    :func:`build_file_sharing_toggles`. Rows follow the verb catalog's order;
    a targeted verb gets one row per target, labelled with the target
    workspace's display name.
    """
    granted = frozenset(_self_rule_permissions(config))
    candidates = {name for name in granted if parse_workspace_permission(name) is not None}
    candidates.update(name for name in config.schemas if parse_workspace_permission(name) is not None)
    verb_order = {verb.permission: index for index, verb in enumerate(WORKSPACE_VERBS)}
    verb_by_permission = {verb.permission: verb for verb in WORKSPACE_VERBS}
    rows: list[tuple[int, str, SelfPermissionToggle]] = []
    for name in candidates:
        parsed = parse_workspace_permission(name)
        if parsed is None:
            continue
        verb_permission, target = parsed
        verb = verb_by_permission[verb_permission]
        detail = "All machines" if target is None else resolve_target_workspace_name(backend_resolver, target)
        rows.append(
            (
                verb_order[verb_permission],
                detail.lower(),
                SelfPermissionToggle(
                    permission=name,
                    label=verb.display_name,
                    detail=detail,
                    description=verb.description,
                    is_granted=name in granted,
                    can_enable=name in config.schemas,
                ),
            )
        )
    return tuple(toggle for _, _, toggle in sorted(rows, key=lambda row: (row[0], row[1])))


@pure
def compute_connector_permissions(
    info: ServicePermissionInfo,
    current: Sequence[str],
    permission: str,
    enabled: bool,
) -> tuple[str, ...]:
    """Recompute the full permission set for one connector rule after a toggle flip.

    The result is complete (never a diff): catalog permissions in catalog
    order with the toggled one added or removed, followed by any names in the
    current grant that the catalog does not know (hand-edited entries are
    preserved verbatim rather than silently dropped). Raises
    :class:`PermissionToggleError` for a permission outside the scope's
    catalog surface -- defence-in-depth against a crafted request.
    """
    if permission not in info.permission_schemas:
        raise PermissionToggleError(
            f"Permission '{permission}' is not grantable for scope '{info.scope}'.",
        )
    membership = set(current)
    if enabled:
        membership.add(permission)
    else:
        membership.discard(permission)
    known = [name for name in info.permission_schemas if name in membership]
    unknown = [name for name in current if name not in info.permission_schemas and name in membership]
    return tuple(known + unknown)


def apply_connector_toggle(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
    scope: str,
    account: str,
    permission: str,
    enabled: bool,
    push_permissions_to_machine: Callable[[str], None],
) -> None:
    """Flip one connector permission for ``(scope, account)`` on the workspace's host.

    Reads the host file, recomputes the affected rule's complete permission
    set from the flip, and writes it back with the generated per-account
    schema (:func:`build_account_grant`). Turning the last permission off
    deletes the rule instead of leaving an empty one behind, matching the
    revoke paths. The edited policy is then pushed to the workspace's own
    machine, and this does not return until it lands there. Raises
    :class:`PermissionToggleError` for unknown scopes / permissions /
    unresolvable workspaces; gateway failures propagate as
    :class:`LatchkeyGatewayClientError` and a machine that would not take the
    policy as :class:`MachineOperationError`.
    """
    info = services_catalog.get_by_scope(scope)
    if info is None:
        raise PermissionToggleError(f"Unknown scope '{scope}'.")
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot change permissions.",
        )
    path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    config = gateway_client.get_permissions_config(path)
    current: tuple[str, ...] = ()
    current_rule_key: str | None = None
    for grant in list_account_grants(config):
        if grant.scope == scope and grant.account == account:
            current = current + tuple(name for name in grant.permissions if name not in current)
            current_rule_key = grant.rule_key
    updated = compute_connector_permissions(info, current, permission, enabled)
    if tuple(updated) == current:
        return
    rule_key, granted_permissions, schemas = build_account_grant(scope, account, updated, info.scope_schema)
    if not updated:
        gateway_client.delete_permission_rule(path, current_rule_key or rule_key)
    else:
        gateway_client.set_permission_rule(path, rule_key, granted_permissions, schemas)
    push_permissions_to_machine(workspace_agent_id)


def _write_desktop_egress_rules_copy(
    data_dir: Path, host_id: HostId, route_by_service_name: Mapping[str, DesktopEgressRoute]
) -> None:
    rules_path = desktop_egress_rules_path_for_host(data_dir, host_id)
    try:
        atomic_write(rules_path, serialize_desktop_egress_rules(route_by_service_name))
    except OSError as e:
        raise PermissionToggleError(f"Could not store the desktop egress rules at {rules_path}: {e}") from e


def apply_desktop_egress_route(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
    service_name: str,
    route: DesktopEgressRoute,
    # This install's device id; empty when the app was built without one.
    device_id: str,
    push_permissions_and_desktop_egress_rules_to_machine: Callable[[str], None],
) -> None:
    """Set where one service's requests from the workspace leave from.

    The route takes the place of the service's rule among the routes this
    computer's copy of the machine's rules file gives. The desktop egress
    grants in the host's permissions file are first brought in line with every
    one of those routes (:func:`plan_desktop_egress_grant_changes`), so each
    desktop a route names accepts its requests and no other does; the copy is
    written after them, whole and as routes, so a copy from before routes
    stops being one. Both files are then pushed to the machine together, and
    this does not return until they land there.

    Raises :class:`PermissionToggleError` for an unknown service, an
    unresolvable workspace, a route through a desktop where that is not
    supported, and a copy of the rules file that cannot be used; gateway
    failures propagate as :class:`LatchkeyGatewayClientError` and a machine
    that would not take the change as :class:`MachineOperationError`.
    """
    infos = services_catalog.get(service_name)
    if not infos:
        raise PermissionToggleError(f"Unknown service '{service_name}'.")
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot change permissions.",
        )
    data_dir = latchkey.plugin_data_dir
    path = permissions_path_for_host(data_dir, host_id)
    if route != SELF_ONLY_DESKTOP_EGRESS_ROUTE:
        unsupported_reason = describe_why_workspace_desktop_egress_is_unsupported(data_dir, host_id, device_id)
        if unsupported_reason is not None:
            raise PermissionToggleError(unsupported_reason)

    config = gateway_client.get_permissions_config(path)
    infos_by_service_name = services_catalog.as_mapping()
    # Every other service's route is rebuilt from the copy, so one that cannot be used must not read as empty.
    route_by_service_name = {
        **_load_desktop_egress_rules_copy(data_dir, host_id, config, infos_by_service_name),
        service_name: route,
    }
    try:
        changes = plan_desktop_egress_grant_changes(config, route_by_service_name, infos_by_service_name)
    except DesktopEgressError as e:
        raise PermissionToggleError(f"Could not build the grants this workspace's routes need: {e}") from e
    for rule_key, permissions, schemas in changes.grants_to_write:
        gateway_client.set_permission_rule(path, rule_key, permissions, schemas)
    for rule_key in changes.rule_keys_to_delete:
        gateway_client.delete_permission_rule(path, rule_key)
    _write_desktop_egress_rules_copy(data_dir, host_id, route_by_service_name)
    push_permissions_and_desktop_egress_rules_to_machine(workspace_agent_id)


class DesktopEgressRouteThroughThisComputer(FrozenModel):
    """What putting this computer first on a service's route came to."""

    route: DesktopEgressRoute = Field(description="The route the service has afterwards.")
    is_this_computer_added: bool = Field(
        description="Whether the route was changed; false when it already named this computer.",
    )


def put_this_computer_first_on_desktop_egress_route(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
    service_name: str,
    # This install's device id; empty when the app was built without one.
    device_id: str,
    push_permissions_and_desktop_egress_rules_to_machine: Callable[[str], None],
) -> DesktopEgressRouteThroughThisComputer:
    """Make this computer the first place one service's requests from the workspace leave from.

    This computer is put ahead of whatever route the service has, so the places
    that route already names stay on it as what is tried next. A route that
    already names this computer, wherever on it, is left as it is and nothing
    is written.

    Raises and propagates what :func:`apply_desktop_egress_route` does.
    """
    if not services_catalog.get(service_name):
        raise PermissionToggleError(f"Unknown service '{service_name}'.")
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot change permissions.",
        )
    data_dir = latchkey.plugin_data_dir
    unsupported_reason = describe_why_workspace_desktop_egress_is_unsupported(data_dir, host_id, device_id)
    if unsupported_reason is not None:
        raise PermissionToggleError(unsupported_reason)
    current_route = desktop_egress_route_for_service(
        _load_desktop_egress_rules_copy(
            data_dir,
            host_id,
            gateway_client.get_permissions_config(permissions_path_for_host(data_dir, host_id)),
            services_catalog.as_mapping(),
        ),
        service_name,
    )
    if device_id in current_route.hops:
        return DesktopEgressRouteThroughThisComputer(route=current_route, is_this_computer_added=False)
    try:
        route = build_desktop_egress_route((device_id, *current_route.hops))
    except DesktopEgressError as e:
        raise PermissionToggleError(f"Could not put this computer on the route: {e}") from e
    apply_desktop_egress_route(
        backend_resolver=backend_resolver,
        gateway_client=gateway_client,
        services_catalog=services_catalog,
        latchkey=latchkey,
        workspace_agent_id=workspace_agent_id,
        service_name=service_name,
        route=route,
        device_id=device_id,
        push_permissions_and_desktop_egress_rules_to_machine=push_permissions_and_desktop_egress_rules_to_machine,
    )
    return DesktopEgressRouteThroughThisComputer(route=route, is_this_computer_added=True)


def connect_service_with_credentials(
    latchkey: Latchkey,
    services_catalog: ServicesCatalog,
    service_name: str,
    value_by_parameter_name: Mapping[str, str],
    account_name: str,
) -> str:
    """Store credentials the user typed in as an account of ``service_name``, and return that account.

    The command that stores them comes from the service's own
    ``setCredentialsExample``; only the ``<placeholder>`` values are the
    user's, and they are never logged. The account it is pinned to -- which is
    what the returned name reports, so a caller can hand exactly that account
    onward -- is the name the user gave, or latchkey's unnamed default when the
    service has no stored account yet: the same rule the permission dialog
    follows.

    Nothing is granted here: the account joins the pane with no permissions,
    exactly as a completed browser sign-in does.

    Raises :class:`PermissionToggleError` for everything the user can act on --
    an unknown service, a service that signs in through a browser instead, a
    service whose credential command cannot be turned into inputs, a missing
    account name or value, a credential latchkey itself refused, and a probe
    that did not report (``None`` -- guessing how the service connects could
    only reject the typed credentials for the wrong reason).
    """
    infos = services_catalog.get(service_name)
    if not infos:
        raise PermissionToggleError(f"Unknown service '{service_name}'.")
    # Every message below is about the connection as a whole -- one credential
    # backs all of a service's scopes -- so it takes the service's own name.
    display_name = infos[0].service_display_name
    service_info = latchkey.services_info(service_name, is_offline=True)
    if service_info is None:
        # Everything below is read off this probe; without an answer there is
        # no way to tell a credentials service from a browser one, and the
        # wrong guess would reject what the user just typed.
        raise PermissionToggleError(
            f"Imbue Studio could not ask latchkey how {display_name} connects. Try again in a moment.",
        )
    if service_info.is_browser_auth_supported:
        raise PermissionToggleError(f"{display_name} is connected by signing in, not by entering credentials.")
    try:
        parsed_command = _parse_service_credential_command(
            service_name,
            service_info.set_credentials_example or "",
            custom_service_credential_header(read_registered_services(latchkey.latchkey_directory), service_name),
        )
    except CredentialCommandError as e:
        raise PermissionToggleError(
            f"Imbue Studio cannot work out which credentials {display_name} needs: {e}."
        ) from e

    # Which account the credentials land under is decided from what latchkey
    # stores right now, not from what the pane was rendered with. The first
    # account of a service is latchkey's unnamed default, which needs no name.
    is_account_name_required = bool(service_info.accounts)
    account = account_name.strip() if is_account_name_required else DEFAULT_ACCOUNT
    if is_account_name_required and not account:
        raise PermissionToggleError(f"Enter a name for the new {display_name} account.")
    try:
        argv = build_credential_command_argv(parsed_command, value_by_parameter_name, account)
    except CredentialCommandError as e:
        raise PermissionToggleError(f"The {display_name} credentials are incomplete: {e}.") from e

    is_success, detail = latchkey.auth_set_credentials(service_name, argv)
    if is_success:
        return account
    # The service itself usually says which value it did not like; its usage
    # lines (and any crash noise) are not worth showing.
    described_failure = describe_credential_command_failure(detail)
    raise PermissionToggleError(
        f"{display_name} rejected those credentials: {described_failure}"
        if described_failure
        else f"Storing the {display_name} credentials failed."
    )


@pure
def compute_self_permissions(
    config: LatchkeyPermissionsConfig,
    permission: str,
    enabled: bool,
) -> tuple[str, ...] | None:
    """Recompute the full ``latchkey-self`` permission list after a toggle flip.

    Only the ``minds-file-server-*`` / ``minds-workspaces-*`` names this screen
    owns may be flipped; everything else on the rule (baseline, accounts) is
    preserved verbatim, which is why the full list is recomputed here rather
    than trusted from the client. Returns ``None`` when the flip is a no-op.

    Turning a shared path off also drops the grant for the same path and access
    from before grants named a desktop, which would otherwise keep the path
    reachable at the device-less URL. Such a name is not itself toggleable, so
    nothing turns one on.

    Raises :class:`PermissionToggleError` for a name outside the toggleable
    families, and for enabling a name whose schema definition is no longer in
    the file (detent would fail the entire permission check on an unresolvable
    reference, taking every ``latchkey-self`` grant down with it).
    """
    file_sharing_grant = parse_file_sharing_permission(permission)
    if file_sharing_grant is None and parse_workspace_permission(permission) is None:
        raise PermissionToggleError(f"Permission '{permission}' is not toggleable from the permissions screen.")
    current = _self_rule_permissions(config)
    if enabled:
        if permission in current:
            return None
        if permission not in config.schemas:
            raise PermissionToggleError(
                f"Permission '{permission}' cannot be re-enabled because its definition is gone; "
                "ask the agent to request it again.",
            )
        return current + (permission,)
    revoked = {permission}
    # CLEANUP: revoke ``permission`` alone once no policy carries a device-less file-sharing grant, i.e.
    # once a permissions migration has deleted them.
    if file_sharing_grant is not None:
        revoked.add(
            legacy_file_sharing_permission_name(
                LegacyFileSharingGrant(access=file_sharing_grant.access, path=file_sharing_grant.path)
            )
        )
    if revoked.isdisjoint(current):
        return None
    return tuple(name for name in current if name not in revoked)


def apply_self_toggle(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
    workspace_agent_id: str,
    permission: str,
    enabled: bool,
    push_permissions_to_machine: Callable[[str], None],
) -> None:
    """Flip one ``latchkey-self`` toggle (shared path / cross-workspace verb) on the workspace's host.

    Rewrites the whole ``latchkey-self`` rule with the recomputed full list
    (unrelated names preserved); a no-op flip writes nothing. The edited policy
    is then pushed to the workspace's own machine, and this does not return
    until it lands there. Raises :class:`PermissionToggleError` per
    :func:`compute_self_permissions`; gateway failures propagate as
    :class:`LatchkeyGatewayClientError` and a machine that would not take the
    policy as :class:`MachineOperationError`.
    """
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot change permissions.",
        )
    path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    updated = compute_self_permissions(gateway_client.get_permissions_config(path), permission, enabled)
    if updated is None:
        return
    gateway_client.set_permission_rule(path, SELF_SCOPE, updated)
    push_permissions_to_machine(workspace_agent_id)
