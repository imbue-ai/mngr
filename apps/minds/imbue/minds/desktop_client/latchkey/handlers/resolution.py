"""The one resolve epilogue shared by every permission-grant handler.

Lives alongside the handlers (like :mod:`.messaging`) so all four import
a sibling rather than each other. Resolving a request means exactly
three things, in order: durably record the verdict (the response event
log plus its in-memory index -- the authority every pending/resolved
read consults), nudge the agent with an id-and-verdict-tagged message so
its transcript records the outcome and it resumes, and wake the chrome
SSE so every surface repaints. Gateway-record removal is NOT here: how
the gateway forgets a request differs per flow (approve consumes it,
deny DELETEs it), so that stays with each handler.
"""

from pathlib import Path

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.response_events import RequestResponseEvent
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import append_response_event
from imbue.minds.desktop_client.latchkey.response_events import create_request_response_event
from imbue.minds.desktop_client.state import get_state
from imbue.minds.errors import PendingRequestsUnavailableError
from imbue.mngr.primitives import AgentId


def resolve_request(
    mngr_message_sender: MngrMessageSender,
    data_dir: Path,
    request_event_id: str,
    agent_id: AgentId,
    status: RequestStatus,
    message: str,
) -> RequestResponseEvent:
    """Record ``status`` for the request and tell the agent and the chrome.

    The verdict is appended to the response event log first -- the durable
    record every read consults after a restart -- then indexed in the live
    pending-requests view so this process's reads see it at once.
    """
    response_event = create_request_response_event(
        request_event_id=request_event_id,
        status=status,
        agent_id=str(agent_id),
    )
    append_response_event(data_dir, response_event)
    pending = get_state().pending_requests
    if pending is None:
        raise PendingRequestsUnavailableError(
            "The pending-requests view is not configured; a verdict cannot be indexed."
        )
    pending.record_response(response_event)
    mngr_message_sender.send(agent_id, format_resolution_notice(message, request_event_id, status))
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.notify_change()
    return response_event
