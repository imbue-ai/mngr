"""A permission request's ask for a proxy: what its dialog offers, and what an approval does about it.

A request that grants a service may also ask for the service's requests to
leave through the computer that approves it (see
:mod:`imbue.mngr_latchkey.desktop_egress`). That is not a permission -- the
gateway carries the ask and never acts on it -- so the dialog offers it as a
switch, on because the agent asked, and when the switch was left on the handler
puts this computer first on the service's route in the same change that carries
the grant to the machine. A service whose route already names this computer
gets no switch: there is nothing to decide.
"""

from collections.abc import Callable
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.permission_toggles import DesktopEgressRouteThroughThisComputer
from imbue.minds.desktop_client.latchkey.permission_toggles import PermissionToggleError
from imbue.minds.desktop_client.latchkey.permission_toggles import describe_why_workspace_desktop_egress_is_unsupported
from imbue.minds.desktop_client.latchkey.permission_toggles import list_workspace_desktops
from imbue.minds.desktop_client.latchkey.permission_toggles import put_this_computer_first_on_desktop_egress_route
from imbue.minds.desktop_client.latchkey.permission_toggles import read_workspace_desktop_egress_routes
from imbue.minds.desktop_client.request_handler import UiRequestedProxy
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_models import UiWorkspaceDesktop
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.desktop_egress import DesktopEgressRoute
from imbue.mngr_latchkey.desktop_egress import SELF_HOP
from imbue.mngr_latchkey.desktop_egress import desktop_egress_route_for_service
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import permissions_path_for_host

# Form field carrying whether the dialog's proxy switch was on. Present only
# when the dialog drew the switch.
PROXY_FIELD: Final[str] = "proxy"
_PROXY_FIELD_ON: Final[str] = "true"
_PROXY_FIELD_OFF: Final[str] = "false"


PROXY_NO_LONGER_SUPPORTED_SENTENCE: Final[str] = (
    'This workspace can\'t send its requests through this computer any more. Turn off "Proxy through my desktop" '
    "and approve again."
)


class RequestedProxyError(Exception):
    """Raised when the dialog submitted a proxy choice the request or its workspace does not allow."""


class ProxyNoLongerSupportedError(RequestedProxyError):
    """Raised when the dialog's proxy switch was left on for a workspace that can no longer have a proxy."""


class DesktopEgressDecision(FrozenModel):
    """What an approval does about sending the service's requests through this computer, and what the agent hears."""

    is_this_computer_put_first: bool = Field(
        description=(
            "Whether this computer is put first on the service's desktop egress route, in the change that "
            "carries the grant to the machine. The agent is then told the route that results."
        ),
    )
    note: str = Field(
        description=(
            "A sentence appended to the agent's grant notice when this computer is not put on the route; "
            "empty when the request asked for no proxy."
        ),
    )


# The decision for a request that asked for no proxy.
NO_DESKTOP_EGRESS_CHANGE: Final[DesktopEgressDecision] = DesktopEgressDecision(
    is_this_computer_put_first=False, note=""
)


def _describe_route_for_agent(route: DesktopEgressRoute) -> str:
    """Spell a route out for the agent, naming desktops by the ids ``/devices`` gives it."""
    places: list[str] = []
    for hop in route.hops:
        if hop == SELF_HOP:
            places.append("your machine itself")
        else:
            places.append(f"the desktop {hop}")
    return ", then ".join(places)


def format_route_note(service_display_name: str, proxied: DesktopEgressRouteThroughThisComputer) -> str:
    """The sentence appended to the agent's grant notice once this computer has been put on the route."""
    described_route = _describe_route_for_agent(proxied.route)
    if proxied.is_this_computer_added:
        return f" Requests to {service_display_name} now leave from {described_route}."
    return (
        f" Requests to {service_display_name} already leave from {described_route}, so the proxy you asked for "
        "changed nothing."
    )


def format_proxy_failed_message(service_display_name: str) -> str:
    """What the dialog says when an approval stopped at sending the service's requests through this computer."""
    return (
        f"Imbue Studio couldn't send {service_display_name}'s requests through this computer, so nothing was "
        'approved yet. Try again, or turn off "Proxy through my desktop" and approve without it.'
    )


