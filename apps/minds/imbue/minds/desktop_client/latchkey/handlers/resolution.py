"""The one resolve epilogue shared by every permission-grant handler.

Lives alongside the handlers (like :mod:`.messaging`) so all four import
a sibling rather than each other. Resolving a request means exactly
four things, in order: durably record the verdict (the response event
log plus its in-memory index -- the authority every pending/resolved
read consults), ask the machine that filed the request (if any) to forget
it and withdraw it from the user's other desktops, off this path and
best-effort (see :mod:`imbue.minds.desktop_client.latchkey.machine_request_sync`),
nudge the agent with an id-and-verdict-tagged message so its transcript
records the outcome and it resumes, and wake the chrome SSE so every
surface repaints. Gateway-record removal is NOT here: how this desktop's
gateway forgets a request differs per flow (approve consumes it, deny
DELETEs it), so that stays with each handler.
"""

from pathlib import Path

from flask import has_request_context
from flask import request

from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.api_transport import agent_label_from_environ
from imbue.minds.desktop_client.api_transport import is_local_trusted_environ
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.messaging import format_resolution_notice
from imbue.minds.desktop_client.latchkey.response_events import RequestResponseEvent
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import UNLABELED_LOCAL_AGENT_DECIDER
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
        decided_by=_deciding_agent_label(),
    )
    append_response_event(data_dir, response_event)
    pending = get_state().pending_requests
    if pending is None:
        raise PendingRequestsUnavailableError(
            "The pending-requests view is not configured; a verdict cannot be indexed."
        )
    pending.record_response(response_event)
    machine_request_sync = get_state().machine_request_sync
    if machine_request_sync is not None:
        machine_request_sync.forget_in_background(request_event_id, agent_id)
    backend_resolver: BackendResolverInterface = get_state().backend_resolver
    # The request names its chat; a seeded chat's id is not an agent's, so the nudge runs on
    # the agent the resolver knows for it (the chat's newest member) and names the chat by id.
    mngr_message_sender.send(
        agent_id,
        format_resolution_notice(message, request_event_id, status),
        exec_agent_address=build_agent_address(backend_resolver.resolve_agent_id(agent_id), backend_resolver),
    )
    if isinstance(backend_resolver, MngrCliBackendResolver):
        backend_resolver.notify_change()
    return response_event


def _deciding_agent_label() -> str | None:
    """The label of the same-machine agent giving this verdict, when one is; None for the user in the UI.

    A verdict arriving over the local socket is an agent acting for the user, so the record names it
    (its self-declared label, or a fixed marker when it sent none) and the inbox can say who decided.
    """
    if not has_request_context() or not is_local_trusted_environ(request.environ):
        return None
    return agent_label_from_environ(request.environ) or UNLABELED_LOCAL_AGENT_DECIDER
