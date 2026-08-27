"""Tests for the gateway-backed pending-requests view."""

import json
from pathlib import Path

import httpx

from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.pending_requests import GatewayPendingRequests
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import append_response_event
from imbue.minds.desktop_client.latchkey.response_events import create_request_response_event


def _streamed_record(request_id: str, agent_id: str = "agent-abc") -> dict[str, object]:
    return {
        "request_id": request_id,
        "agent_id": agent_id,
        "rationale": "why",
        "request_type": "predefined",
        "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        "target": "/tmp/permissions.json",
        "effect": {"rules": [{"slack-api": ["slack-read-all"]}]},
    }


def _gateway_with(records: list[dict[str, object]], outage: list[bool] | None = None) -> LatchkeyGatewayClient:
    """A stub gateway serving ``records``; flip ``outage[0]`` to make it 502."""

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        if outage is not None and outage[0]:
            return httpx.Response(502, content=b"gateway down")
        payload = b"".join(json.dumps(r).encode() + b"\n" for r in records)
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-ndjson"})

    return LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(_handler),
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
    )


def test_list_pending_reads_the_gateway_newest_first_and_hides_answered(tmp_path: Path) -> None:
    """One gateway read; recorded verdicts never resurface as pending.

    The gateway keeps listing a request whose deny-time DELETE failed, so the
    verdict index -- not the gateway -- decides what still counts as pending.
    """
    view = GatewayPendingRequests.load(
        gateway_client=_gateway_with([_streamed_record("r-old"), _streamed_record("r-new")]),
        data_dir=tmp_path,
    )

    assert [req.request_id for req in view.list_pending()] == ["r-new", "r-old"]

    view.record_response(
        create_request_response_event(request_event_id="r-old", status=RequestStatus.DENIED, agent_id="agent-abc")
    )
    assert [req.request_id for req in view.list_pending()] == ["r-new"]
    assert view.is_resolved("r-old") is True
    assert view.get_pending("r-old") is None
    assert view.get_pending("r-new") is not None


def test_list_pending_falls_back_to_the_last_good_read_when_the_gateway_errors(tmp_path: Path) -> None:
    """A gateway restart degrades to briefly-stale pending state, not an empty panel."""
    outage = [False]
    view = GatewayPendingRequests.load(
        gateway_client=_gateway_with([_streamed_record("r-1")], outage=outage), data_dir=tmp_path
    )
    assert [req.request_id for req in view.list_pending()] == ["r-1"]

    outage[0] = True
    assert [req.request_id for req in view.list_pending()] == ["r-1"]

    # A verdict recorded during the outage still filters the stale fallback.
    view.record_response(
        create_request_response_event(request_event_id="r-1", status=RequestStatus.GRANTED, agent_id="agent-abc")
    )
    assert view.list_pending() == ()


def test_load_seeds_the_verdict_index_from_the_response_log(tmp_path: Path) -> None:
    """Verdicts recorded by earlier runs count from the first read after a restart."""
    append_response_event(
        tmp_path,
        create_request_response_event(request_event_id="r-done", status=RequestStatus.GRANTED, agent_id="agent-abc"),
    )
    view = GatewayPendingRequests.load(
        gateway_client=_gateway_with([_streamed_record("r-done"), _streamed_record("r-live")]),
        data_dir=tmp_path,
    )

    assert [req.request_id for req in view.list_pending()] == ["r-live"]
    assert view.is_resolved("r-done") is True
    assert [event.request_event_id for event in view.responses()] == ["r-done"]