def _format_proxy_not_set_up_note(reason: str) -> str:
    return f" The proxy you asked for was not set up: {reason}"


def _parse_proxy_form(raw_proxy: str) -> bool:
    """Parse the dialog's proxy field. Raises :class:`RequestedProxyError` for anything but its two values."""
    if raw_proxy == _PROXY_FIELD_ON:
        return True
    elif raw_proxy == _PROXY_FIELD_OFF:
        return False
    else:
        raise RequestedProxyError(f"The submitted proxy choice must be {_PROXY_FIELD_ON!r} or {_PROXY_FIELD_OFF!r}.")


def _desktop_egress_unsupported_reason(latchkey: Latchkey, host_id: HostId) -> str | None:
    """Why no route but the machine itself can be set for this host's services, or ``None`` when one can."""
    try:
        return describe_why_workspace_desktop_egress_is_unsupported(
            latchkey.plugin_data_dir, host_id, get_state().device_id
        )
    except PermissionToggleError as e:
        logger.warning("Could not tell whether host {} supports desktop egress: {}", host_id, e)
        return str(e)


def _last_known_route(
    latchkey: Latchkey,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    host_id: HostId,
    service_name: str,
) -> DesktopEgressRoute | None:
    """The service's route in this computer's copies, or ``None`` when the host's permissions cannot be read."""
    data_dir = latchkey.plugin_data_dir
    try:
        config = gateway_client.get_permissions_config(permissions_path_for_host(data_dir, host_id))
    except LatchkeyGatewayClientError as e:
        logger.warning("Could not read the permissions of host {}; its routes are unknown: {}", host_id, e)
        return None
    return desktop_egress_route_for_service(
        read_workspace_desktop_egress_routes(data_dir, host_id, config, services_catalog.as_mapping()),
        service_name,
    )


def build_requested_proxy(
    latchkey: Latchkey,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    is_proxy_requested: bool,
    host_id: HostId | None,
    # The latchkey service the request grants, whether or not it is registered yet.
    service_name: str,
) -> UiRequestedProxy | None:
    """What the dialog draws the proxy switch from, or ``None`` when it draws none.

    It draws none when the agent asked for no proxy, when the workspace's
    host is not known yet, when no route through a desktop can be set for
    the workspace, when the host's permissions cannot be read, and when the
    service's route already names this computer: there is then nothing for
    the user to decide.
    """
    if not is_proxy_requested or host_id is None:
        return None
    if _desktop_egress_unsupported_reason(latchkey, host_id) is not None:
        return None
    last_known_route = _last_known_route(latchkey, gateway_client, services_catalog, host_id, service_name)
    device_id = get_state().device_id
    if last_known_route is None or device_id in last_known_route.hops:
        return None
    return UiRequestedProxy(
        resulting_route=(device_id, *last_known_route.hops),
        desktops=tuple(
            UiWorkspaceDesktop.model_validate(desktop.model_dump())
            for desktop in list_workspace_desktops(device_id, {service_name: last_known_route})
        ),
    )


def decide_desktop_egress(
    latchkey: Latchkey,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    is_proxy_requested: bool,
    host_id: HostId,
    # The latchkey service the request grants, whether or not it is registered yet.
    service_name: str,
    # The dialog's proxy field; ``None`` when it drew no proxy switch.
    raw_proxy: str | None,
) -> DesktopEgressDecision:
    """Turn the dialog's proxy field into what the grant does about the service's route.

    Raises :class:`RequestedProxyError` for a field that is not one of the
    switch's two values, for a field on a request that asked for no proxy, and
    for a proxy where the workspace cannot have one.
    """
    if not is_proxy_requested:
        if raw_proxy is not None:
            raise RequestedProxyError(
                "The request asked for no proxy, so the dialog cannot have submitted a choice about one.",
            )
        return NO_DESKTOP_EGRESS_CHANGE
    unsupported_reason = _desktop_egress_unsupported_reason(latchkey, host_id)
    if raw_proxy is None:
        if unsupported_reason is not None:
            return DesktopEgressDecision(
                is_this_computer_put_first=False, note=_format_proxy_not_set_up_note(unsupported_reason)
            )
        last_known_route = _last_known_route(latchkey, gateway_client, services_catalog, host_id, service_name)
        if last_known_route is None or get_state().device_id not in last_known_route.hops:
            # The dialog also draws no switch when it cannot read the route, and the user was
            # then never asked whether the service's requests may leave through this computer.
            return DesktopEgressDecision(
                is_this_computer_put_first=False,
                note=_format_proxy_not_set_up_note("the approval dialog could not offer it. Ask again to set it up."),
            )
        # The copy that names this computer can be ahead of the machine (a push it refused
        # leaves it so), so the grant goes by what the machine holds once it has been read.
        return DesktopEgressDecision(is_this_computer_put_first=True, note="")
    if not _parse_proxy_form(raw_proxy):
        return DesktopEgressDecision(
            is_this_computer_put_first=False,
            note=_format_proxy_not_set_up_note("the user turned it off."),
        )
    if unsupported_reason is not None:
        raise ProxyNoLongerSupportedError(unsupported_reason)
    return DesktopEgressDecision(is_this_computer_put_first=True, note="")


