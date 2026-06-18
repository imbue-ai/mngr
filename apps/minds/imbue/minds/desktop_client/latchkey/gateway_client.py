"""Synchronous HTTP client for the latchkey gateway's bundled extensions.

The shared latchkey gateway (spawned by ``mngr latchkey forward``)
exposes two extensions added in version 2.9.0:

* ``permission-requests`` -- pending-permission queue. Agents POST to
  ``/permission-requests`` when they hit a blocked service; the desktop
  client consumes ``GET /permission-requests?follow=true`` to learn
  about new requests and ``DELETE /permission-requests/<id>`` to remove
  them once granted or denied.
* ``permissions`` -- per-host permissions config editor. The desktop
  client POSTs to ``/permissions/rules?path=<host_file>&rule_key=<scope>``
  with a JSON body of permission-schema names to apply a grant.

Both extensions sit behind the gateway's standard auth wall: callers
present the listen password in ``Authorization: Bearer <password>`` and
a permissions-override JWT in
``X-Latchkey-Gateway-Permissions-Override``. This client always sends
the admin JWT (minted via :meth:`Latchkey.create_admin_permissions_jwt`)
because the desktop client needs wildcard access to both extensions.

All operations are synchronous. The streaming consumer in
:mod:`permission_requests_consumer` drives the long-running
``?follow=true`` connection from a daemon thread; everything else is
called from the FastAPI request thread under
``asyncio.get_running_loop().run_in_executor``.
"""

import json
import threading
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import JsonValue
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.forward_supervisor import is_forward_info_alive
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import load_forward_info

# Header names baked into the upstream gateway's wire contract.
_HEADER_PASSWORD: Final[str] = "X-Latchkey-Gateway-Password"
_HEADER_PERMISSIONS_OVERRIDE: Final[str] = "X-Latchkey-Gateway-Permissions-Override"

# Per-line read timeout on the follow stream. Finite (not ``None``) so
# the consumer thread can exit promptly on shutdown -- a read=None would
# leave the consumer wedged inside ``response.iter_lines()`` until the
# gateway happened to push the next request, which on a clean shutdown
# is never, and the root concurrency group would then time out waiting
# for the thread to join. The trade-off is that an idle stream gets torn
# down and rebuilt every ~2 seconds, which is fine for the local
# 127.0.0.1 gateway (negligible network cost) and bounds shutdown delay
# to one read-timeout interval. The consumer's reconnect loop treats a
# ReadTimeout-driven close as "no work to do", not as an error.
_FOLLOW_READ_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=10.0, read=2.0, write=10.0, pool=10.0)

# Short timeout for one-shot POST / DELETE / GET calls.
_ONE_SHOT_TIMEOUT_SECONDS: Final[float] = 10.0

# How long minds is willing to wait for ``mngr latchkey forward`` to
# bind its gateway port and stamp the port onto its on-disk supervisor
# record. Long enough to tolerate a cold gateway-binary start on a
# slow box but short enough to keep ``minds run`` from blocking
# forever if the supervisor never becomes ready.
_GATEWAY_PORT_WAIT_SECONDS: Final[float] = 30.0
_GATEWAY_PORT_POLL_INTERVAL_SECONDS: Final[float] = 0.2


class LatchkeyGatewayClientError(Exception):
    """Raised for any HTTP / parse failure when talking to the gateway."""


class LatchkeyGatewayInitializationError(LatchkeyGatewayClientError):
    """Raised when initialization didn't succeed."""


class LatchkeyGatewayClientNotInitializedError(LatchkeyGatewayClientError):
    """Raised when the client isn't initialized yet."""


class PredefinedRequestPayload(FrozenModel):
    """Payload for ``type == "predefined"`` permission requests."""

    scope: str = Field(description="Detent scope schema name (e.g. ``slack-api``).")
    permissions: tuple[str, ...] = Field(
        default=(),
        description=(
            "Permission schemas the agent is requesting under the scope. May be empty, "
            "in which case the agent is asking for any permissions the user chooses to grant."
        ),
    )


class FileSharingAccess(UpperCaseStrEnum):
    """Access mode an agent requests for a file-sharing grant.

    ``READ`` unlocks the non-mutating WebDAV verbs only (GET, HEAD,
    OPTIONS, PROPFIND); ``WRITE`` is a strict superset that also unlocks
    the verbs that mutate the resource (PUT, DELETE, PROPPATCH, MKCOL,
    COPY, MOVE, LOCK, UNLOCK). Read-only and read-write grants for the
    same path live as distinct schemas in the user's permissions.json
    so the two can be held independently.
    """

    READ = auto()
    WRITE = auto()


