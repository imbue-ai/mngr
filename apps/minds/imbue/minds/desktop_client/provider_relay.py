"""Relay a provider sign-in's loopback callback from this machine into a workspace.

A workspace signs in to Claude or ChatGPT by running the provider's own CLI, which listens on a
loopback port inside the workspace and names that port in the authorize URL's ``redirect_uri``.
The browser that finishes the sign-in runs on the user's machine, not in the workspace, so its
redirect to ``http://localhost:<port>/callback`` would land nowhere. The relay listens on that
same port here and hands the callback to the workspace's chat app, which replays it against the
CLI's listener and answers with how the sign-in ended.

The browser is answered with a page of the relay's own, never with anything the workspace sent:
the CLI's own answer can claim a success its token exchange then fails (claude redirects to its
success page before it knows), and the workspace is not trusted to put content on a localhost page.

Only a URL that names a known provider host and a loopback callback is relayed, and only the
callback carrying the ``state`` the URL was issued with is passed on, once.
"""

import errno
import html
import re
import socket
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Mapping
from enum import StrEnum
from enum import auto
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import Final
from typing import assert_never
from urllib.parse import SplitResult
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import make_workspace_probe_client
from imbue.minds.errors import MindError
from imbue.mngr.primitives import AgentId

PROVIDER_SIGN_IN_HOSTS: Final[frozenset[str]] = frozenset(
    {"claude.com", "claude.ai", "platform.claude.com", "auth.openai.com"}
)
_LOOPBACK_CALLBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1"})
# claude listens on a port its workspace's network stack assigns from its ephemeral range: 16000 and
# up under gVisor (the runsc runtime remote workspaces run in), 32768 and up under a Linux kernel.
# codex listens on 1455, or 1457 when 1455 is taken. Nothing else is ever bound here.
_CALLBACK_PORTS_BY_PATH: Final[Mapping[str, range | frozenset[int]]] = {
    "/callback": range(16000, 65536),
    "/auth/callback": frozenset({1455, 1457}),
}
# A browser reads a backslash as a slash and drops whitespace and control characters, so a URL
# carrying any of them can name one host to Python's parser and another to the browser.
_AMBIGUOUS_URL_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[\\\s\x00-\x1f\x7f]")
# How long the relay waits for the browser to come back before giving the port up.
NO_CALLBACK_TIMEOUT_SECONDS: Final[float] = 15 * 60
# How long after the callback the relay keeps answering a reload of its page, before it gives the
# port up.
AFTER_CALLBACK_SECONDS: Final[float] = 60
# Longer than the chat app takes to deliver the callback and wait for its flow to settle.
_WORKSPACE_FORWARD_TIMEOUT_SECONDS: Final[float] = 60.0
# The chat app's service name; its origin label is looked up per workspace, and an
# unknown label falls back to the name, which the forward plugin also routes on.
CHAT_SERVICE_NAME: Final[str] = "chat"
_MAX_DETAIL_CHARS: Final[int] = 300
_PAGE_CONTENT_SECURITY_POLICY: Final[str] = "default-src 'none'; style-src 'unsafe-inline'"


class InvalidSignInUrlError(MindError, ValueError):
    """Raised when a URL handed to the relay is not a provider sign-in with a loopback callback."""

    ...


class SignInCallback(FrozenModel):
    """Where a validated sign-in URL sends the browser back to, and the state that proves it."""

    port: int = Field(ge=1024, le=65535, description="Loopback port the CLI listens on")
    path: str = Field(description="Callback path the CLI answers on, `/callback` or `/auth/callback`")
    state: str = Field(min_length=1, description="The `state` the URL was issued with; the callback must echo it")


