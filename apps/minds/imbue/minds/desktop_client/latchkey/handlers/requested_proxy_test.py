"""Unit tests for the pure parts of :mod:`.requested_proxy`; its decisions are tested through the handlers."""

import json

import pytest

from imbue.minds.desktop_client.latchkey.handlers.requested_proxy import RequestedProxyError
from imbue.minds.desktop_client.latchkey.handlers.requested_proxy import _describe_route_for_agent
from imbue.minds.desktop_client.latchkey.handlers.requested_proxy import _format_proxy_not_set_up_note
from imbue.minds.desktop_client.latchkey.handlers.requested_proxy import _parse_proxy_form
from imbue.minds.desktop_client.latchkey.handlers.requested_proxy import format_route_note
from imbue.minds.desktop_client.latchkey.permission_toggles import DesktopEgressRouteThroughThisComputer
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_route

_THIS_DEVICE_ID: str = "desktop-this-2946"
_OTHER_DEVICE_ID: str = "desktop-other-8153"


@pytest.mark.parametrize(
    "hops,expected_description",
    [
        pytest.param((_THIS_DEVICE_ID,), f"the desktop {_THIS_DEVICE_ID}", id="one-desktop"),
        pytest.param(("self",), "your machine itself", id="the-machine-itself"),
        pytest.param(
            (_THIS_DEVICE_ID, _OTHER_DEVICE_ID, "self"),
            f"the desktop {_THIS_DEVICE_ID}, then the desktop {_OTHER_DEVICE_ID}, then your machine itself",
            id="desktops-then-the-machine",
        ),
    ],
)
def test_describe_route_for_agent_names_each_place_in_order(hops: tuple[str, ...], expected_description: str) -> None:
    assert _describe_route_for_agent(build_desktop_egress_route(hops)) == expected_description


def test_format_route_note_names_the_service_and_where_its_requests_leave_from() -> None:
    route = build_desktop_egress_route((_THIS_DEVICE_ID, "self"))

    added_note = format_route_note(
        "Slack", DesktopEgressRouteThroughThisComputer(route=route, is_this_computer_added=True)
    )
    unchanged_note = format_route_note(
        "Slack", DesktopEgressRouteThroughThisComputer(route=route, is_this_computer_added=False)
    )

    assert added_note == f" Requests to Slack now leave from the desktop {_THIS_DEVICE_ID}, then your machine itself."
    assert unchanged_note == (
        f" Requests to Slack already leave from the desktop {_THIS_DEVICE_ID}, then your machine itself, so the "
        "proxy you asked for changed nothing."
    )


def test_format_proxy_not_set_up_note_gives_the_reason() -> None:
    assert (
        _format_proxy_not_set_up_note("the user turned it off.")
        == " The proxy you asked for was not set up: the user turned it off."
    )


@pytest.mark.parametrize(
    "raw_proxy,expected_is_on",
    [
        pytest.param("true", True, id="on"),
        pytest.param("false", False, id="off"),
    ],
)
def test_parse_proxy_form_reads_the_two_values_of_the_switch(raw_proxy: str, expected_is_on: bool) -> None:
    assert _parse_proxy_form(raw_proxy) is expected_is_on


@pytest.mark.parametrize("raw_proxy", ["", "True", "1", "on", "null", json.dumps([_THIS_DEVICE_ID])])
def test_parse_proxy_form_rejects_anything_but_the_two_values_of_the_switch(raw_proxy: str) -> None:
    with pytest.raises(RequestedProxyError, match="must be 'true' or 'false'"):
        _parse_proxy_form(raw_proxy)