class FileSharingRequestPayload(FrozenModel):
    """Payload for ``type == "file-sharing"`` permission requests."""

    path: str = Field(description="Absolute filesystem path the agent wants to share.")
    access: FileSharingAccess = Field(
        description=(
            "Access mode the agent is requesting for ``path``. ``READ`` grants the non-mutating "
            "WebDAV verbs; ``WRITE`` is a superset that also grants the mutating ones."
        ),
    )


class PermissionEffect(FrozenModel):
    """Pre-computed patch the gateway will splice into ``target`` when a request is approved.

    Mirrors the shape produced by ``computeEffect`` in
    ``permission_requests.mjs``. The desktop client carries this
    through for traceability and does not have to interpret it; the
    gateway is responsible for merging it into the target
    ``permissions.json`` on approve.
    """

    rules: tuple[Mapping[str, tuple[str, ...]], ...] = Field(
        default=(),
        description=(
            "Scope-to-permissions grants. Each element is a single-key mapping from a detent "
            "scope schema name to the permission schema names being granted under it."
        ),
    )
    schemas: Mapping[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            "Detent schema definitions referenced by ``rules``. Populated for ``file-sharing`` "
            "requests (which introduce custom per-path schemas); empty for ``predefined`` "
            "requests, which only reference detent's built-in catalog."
        ),
    )


class StreamedPermissionRequest(FrozenModel):
    """Single JSONL record produced by ``GET /permission-requests``.

    The on-disk schema is versioned (``permission_requests/v2``). The
    ``payload`` field is a tagged union over ``request_type``; pydantic
    resolves it by shape (the two variants have disjoint required
    fields, and ``FrozenModel`` forbids extras), so consumers can
    dispatch with ``isinstance`` on ``payload`` directly.
    """

    request_id: str = Field(description="Filename-safe request id generated by the extension.")
    agent_id: str = Field(description="Agent that submitted the request.")
    rationale: str = Field(description="One-paragraph human-readable rationale supplied by the agent.")
    request_type: str = Field(
        description=(
            "Wire echo of the request kind (``predefined`` or ``file-sharing``). The actual "
            "dispatch type is determined by ``payload``'s concrete variant; this field is "
            "carried through for logging / round-tripping only."
        ),
    )
    payload: PredefinedRequestPayload | FileSharingRequestPayload = Field(
        description="Type-specific payload; dispatch with ``isinstance``.",
    )
    target: str = Field(
        description="Absolute path of the permissions.json that an approval would modify.",
    )
    effect: PermissionEffect = Field(
        description=(
            "Pre-computed patch the gateway will splice into ``target`` when this request is "
            "approved. Carried here mainly for traceability; the desktop client does not have "
            "to interpret it."
        ),
    )