def put_this_computer_first_on_route(
    latchkey: Latchkey,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    workspace_agent_id: str,
    service_name: str,
    # The account just granted in the copy here, whose grant a changed route carries to the machine.
    granted_account: str,
    carry_grant_and_desktop_egress_rules_to_machine: Callable[[str, str, str], None],
) -> DesktopEgressRouteThroughThisComputer:
    """Put this computer first on the service's route for the workspace, and say what that came to.

    A route that is changed is pushed to the machine together with the grant
    of ``granted_account``, as one change. One that already names this
    computer is left as it is, and nothing is pushed.

    Raises :class:`PermissionToggleError` when the route cannot be set here
    and :class:`MachineOperationError` when the machine does not take it;
    gateway failures propagate as :class:`LatchkeyGatewayClientError`.
    """
    return put_this_computer_first_on_desktop_egress_route(
        backend_resolver=get_state().backend_resolver,
        gateway_client=gateway_client,
        services_catalog=services_catalog,
        latchkey=latchkey,
        workspace_agent_id=workspace_agent_id,
        service_name=service_name,
        device_id=get_state().device_id,
        push_permissions_and_desktop_egress_rules_to_machine=(
            lambda pushed_workspace_agent_id: carry_grant_and_desktop_egress_rules_to_machine(
                pushed_workspace_agent_id, service_name, granted_account
            )
        ),
    )


def carry_grant_with_desktop_egress(
    latchkey: Latchkey,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    workspace_agent_id: str,
    service_name: str,
    # The account just granted in the copy here.
    account: str,
    desktop_egress: DesktopEgressDecision,
    carry_grant_to_machine: Callable[[str, str, str], None],
    carry_grant_and_desktop_egress_rules_to_machine: Callable[[str, str, str], None],
    # what putting this computer first on the service's route came to, or ``None`` when that was not asked for
) -> DesktopEgressRouteThroughThisComputer | None:
    """Take a grant to the workspace's machine, with the service's new route when the approval sets one.

    A route that is changed takes the grant of ``account`` to the machine
    with it (see :func:`put_this_computer_first_on_route`). One that already
    names this computer is left as it is, and the grant travels without the
    rules, as it does when no route was asked for.

    Raises :class:`PermissionToggleError` when the route cannot be set here
    and :class:`MachineOperationError` when the machine does not take the
    change; gateway failures propagate as :class:`LatchkeyGatewayClientError`.
    """
    if not desktop_egress.is_this_computer_put_first:
        carry_grant_to_machine(workspace_agent_id, service_name, account)
        return None
    proxied = put_this_computer_first_on_route(
        latchkey=latchkey,
        gateway_client=gateway_client,
        services_catalog=services_catalog,
        workspace_agent_id=workspace_agent_id,
        service_name=service_name,
        granted_account=account,
        carry_grant_and_desktop_egress_rules_to_machine=carry_grant_and_desktop_egress_rules_to_machine,
    )
    if not proxied.is_this_computer_added:
        carry_grant_to_machine(workspace_agent_id, service_name, account)
    return proxied