@pure
def _single_query_value(query: Mapping[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    if len(values) != 1 or not values[0]:
        raise InvalidSignInUrlError(f"The sign-in URL needs exactly one non-empty {name}")
    return values[0]


@pure
def _split_unambiguous(url: str, what: str) -> SplitResult:
    """Split ``url``, refusing anything a browser could read as a different host than Python does."""
    if _AMBIGUOUS_URL_CHARACTERS.search(url) is not None:
        raise InvalidSignInUrlError(f"The {what} contains characters a browser reads differently")
    parts = urlsplit(url)
    # A userinfo or an uppercase host makes the netloc differ from the host; neither is ever
    # in a URL the provider CLIs build.
    try:
        port = parts.port
    except ValueError:
        raise InvalidSignInUrlError(f"The {what} has an invalid port") from None
    expected_netloc = parts.hostname if port is None else f"{parts.hostname}:{port}"
    if parts.netloc != expected_netloc:
        raise InvalidSignInUrlError(f"The {what}'s host is not a plain host name")
    return parts


@pure
def parse_provider_sign_in_url(url: str) -> SignInCallback:
    """Validate a provider authorize URL and return the loopback callback it names.

    Raises InvalidSignInUrlError unless the URL is https on a known provider host with no port or
    userinfo, and its ``redirect_uri`` is ``http://localhost`` or ``http://127.0.0.1`` on a port
    the provider CLIs use for a known callback path, and it carries a ``state``.
    """
    parts = _split_unambiguous(url, "sign-in URL")
    if parts.scheme != "https" or parts.port is not None:
        raise InvalidSignInUrlError("The sign-in URL must be https on the default port")
    if parts.hostname not in PROVIDER_SIGN_IN_HOSTS:
        raise InvalidSignInUrlError(f"The sign-in URL's host {parts.hostname!r} is not a known provider")
    query = parse_qs(parts.query, keep_blank_values=True)
    state = _single_query_value(query, "state")
    redirect = _split_unambiguous(_single_query_value(query, "redirect_uri"), "sign-in URL's redirect_uri")
    if redirect.scheme != "http" or redirect.hostname not in _LOOPBACK_CALLBACK_HOSTS:
        raise InvalidSignInUrlError("The sign-in URL's redirect_uri must be http on localhost or 127.0.0.1")
    allowed_ports = _CALLBACK_PORTS_BY_PATH.get(redirect.path)
    if allowed_ports is None:
        raise InvalidSignInUrlError(f"The sign-in URL's redirect_uri path {redirect.path!r} is not a callback path")
    if redirect.port is None or redirect.port not in allowed_ports:
        raise InvalidSignInUrlError(f"The sign-in URL's redirect_uri port {redirect.port} is not one a sign-in uses")
    return SignInCallback(port=redirect.port, path=redirect.path, state=state)


class SignInForwardError(MindError, ConnectionError):
    """Raised when the callback could not be handed to the workspace, or its answer could not be read."""

    ...


class SignInCallbackRefusedError(MindError):
    """Raised when the workspace answered that its sign-in flow no longer takes the callback."""

    ...


class SignInOutcome(UpperCaseStrEnum):
    """How the workspace's sign-in flow stood once it had taken the callback."""

    SIGNED_IN = auto()
    FAILED = auto()
    # Still finishing when the workspace stopped waiting; the app shows how it ends.
    FINISHING = auto()


class SignInCallbackResult(FrozenModel):
    """What the workspace said about the sign-in the callback belonged to."""

    outcome: SignInOutcome = Field(description="How the sign-in stood once the CLI had the callback")
    provider_name: str = Field(description="The provider, as the workspace names it (e.g. 'Anthropic')")
    detail: str | None = Field(default=None, description="Why it failed, in the workspace's words")


class SignInCallbackForwarderInterface(MutableModel, ABC):
    """Hands the callback the relay accepted to the workspace's sign-in flow."""

    @abstractmethod
    def forward(self, path_and_query: str) -> SignInCallbackResult:
        """Replay the callback against the flow's CLI.

        Raises SignInForwardError when the workspace could not be asked, and SignInCallbackRefusedError
        when its flow no longer takes the callback.
        """


class RelayRequestVerdict(UpperCaseStrEnum):
    """What the relay does with one request that reached its port."""

    REJECT_METHOD = auto()
    NOT_FOUND = auto()
    STALE_STATE = auto()
    ALREADY_HANDLED = auto()
    FORWARD_CALLBACK = auto()


@pure
def decide_relay_request(
    method: str, path_and_query: str, callback: SignInCallback, is_callback_taken: bool
) -> RelayRequestVerdict:
    """Only a GET to the callback path carrying the URL's state is passed on, and only once."""
    if method != "GET":
        return RelayRequestVerdict.REJECT_METHOD
    parts = urlsplit(path_and_query)
    if parts.path != callback.path:
        return RelayRequestVerdict.NOT_FOUND
    if is_callback_taken:
        return RelayRequestVerdict.ALREADY_HANDLED
    states = parse_qs(parts.query, keep_blank_values=True).get("state", [])
    if states != [callback.state]:
        return RelayRequestVerdict.STALE_STATE
    return RelayRequestVerdict.FORWARD_CALLBACK


class RelayPage(FrozenModel):
    """A page the relay answers the browser with."""

    status: int = Field(description="HTTP status")
    title: str = Field(description="The page's heading")
    message: str = Field(description="What the user should know, and do next")


@pure
def page_for_result(result: SignInCallbackResult) -> RelayPage:
    provider = result.provider_name
    match result.outcome:
        case SignInOutcome.SIGNED_IN:
            return RelayPage(
                status=200,
                title=f"You're signed in to {provider}",
                message="You can close this tab and go back to Imbue Studio.",
            )
        case SignInOutcome.FAILED:
            detail = (result.detail or "").strip()[:_MAX_DETAIL_CHARS]
            reason = f"{detail} " if detail else ""
            return RelayPage(
                status=200,
                title=f"{provider} sign-in didn't finish",
                message=f"{reason}Go back to Imbue Studio to try again, or to sign in another way.",
            )
        case SignInOutcome.FINISHING:
            return RelayPage(
                status=200,
                title="Almost done",
                message=f"{provider} is finishing the sign-in. Go back to Imbue Studio to see when it's ready.",
            )
        case _ as unreachable:
            assert_never(unreachable)


_UNREACHABLE_WORKSPACE_PAGE: Final[RelayPage] = RelayPage(
    status=502,
    title="Couldn't reach your machine",
    message="Imbue Studio couldn't pass this sign-in to your machine. Make sure the machine is running, then reload this page.",
)
_STALE_STATE_PAGE: Final[RelayPage] = RelayPage(
    status=400,
    title="This sign-in page is out of date",
    message="It belongs to an earlier attempt. Go back to Imbue Studio and start the sign-in again.",
)
_ALREADY_HANDLED_PAGE: Final[RelayPage] = RelayPage(
    status=200,
    title="This sign-in has already gone through",
    message="Go back to Imbue Studio to see how it went.",
)
_NO_LONGER_WAITING_PAGE: Final[RelayPage] = RelayPage(
    status=409,
    title="This sign-in is no longer waiting",
    message="It ended or was replaced before the browser came back. Go back to Imbue Studio to see where it "
    "stands, or start the sign-in again.",
)
_NOT_FOUND_PAGE: Final[RelayPage] = RelayPage(status=404, title="Not found", message="Go back to Imbue Studio.")
_METHOD_PAGE: Final[RelayPage] = RelayPage(status=405, title="Not allowed", message="Go back to Imbue Studio.")


@pure
def render_relay_page(page: RelayPage) -> bytes:
    title = html.escape(page.title)
    message = html.escape(page.message)
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{title}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1.5rem;"
        "color:#1a1a1a;background:#fff}h1{font-size:1.4rem;margin:0 0 .5rem}p{margin:0;color:#555}"
        "@media (prefers-color-scheme:dark){body{color:#eee;background:#111}p{color:#aaa}}</style>"
        f"<h1>{title}</h1><p>{message}</p></html>"
    ).encode("utf-8")


def _is_port_answering(port: int) -> bool:
    """Whether a program already accepts connections on ``port`` on either loopback.

    Asked before binding because the relay binds with SO_REUSEADDR (so a port its own previous
    relay just closed can be bound again at once), and on macOS that also lets a bind to
    127.0.0.1 succeed over another program's wildcard listener and take its loopback traffic.
    """
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        if family == socket.AF_INET6 and not socket.has_ipv6:
            continue
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((address, port)) == 0:
                return True
    return False


class _RelayHTTPServer(ThreadingHTTPServer):
    """The listener on 127.0.0.1."""

    daemon_threads = True


class _RelayHTTPServerV6(_RelayHTTPServer):
    """The listener on ::1."""

    address_family = socket.AF_INET6


class _RelayRequestHandler(BaseHTTPRequestHandler):
    """Answers one request on the relay's port with the page the relay picks for it.

    Each relay binds its own subclass (see ``_handler_class_for``), so the relay is a class
    attribute rather than something read back off the server.
    """

    relay: "ProviderSignInRelay"

    def log_message(self, format: str, *args: Any) -> None:
        # The default access log prints the request line, whose query carries the
        # authorization code; the relay logs what it did without it.
        return

    def _handle(self) -> None:
        page = self.relay.answer(self.command, self.path)
        body = render_relay_page(page)
        self.send_response(page.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _PAGE_CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()


def _handler_class_for(relay: "ProviderSignInRelay") -> type[_RelayRequestHandler]:
    return type("_BoundRelayRequestHandler", (_RelayRequestHandler,), {"relay": relay})


class ProviderSignInRelay(MutableModel):
    """One armed relay: the listener on a sign-in's callback port, and when it gives the port up."""

    workspace_id: AgentId = Field(frozen=True, description="The workspace whose sign-in this relays")
    flow_id: str = Field(frozen=True, description="The chat app's sign-in flow the callback belongs to")
    callback: SignInCallback = Field(frozen=True, description="The callback this relay waits for")
    forwarder: SignInCallbackForwarderInterface = Field(frozen=True, description="Where the callback goes")
    on_callback_handled: Callable[[], None] = Field(
        frozen=True, description="Called once the workspace answered the callback (raises the app window)"
    )
    no_callback_timeout_seconds: float = Field(frozen=True, description="Give up this long after arming")
    after_callback_seconds: float = Field(
        frozen=True, description="Keep answering reloads this long after the callback"
    )

    _servers: list[_RelayHTTPServer] = PrivateAttr(default_factory=list)
    # Guards the fields below; notified whenever the deadline moves or the relay is told to stop.
    _condition: threading.Condition = PrivateAttr(default_factory=threading.Condition)
    # Set while the callback is out to the workspace and once it has been answered, so a second
    # copy of it (a double-submitted redirect, a reload) is never passed on.
    _is_callback_taken: bool = PrivateAttr(default=False)
    # Set once the workspace has answered the callback; from then on the relay only answers reloads.
    _is_callback_answered: bool = PrivateAttr(default=False)
    _deadline: float = PrivateAttr(default=0.0)
    _is_stop_requested: bool = PrivateAttr(default=False)
    # Set once every listener has closed, so the port is free again.
    _closed: threading.Event = PrivateAttr(default_factory=threading.Event)

    def start(self) -> bool:
        """Bind the callback port and start relaying; False when another program holds it.

        The redirect names ``localhost``, which a browser may resolve to either loopback, so the
        port has to be free on both: a relay holding only one could hand the code to whatever
        holds the other. A machine with no IPv6 loopback at all has nothing on ``::1`` to lose it to.
        """
        if _is_port_answering(self.callback.port):
            logger.debug("Port {} already answers on loopback; not relaying a sign-in there", self.callback.port)
            return False
        handler_class = _handler_class_for(self)
        try:
            self._servers.append(_RelayHTTPServer(("127.0.0.1", self.callback.port), handler_class))
            if socket.has_ipv6:
                try:
                    self._servers.append(_RelayHTTPServerV6(("::1", self.callback.port), handler_class))
                except OSError as e:
                    if e.errno == errno.EADDRINUSE:
                        raise
                    logger.debug("Relaying the sign-in on 127.0.0.1 only; this machine has no ::1: {}", e)
        except OSError as e:
            logger.debug("Could not bind port {} on loopback for a sign-in relay: {}", self.callback.port, e)
            for server in self._servers:
                server.server_close()
            self._servers.clear()
            return False
        self._deadline = time.monotonic() + self.no_callback_timeout_seconds
        for server in self._servers:
            threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name=f"sign-in-relay-{self.callback.port}",
                daemon=True,
            ).start()
        threading.Thread(
            target=self._stop_at_deadline, name=f"sign-in-relay-deadline-{self.callback.port}", daemon=True
        ).start()
        return True

    def stop(self) -> None:
        """Give the port up, returning once it is free. Idempotent."""
        with self._condition:
            if self._is_stop_requested:
                is_closing_elsewhere = True
            else:
                is_closing_elsewhere = False
                self._is_stop_requested = True
                self._condition.notify_all()
        if is_closing_elsewhere:
            self._closed.wait()
            return
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._closed.set()

    def is_stopped(self) -> bool:
        return self._closed.is_set()

    def has_taken_callback(self) -> bool:
        with self._condition:
            return self._is_callback_taken

    def has_answered_callback(self) -> bool:
        with self._condition:
            return self._is_callback_answered

    def wait_until_stopped(self, timeout_seconds: float) -> bool:
        """Block until the port is free again, or the timeout passes; whether it is free."""
        return self._closed.wait(timeout=timeout_seconds)

    def answer(self, method: str, path_and_query: str) -> RelayPage:
        """The page the browser gets for one request on the callback port."""
        with self._condition:
            verdict = decide_relay_request(method, path_and_query, self.callback, self._is_callback_taken)
            if verdict is RelayRequestVerdict.FORWARD_CALLBACK:
                self._is_callback_taken = True
        match verdict:
            case RelayRequestVerdict.REJECT_METHOD:
                return _METHOD_PAGE
            case RelayRequestVerdict.NOT_FOUND:
                return _NOT_FOUND_PAGE
            case RelayRequestVerdict.STALE_STATE:
                logger.debug("Refused a sign-in callback on port {} carrying another state", self.callback.port)
                return _STALE_STATE_PAGE
            case RelayRequestVerdict.ALREADY_HANDLED:
                return _ALREADY_HANDLED_PAGE
            case RelayRequestVerdict.FORWARD_CALLBACK:
                return self._forward(path_and_query)
            case _ as unreachable:
                assert_never(unreachable)

    def _forward(self, path_and_query: str) -> RelayPage:
        try:
            result = self.forwarder.forward(path_and_query)
        except SignInForwardError as e:
            logger.warning("Could not relay a sign-in callback into workspace {}: {}", self.workspace_id, e)
            # The CLI never saw the code, so a reload can still deliver it.
            with self._condition:
                self._is_callback_taken = False
            return _UNREACHABLE_WORKSPACE_PAGE
        except SignInCallbackRefusedError as e:
            logger.info("Workspace {} no longer takes its sign-in callback: {}", self.workspace_id, e)
            self._keep_answering_reloads_briefly()
            return _NO_LONGER_WAITING_PAGE
        logger.info("Relayed a sign-in callback into workspace {}: {}", self.workspace_id, result.outcome)
        self._keep_answering_reloads_briefly()
        self.on_callback_handled()
        return page_for_result(result)

    def _keep_answering_reloads_briefly(self) -> None:
        with self._condition:
            self._is_callback_answered = True
            self._deadline = time.monotonic() + self.after_callback_seconds
            self._condition.notify_all()

    def _stop_at_deadline(self) -> None:
        with self._condition:
            while not self._is_stop_requested:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            is_past_deadline = not self._is_stop_requested
        if is_past_deadline:
            logger.debug("Giving up the sign-in relay on port {}", self.callback.port)
            self.stop()


class _WorkspaceFlowState(StrEnum):
    """A chat app sign-in flow's state, as its routes spell it."""

    OK = "ok"
    FAILED = "failed"
    PENDING = "pending"


class _WorkspaceCallbackAnswer(FrozenModel):
    """The chat app's answer to the relayed callback: its sign-in flow's status once the CLI had it."""

    # Answered by a workspace deployed on its own schedule, which also sends fields the relay does not read.
    model_config = ConfigDict(extra="ignore")

    state: _WorkspaceFlowState = Field(description="The flow's state")
    detail: str | None = Field(default=None, description="Why it failed, or a note about it")
    provider_name: str = Field(description="The provider the flow signs in to")


_OUTCOME_BY_FLOW_STATE: Final[Mapping[_WorkspaceFlowState, SignInOutcome]] = {
    _WorkspaceFlowState.OK: SignInOutcome.SIGNED_IN,
    _WorkspaceFlowState.FAILED: SignInOutcome.FAILED,
    _WorkspaceFlowState.PENDING: SignInOutcome.FINISHING,
}


class WorkspaceChatSignInForwarder(SignInCallbackForwarderInterface):
    """Hands the callback to a workspace chat app's sign-in flow, through the local forward plugin.

    The plugin accepts the preauth cookie as the owner and stamps the request's identity itself,
    so the chat app's owner check holds for the relay and for nothing a page could forge.
    """

    mngr_forward_port: int = Field(frozen=True, description="Port the local mngr forward plugin listens on")
    preauth_cookie: str = Field(frozen=True, description="The plugin's preauth session cookie")
    workspace_id: AgentId = Field(frozen=True, description="The workspace whose chat app runs the flow")
    chat_service_label: str = Field(frozen=True, description="The chat app's origin label in that workspace")
    flow_id: str = Field(frozen=True, description="The chat app's sign-in flow the callback belongs to")

    def forward(self, path_and_query: str) -> SignInCallbackResult:
        url = f"https://127.0.0.1:{self.mngr_forward_port}/api/accounts/flow/{self.flow_id}/callback"
        host = f"{self.chat_service_label}.{self.workspace_id}.localhost"
        with make_workspace_probe_client(
            preauth_cookie=self.preauth_cookie, probe_timeout_seconds=_WORKSPACE_FORWARD_TIMEOUT_SECONDS
        ) as client:
            try:
                response = client.post(url, headers={"Host": host}, json={"path_and_query": path_and_query})
            except httpx.HTTPError as e:
                raise SignInForwardError(f"the workspace could not be reached ({type(e).__name__})") from e
        if response.status_code == 409:
            raise SignInCallbackRefusedError(f"the workspace answered 409: {response.text[:_MAX_DETAIL_CHARS]}")
        if response.status_code != 200:
            raise SignInForwardError(f"the workspace answered {response.status_code}")
        try:
            answer = _WorkspaceCallbackAnswer.model_validate_json(response.content)
        except ValidationError as e:
            raise SignInForwardError("the workspace's answer could not be read") from e
        return SignInCallbackResult(
            outcome=_OUTCOME_BY_FLOW_STATE[answer.state], provider_name=answer.provider_name, detail=answer.detail
        )


class RelayArmResult(UpperCaseStrEnum):
    """What arming a relay came to."""

    # A new relay is listening; the page should be opened.
    ARMED = auto()
    # This workspace's relay for the same sign-in is already listening; the page can be reopened.
    ALREADY_ARMED = auto()
    # The port is held by another workspace's sign-in or another program; the workspace falls back.
    PORT_UNAVAILABLE = auto()


# A workspace can have a sign-in page opened, or opened again, no more often than this.
_MIN_SECONDS_BETWEEN_OPENS: Final[float] = 1.0


class ProviderRelayRegistry(MutableModel):
    """The relays armed on this machine: at most one per workspace, and one per port.

    A workspace's new sign-in replaces its own earlier relay, but never another workspace's that
    is still waiting: a port another workspace is waiting on stays with it, and the newcomer falls
    back to a manual sign-in instead. Once that relay's callback has been answered it only answers
    reloads of its page, so a new sign-in may take its port. That also bounds what a workspace can
    hold to one loopback port.
    """

    _relay_by_workspace: dict[AgentId, ProviderSignInRelay] = PrivateAttr(default_factory=dict)
    _last_opened_at_by_workspace: dict[AgentId, float] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def arm(self, relay: ProviderSignInRelay) -> RelayArmResult:
        with self._lock:
            self._forget_stopped_locked()
            current = self._relay_by_workspace.get(relay.workspace_id)
            if current is not None and current.flow_id == relay.flow_id and current.callback == relay.callback:
                return RelayArmResult.ALREADY_ARMED
            others_on_port = {
                workspace_id: other
                for workspace_id, other in self._relay_by_workspace.items()
                if workspace_id != relay.workspace_id and other.callback.port == relay.callback.port
            }
            if any(not other.has_answered_callback() for other in others_on_port.values()):
                return RelayArmResult.PORT_UNAVAILABLE
            for workspace_id, finished in others_on_port.items():
                finished.stop()
                del self._relay_by_workspace[workspace_id]
            if current is not None:
                current.stop()
                del self._relay_by_workspace[relay.workspace_id]
            if not relay.start():
                return RelayArmResult.PORT_UNAVAILABLE
            self._relay_by_workspace[relay.workspace_id] = relay
            return RelayArmResult.ARMED

    def claim_browser_open(self, workspace_id: AgentId) -> bool:
        """Whether a sign-in page may be opened for this workspace now; a claim starts its cool-down."""
        now = time.monotonic()
        with self._lock:
            last = self._last_opened_at_by_workspace.get(workspace_id)
            if last is not None and now - last < _MIN_SECONDS_BETWEEN_OPENS:
                return False
            self._last_opened_at_by_workspace[workspace_id] = now
            return True

    def disarm(self, workspace_id: AgentId, flow_id: str) -> None:
        """Stop the workspace's relay if it is still the one for ``flow_id``.

        A relay that has taken its callback is left to its own short deadline, so a reload of the
        page it answered with still gets that page.
        """
        with self._lock:
            current = self._relay_by_workspace.get(workspace_id)
            if current is None or current.flow_id != flow_id or current.has_taken_callback():
                return
            del self._relay_by_workspace[workspace_id]
        current.stop()

    def stop_all(self) -> None:
        with self._lock:
            relays = list(self._relay_by_workspace.values())
            self._relay_by_workspace.clear()
        for relay in relays:
            relay.stop()

    def _forget_stopped_locked(self) -> None:
        for workspace_id in [key for key, relay in self._relay_by_workspace.items() if relay.is_stopped()]:
            del self._relay_by_workspace[workspace_id]