class LatchkeyGatewayClient(MutableModel):
    """Synchronous client for the latchkey gateway's HTTP extensions.

    The client owns a single :class:`httpx.Client` used for everything
    except the long-lived follow-stream (which gets its own short-lived
    client per connect attempt so a stream restart cannot poison the
    pool used by the request thread).
    """

    _latchkey: Latchkey | None = PrivateAttr(default=None)

    _transport: httpx.BaseTransport | None = PrivateAttr(default=None)
    _base_url: str | None = PrivateAttr(default=None)
    _password: str | None = PrivateAttr(default=None)
    _admin_jwt: str | None = PrivateAttr(default=None)

    _init_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    # ``httpx.BaseTransport`` is not pydantic-native; allow it through.
    model_config = {"arbitrary_types_allowed": True, "frozen": False, "extra": "forbid"}

    def ensure_initialized(self) -> None:
        """Block until the supervised ``mngr latchkey forward`` binds its gateway port, then derive credentials.

        Idempotent: subsequent calls return immediately once the
        credentials have been populated. Ideally avoid running this on
        the main thread; ``minds run`` kicks it off from a background
        thread so startup does not block on the supervisor.

        Requires ``self.latchkey`` to be set; tests that pre-populate
        the private credential attrs directly skip this entirely.
        """
        with self._init_lock:
            if self._base_url is not None:
                # Already initialized.
                return
            if self._latchkey is None:
                raise LatchkeyGatewayClientNotInitializedError(
                    "LatchkeyGatewayClient was constructed without a Latchkey instance; cannot initialize.",
                )
            forward_info = self._wait_for_gateway_port()
            self._base_url = f"http://{self._latchkey.listen_host}:{forward_info.gateway_port}"
            self._admin_jwt = self._latchkey.create_admin_permissions_jwt()
            self._password = self._latchkey.derive_gateway_password()

    def invalidate_initialization(self) -> None:
        """Drop cached gateway URL + auth credentials so the next call re-resolves from disk.

        The cached ``_base_url`` is built once from the supervisor's
        on-disk ``LatchkeyForwardInfo`` record. If the supervisor
        restarts mid-session -- or if minds startup raced the
        supervisor restart and cached the previous gateway's port --
        every subsequent connection attempt will fail with a
        transport-level error (typically ``[Errno 111] Connection
        refused``). Calling this method clears the cached state so the
        next :meth:`ensure_initialized` re-reads the record and
        rebinds the URL.

        Callers that rely on :meth:`from_credentials` (test fixtures
        that have no :class:`Latchkey` to re-derive from) should not
        call this method: subsequent HTTP calls would fail with
        :class:`LatchkeyGatewayClientNotInitializedError`. In
        production the client is built via :meth:`from_latchkey` and
        re-resolution is safe.
        """
        with self._init_lock:
            self._base_url = None
            self._admin_jwt = None
            self._password = None

    @classmethod
    def from_latchkey(cls, latchkey: Latchkey) -> "LatchkeyGatewayClient":
        client = cls()
        client._latchkey = latchkey
        return client

    @classmethod
    def from_credentials(
        cls, base_url: str, password: str, admin_jwt: str, transport: httpx.BaseTransport | None = None
    ) -> "LatchkeyGatewayClient":
        client = cls()
        client._base_url = base_url
        client._password = password
        client._admin_jwt = admin_jwt
        client._transport = transport
        return client

    def _require_base_url(self) -> str:
        if self._base_url is None:
            raise LatchkeyGatewayClientNotInitializedError("LatchkeyGatewayClient is not initialized yet.")
        return self._base_url

    def _wait_for_gateway_port(self) -> LatchkeyForwardInfo:
        """Block until the supervised ``mngr latchkey forward`` stamps its bound gateway port.

        The supervisor writes its ``LatchkeyForwardInfo`` record with
        ``gateway_port=None`` at spawn time and updates the record in place
        once it has bound the shared ``latchkey gateway`` subprocess to a
        free TCP port. We poll the record until the port becomes non-None
        (or the timeout expires) so subsequent minds startup steps can
        build the gateway URL deterministically without racing the
        supervisor's own startup.
        """
        if self._latchkey is None:
            raise LatchkeyGatewayInitializationError(
                "LatchkeyGatewayClient was constructed without a Latchkey instance; cannot wait for gateway port.",
            )
        plugin_dir = self._latchkey.plugin_data_dir
        deadline = threading.Event()
        timer = threading.Timer(_GATEWAY_PORT_WAIT_SECONDS, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while not deadline.is_set():
                info = load_forward_info(plugin_dir)
                if info is not None and not is_forward_info_alive(info):
                    # Supervisor died between spawn and port-bind; bail out
                    # instead of polling a stale record forever.
                    raise LatchkeyGatewayInitializationError(
                        "The ``mngr latchkey forward`` supervisor we spawned has died before binding its "
                        f"gateway port; check {plugin_dir}/latchkey_forward.log for details.",
                    )
                if info is not None and info.gateway_port is not None:
                    return info
                # Use the same event as the deadline so we wake up promptly
                # when the timer fires; the wait returns True iff the
                # deadline was reached during the sleep.
                if deadline.wait(timeout=_GATEWAY_PORT_POLL_INTERVAL_SECONDS):
                    break
        finally:
            timer.cancel()
        raise LatchkeyGatewayInitializationError(
            f"Timed out after {_GATEWAY_PORT_WAIT_SECONDS:.1f}s waiting for ``mngr latchkey forward`` to stamp "
            f"its bound gateway port onto {plugin_dir}; is the supervisor stuck?",
        )

    def _build_headers(self) -> dict[str, str]:
        if self._password is None or self._admin_jwt is None:
            raise LatchkeyGatewayClientNotInitializedError("LatchkeyGatewayClient is not initialized yet.")
        return {
            _HEADER_PASSWORD: self._password,
            _HEADER_PERMISSIONS_OVERRIDE: self._admin_jwt,
        }

    def _one_shot_client(self) -> httpx.Client:
        """Return a per-call :class:`httpx.Client` honouring the optional test transport."""
        if self._transport is not None:
            return httpx.Client(timeout=_ONE_SHOT_TIMEOUT_SECONDS, transport=self._transport)
        return httpx.Client(timeout=_ONE_SHOT_TIMEOUT_SECONDS)

    def _wrap_transport_error(self, e: httpx.HTTPError, context: str) -> LatchkeyGatewayClientError:
        """Turn an :mod:`httpx` exception into our error type, invalidating cached init on connect-level failures.

        Connect-level errors (:class:`httpx.ConnectError`,
        :class:`httpx.ConnectTimeout`) strongly suggest the cached
        gateway URL is pointing at a port that nothing is listening on
        anymore -- typically because the supervisor restarted on a new
        port mid-session or because startup raced the supervisor and
        cached the previous gateway's port. We drop the cached state
        so the *next* call to :meth:`ensure_initialized` re-reads the
        supervisor's on-disk record and picks up the current port.

        Non-connect errors (5xx, malformed responses, read errors
        mid-stream, etc.) are propagated without invalidation: those
        usually indicate a problem at the gateway end rather than a
        stale local cache, and re-resolving the URL would not help.
        """
        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
            self.invalidate_initialization()
        return LatchkeyGatewayClientError(f"{context}: {e}")

    def _stream_client(self) -> httpx.Client:
        """Return a per-stream :class:`httpx.Client` honouring the optional test transport."""
        if self._transport is not None:
            return httpx.Client(timeout=_FOLLOW_READ_TIMEOUT, transport=self._transport)
        return httpx.Client(timeout=_FOLLOW_READ_TIMEOUT)

    def iter_permission_requests(self) -> Iterator[StreamedPermissionRequest]:
        """Yield every existing + future pending permission request until the connection drops.

        Connects to ``GET /permission-requests?follow=true`` and parses
        each newline-delimited JSON line as a
        :class:`StreamedPermissionRequest`. The iterator is exhausted
        (raising :class:`LatchkeyGatewayClientError` on any HTTP error)
        when the gateway closes the connection or a network error
        terminates the stream.

        ``httpx.ReadTimeout`` is treated specially: the stream uses a
        finite per-read timeout (see ``_FOLLOW_READ_TIMEOUT``) so the
        consumer thread can unblock on shutdown, and a timeout therefore
        means "no events arrived in the polling window" -- not an
        error. We swallow it and return cleanly so the caller's
        reconnect loop can decide whether to keep going (idle) or exit
        (stop event set).
        """
        self.ensure_initialized()
        url = f"{self._require_base_url().rstrip('/')}/permission-requests"
        params = {"follow": "true"}
        try:
            with self._stream_client() as client:
                with client.stream("GET", url, params=params, headers=self._build_headers()) as response:
                    response.raise_for_status()
                    for raw_line in response.iter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError as e:
                            logger.warning("Could not parse permission-requests JSONL line {!r}: {}", line, e)
                            continue
                        try:
                            yield StreamedPermissionRequest.model_validate(data)
                        except ValueError as e:
                            logger.warning(
                                "permission-requests JSONL line had unexpected shape {!r}: {}",
                                line,
                                e,
                            )
                            continue
        except httpx.ReadTimeout:
            # Idle window -- not an error. Return so the caller's
            # reconnect loop can check its stop event and either exit or
            # reconnect promptly without backoff.
            return
        except httpx.HTTPError as e:
            raise self._wrap_transport_error(e, "GET /permission-requests stream failed") from e

    def delete_permission_request(self, request_id: str) -> None:
        """Remove the named pending request from the gateway's queue.

        404s are tolerated because the desktop-client UI may race the
        gateway (e.g. when the same request is granted and deny is
        clicked in a different tab); the intent is satisfied either
        way.
        """
        self.ensure_initialized()
        url = f"{self._require_base_url().rstrip('/')}/permission-requests/{request_id}"
        try:
            with self._one_shot_client() as client:
                response = client.delete(url, headers=self._build_headers())
        except httpx.HTTPError as e:
            raise self._wrap_transport_error(e, f"DELETE {url} failed") from e
        if response.status_code == 404:
            logger.debug("DELETE {} returned 404; request already gone", url)
            return
        if response.status_code >= 400:
            raise LatchkeyGatewayClientError(
                f"DELETE {url} returned {response.status_code}: {response.text.strip()}",
            )

    def approve_permission_request(self, request_id: str, override_path: str | None = None) -> None:
        """Approve the named pending request via the gateway's bundled extension.

        Wraps ``POST /permission-requests/approve/<request_id>``. The
        extension owns the actual work: it reads the request file,
        splices the precomputed ``effect`` into the stored ``target``
        permissions.json, and removes the pending-request file. Failure
        leaves the request pending so the user can retry.

        ``override_path`` is set only for *file-sharing* requests whose
        shared path the user edited in the approval dialog before
        approving. When provided, it is sent as a ``{"path": ...}`` JSON
        body and the gateway recomputes the file-sharing effect for that
        path (re-validating it for traversal) instead of using the
        agent-supplied one. Leaving it ``None`` sends no body, so the
        gateway applies the precomputed effect verbatim.

        Unlike :meth:`delete_permission_request`, ``404`` is *not*
        tolerated here -- approving a request that the gateway has
        forgotten about would silently drop the user's intent on the
        floor, which is much worse for a grant than for a deny.
        """
        self.ensure_initialized()
        url = f"{self._require_base_url().rstrip('/')}/permission-requests/approve/{request_id}"
        json_body = {"path": override_path} if override_path is not None else None
        try:
            with self._one_shot_client() as client:
                response = client.post(url, headers=self._build_headers(), json=json_body)
        except httpx.HTTPError as e:
            raise self._wrap_transport_error(e, f"POST {url} failed") from e
        if response.status_code >= 400:
            raise LatchkeyGatewayClientError(
                f"POST {url} returned {response.status_code}: {response.text.strip()}",
            )

    def get_granted_permissions_for_scopes(
        self,
        permissions_file_path: Path,
        scopes: Sequence[str],
    ) -> frozenset[str]:
        """Return the union of granted permission schemas across ``scopes`` in the file.

        Wraps ``GET /permissions?path=<...>`` and walks the parsed
        ``rules`` list. A missing file (404) is treated as "no rules"
        and returns the empty set, matching the previous filesystem
        read's behaviour. Any other non-2xx surfaces as
        :class:`LatchkeyGatewayClientError`.
        """
        self.ensure_initialized()
        url = f"{self._require_base_url().rstrip('/')}/permissions"
        params = {"path": str(permissions_file_path)}
        try:
            with self._one_shot_client() as client:
                response = client.get(url, params=params, headers=self._build_headers())
        except httpx.HTTPError as e:
            raise self._wrap_transport_error(e, f"GET {url} failed") from e
        if response.status_code == 404:
            return frozenset()
        if response.status_code >= 400:
            raise LatchkeyGatewayClientError(
                f"GET {url} returned {response.status_code}: {response.text.strip()}",
            )
        try:
            payload = response.json()
        except ValueError as e:
            raise LatchkeyGatewayClientError(f"GET {url} returned non-JSON body: {e}") from e
        if not isinstance(payload, dict):
            raise LatchkeyGatewayClientError(f"GET {url} returned non-object JSON: {payload!r}")
        rules = payload.get("rules")
        if not isinstance(rules, list):
            return frozenset()
        scopes_set = set(scopes)
        granted: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            for scope_name, permissions in rule.items():
                if scope_name in scopes_set and isinstance(permissions, list):
                    granted.update(p for p in permissions if isinstance(p, str))
        return frozenset(granted)

    def set_permission_rule(
        self,
        permissions_file_path: Path,
        rule_key: str,
        granted_permissions: Sequence[str],
    ) -> None:
        """Add or replace the rule for ``rule_key`` in ``permissions_file_path``.

        Wraps ``POST /permissions/rules?path=<...>&rule_key=<...>`` with
        a JSON-array body of permission-schema names. The extension
        creates the target file (and any missing parent directories,
        e.g. ``hosts/<host_id>/``) if it does not yet exist; it refuses
        any path outside ``LATCHKEY_EXTENSION_PERMISSIONS_ROOT`` with
        a 403, which we surface verbatim so misconfigurations are
        loud.
        """
        self.ensure_initialized()
        url = f"{self._require_base_url().rstrip('/')}/permissions/rules"
        params = {"path": str(permissions_file_path), "rule_key": rule_key}
        try:
            with self._one_shot_client() as client:
                response = client.post(
                    url,
                    params=params,
                    json=list(granted_permissions),
                    headers=self._build_headers(),
                )
        except httpx.HTTPError as e:
            raise self._wrap_transport_error(e, f"POST {url} failed") from e
        if response.status_code >= 400:
            raise LatchkeyGatewayClientError(
                f"POST {url} returned {response.status_code}: {response.text.strip()}",
            )
