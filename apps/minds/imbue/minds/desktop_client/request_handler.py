"""Abstract handler for a single ``RequestEvent`` subtype.

The desktop client supports multiple kinds of pending requests (sharing,
latchkey-permission, ...). Each is rendered, granted, and denied through a
type-specific ``RequestEventHandler`` so the route layer can stay a thin
dispatcher: it authenticates, looks up the request event by id, picks the
handler that claims the event's ``request_type``, and forwards the rest
of the work.

Adding a new request kind is now a matter of writing a new
``RequestEventHandler`` subclass and registering it with the desktop
client; no churn in ``app.py`` is required.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence

from flask import Request
from flask import Response

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.request_events import RequestEvent


class RequestEventHandler(MutableModel, ABC):
    """Per-``RequestType`` handler for the request inbox flow.

    Each implementation owns rendering the request detail fragment,
    applying a grant, applying a deny, and providing the human-readable
    labels the inbox list uses to describe pending requests of its
    kind. The route layer guarantees that ``req_event.request_type``
    matches ``handles_request_type()`` before calling any of the
    methods below, so subclasses may safely narrow ``req_event`` to
    their concrete type.
    """

    @abstractmethod
    def handles_request_type(self) -> str:
        """Return the ``RequestType`` string this handler claims (e.g. ``"SHARING"``)."""

    @abstractmethod
    def kind_label(self) -> str:
        """Short, lower-case label shown on inbox list cards (e.g. ``"sharing"``)."""

    @abstractmethod
    def display_name_for_event(self, req_event: RequestEvent) -> str:
        """Human-readable secondary label for the inbox list card.

        Typically the friendly service name (e.g. ``"Slack"`` rather than
        ``"slack"``). Falls back to whatever raw identifier the event
        carries when no nicer label is available.
        """

    @abstractmethod
    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        """Render the right-pane HTML fragment for an inbox detail view.

        The fragment is embedded inside the inbox modal's
        ``#inbox-detail`` container (or innerHTML-swapped into it). It
        must not include ``<html>``, a backdrop, a close button, or any
        per-handler script tags: chrome and submission JS live in the
        inbox shell and operate on shared element ids the fragment
        emits (``#permissions-form``, ``#permissions-approve-btn``,
        ``#permissions-error``, ``#permissions-progress``,
        ``#permissions-manual-credentials``).

        ``mngr_forward_origin`` is the bare-origin URL of the
        ``mngr forward`` plugin (e.g. ``"http://localhost:8421"``);
        handlers thread it into rendered templates so workspace links
        target the plugin's ``/goto/<agent>/`` route rather than minds.
        """

    @abstractmethod
    def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        """Apply a grant from ``POST /requests/{id}/grant`` and return the response.

        Implementations are responsible for parsing any form body, doing
        the underlying work (rewriting permission files, enabling
        sharing, ...), appending the corresponding response event to the
        inbox, and producing whatever response shape the originating UI
        expects (JSON for JS-driven dialogs, 303 redirects for plain
        form posts -- the route layer is agnostic).
        """

    @abstractmethod
    def apply_deny_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        """Apply a deny from ``POST /requests/{id}/deny`` and return the response.

        Same contract as :meth:`apply_grant_request`, minus the underlying
        grant work: the handler still appends the ``DENIED`` response
        event so the request stops appearing as pending.
        """


def find_handler_for_event(
    handlers: Sequence[RequestEventHandler],
    req_event: RequestEvent,
) -> RequestEventHandler | None:
    """Return the handler that claims ``req_event.request_type``, or ``None``.

    There is at most one handler per request type by construction (the
    desktop client builds the tuple from a fixed set of handlers); if
    two ever claimed the same type, the first registered one wins.
    """
    for handler in handlers:
        if handler.handles_request_type() == req_event.request_type:
            return handler
    return None
