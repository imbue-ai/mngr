"""Client for the owner-exec service: SSH-equivalent authority over a workspace.

Signs each request with an Ed25519 key (whose public half is in the target's
``authorized_keys``) per the strict RFC 9421 / RFC 9530 profile, and verifies
every response (and the ``/run`` stream trailer) against the endpoint's pinned
SSH host key. The profile is defined in the owner-exec repo's
``spec/profile.md`` and cross-checked by ``vectors/vectors.json``.

This module implements the profile's fixed signature bases directly rather than
through a general RFC 9421 library: the covered-component lists are fixed, and
the response ``;req`` binding and the ``/run`` stream trailer are custom
constructions the general libraries do not cover.
"""

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat
from cryptography.hazmat.primitives.serialization import load_ssh_private_key
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel

# --- profile constants (mirror internal/profile in the owner-exec repo) ---

SIGNATURE_LABEL = "sig1"
REQUEST_TAG = "imbue-owner-exec"
RESPONSE_TAG = "imbue-owner-exec-resp"
STREAM_TAG = "imbue-owner-exec-stream"
CREATED_WINDOW_SECONDS = 60
RESPONSE_CREATED_WINDOW_SECONDS = 300
_NONCE_BYTES = 18

# Hard timeout for the non-streaming endpoints (small JSON request/response
# bodies), and the threshold past which a *successful* call is logged as
# suspiciously slow (the two-threshold timeout pattern).
_REQUEST_TIMEOUT_SECONDS = 60.0
_SLOW_REQUEST_WARNING_SECONDS = 15.0
# The server's default /run command timeout, and the extra slack the stream
# read timeout allows beyond the command timeout (a silent command produces no
# stream traffic until the server kills it at its timeout).
_DEFAULT_RUN_TIMEOUT_SECONDS = 600.0
_RUN_TIMEOUT_MARGIN_SECONDS = 60.0
# The /_alive liveness probe is a quick unauthenticated call.
_ALIVE_TIMEOUT_SECONDS = 10.0

AUDIENCE_HEADER = "X-Exec-Audience"
PUBLIC_KEY_HEADER = "X-Exec-Public-Key"
CONTENT_DIGEST_HEADER = "Content-Digest"

_REQUEST_COMPONENTS = ("@method", "@path", "content-digest", "x-exec-audience", "x-exec-public-key")


class OwnerExecError(Exception):
    """Base error for the owner-exec client."""

    ...


class OwnerExecResponseVerificationError(OwnerExecError):
    """Raised when a response (or stream trailer) signature does not verify."""

    ...


class OwnerExecRequestError(OwnerExecError):
    """Raised when an owner-exec request fails at the HTTP level."""

    ...


class GrantsConflictError(OwnerExecError):
    """Raised when a grants PUT loses the compare-and-swap race.

    Carries the current document so the caller can merge and retry.
    """

    def __init__(self, grants_toml: str, revision: str) -> None:
        self.grants_toml = grants_toml
        self.revision = revision
        super().__init__("grants document changed since base_revision was read")


class RunResult(FrozenModel):
    """The collected result of an owner-exec /run call."""

    stdout: str = Field(description="Concatenated stdout stream text")
    stderr: str = Field(description="Concatenated stderr stream text")
    exit_code: int | None = Field(description="Process exit code, or None if it timed out")
    timed_out: bool = Field(description="Whether the command hit its timeout")


class GrantsDocument(FrozenModel):
    """A sharing grants document plus its compare-and-swap revision token."""

    grants_toml: str = Field(description="The grants document TOML text")
    revision: str = Field(description="Opaque CAS token (sha256 of the bytes; '' when absent)")


# --- structured-field helpers (the small subset the profile needs) ---


def _sf_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def content_digest(body: bytes) -> str:
    """The sha-256 Content-Digest header value (RFC 9530) for ``body``."""
    digest = hashlib.sha256(body).digest()
    return "sha-256=:" + base64.b64encode(digest).decode("ascii") + ":"


def _validate_content_digest(header_value: str, body: bytes) -> bool:
    expected = content_digest(body)
    return secrets.compare_digest(header_value.strip(), expected)


# --- key helpers ---


def load_ed25519_private_key(private_key_text: str) -> Ed25519PrivateKey:
    """Load an OpenSSH Ed25519 private key from text. Raises OwnerExecError otherwise."""
    try:
        key = load_ssh_private_key(private_key_text.encode("utf-8"), password=None)
    except (ValueError, TypeError) as e:
        raise OwnerExecError(f"could not parse owner-exec signing key: {e}") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise OwnerExecError("owner-exec signing key is not Ed25519")
    return key


def load_ed25519_public_key(public_key_line: str) -> Ed25519PublicKey:
    """Load an OpenSSH Ed25519 public key line. Raises OwnerExecError otherwise."""
    try:
        key = load_ssh_public_key(public_key_line.strip().encode("utf-8"))
    except (ValueError, TypeError) as e:
        raise OwnerExecError(f"could not parse owner-exec public key: {e}") from e
    if not isinstance(key, Ed25519PublicKey):
        raise OwnerExecError("owner-exec public key is not Ed25519")
    return key


def ssh_fingerprint(public_key: Ed25519PublicKey) -> str:
    """The standard OpenSSH SHA256 fingerprint used as the profile keyid."""
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    # OpenSSH fingerprints hash the full SSH wire encoding, not the raw key.
    wire = _ssh_ed25519_wire(raw)
    digest = hashlib.sha256(wire).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _ssh_ed25519_wire(raw_public: bytes) -> bytes:
    key_type = b"ssh-ed25519"
    return _ssh_string(key_type) + _ssh_string(raw_public)


def _ssh_string(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + data


def public_key_line_for(public_key: Ed25519PublicKey) -> str:
    """Render an Ed25519 public key as an OpenSSH authorized_keys line."""
    return public_key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")


# --- signature bases ---


def _request_signature_base(
    method: str,
    path: str,
    content_digest_value: str,
    audience: str,
    public_key_line: str,
    created: int,
    expires: int,
    nonce: str,
    key_id: str,
) -> tuple[str, bytes]:
    """Build the request signature-input value and the base bytes to sign."""
    params = (
        f"({' '.join(_sf_string(component) for component in _REQUEST_COMPONENTS)})"
        f";created={created};expires={expires};nonce={_sf_string(nonce)}"
        f";tag={_sf_string(REQUEST_TAG)};keyid={_sf_string(key_id)}"
    )
    lines = [
        f'"@method": {method.upper()}',
        f'"@path": {path}',
        f'"content-digest": {content_digest_value}',
        f'"x-exec-audience": {audience}',
        f'"x-exec-public-key": {public_key_line.strip()}',
        f'"@signature-params": {params}',
    ]
    signature_input = f"{SIGNATURE_LABEL}={params}"
    return signature_input, "\n".join(lines).encode("utf-8")


def _response_params(created: int, key_id: str) -> str:
    # Component and parameter ordering mirror the RFC 9421 signer exactly: the
    # signature dict component renders key before req, and the sig params are
    # created, tag, keyid.
    return (
        '("@status" "content-digest" "@method";req "@path";req "signature";key="sig1";req)'
        f";created={created};tag={_sf_string(RESPONSE_TAG)};keyid={_sf_string(key_id)}"
    )


def _response_signature_base(
    status_code: int,
    content_digest_value: str,
    request_method: str,
    request_path: str,
    request_signature_member: str,
    created: int,
    key_id: str,
) -> bytes:
    params = _response_params(created, key_id)
    lines = [
        f'"@status": {status_code}',
        f'"content-digest": {content_digest_value}',
        f'"@method";req: {request_method.upper()}',
        f'"@path";req: {request_path}',
        f'"signature";key="sig1";req: {request_signature_member}',
        f'"@signature-params": {params}',
    ]
    return "\n".join(lines).encode("utf-8")


def _stream_trailer_base(stream_sha256: bytes, request_signature_b64: str, key_id: str, created: int) -> bytes:
    lines = [
        f'"stream-digest": sha-256=:{base64.b64encode(stream_sha256).decode("ascii")}:',
        f'"request-signature": :{request_signature_b64}:',
        (
            '"@signature-params": ("stream-digest" "request-signature")'
            f";created={created};keyid={_sf_string(key_id)};tag={_sf_string(STREAM_TAG)}"
        ),
    ]
    return "\n".join(lines).encode("utf-8")


# --- signature-input / signature header parsing (strict) ---


def _parse_signature_dict_member(header_value: str) -> bytes:
    """Extract the raw sig1 signature bytes from a Signature header value."""
    # Signature: sig1=:<base64>:
    prefix = f"{SIGNATURE_LABEL}=:"
    stripped = header_value.strip()
    if not stripped.startswith(prefix) or not stripped.endswith(":"):
        raise OwnerExecError("Signature header is not a single sig1 byte-sequence member")
    encoded = stripped[len(prefix) : -1]
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as e:
        raise OwnerExecError("Signature header member is not valid base64") from e


class _SignedRequest(FrozenModel):
    """A request's headers plus the values needed to verify its response."""

    headers: dict[str, str] = Field(description="Signed request headers to send")
    method: str = Field(description="HTTP method")
    path: str = Field(description="URL path")
    signature_member: str = Field(description="The request's sig1 signature, sf-serialized")
    signature_b64: str = Field(description="The request's sig1 signature, base64")


def sign_request(
    method: str,
    path: str,
    body: bytes,
    audience: str,
    private_key: Ed25519PrivateKey,
    public_key_line: str,
    now_unix: int,
    nonce: str | None = None,
) -> _SignedRequest:
    """Produce the signed headers for one owner-exec request."""
    public_key = private_key.public_key()
    key_id = ssh_fingerprint(public_key)
    digest_value = content_digest(body)
    created = now_unix
    expires = created + CREATED_WINDOW_SECONDS
    chosen_nonce = nonce if nonce is not None else base64.b64encode(secrets.token_bytes(_NONCE_BYTES)).decode("ascii")

    signature_input, base = _request_signature_base(
        method, path, digest_value, audience, public_key_line, created, expires, chosen_nonce, key_id
    )
    signature = private_key.sign(base)
    signature_b64 = base64.b64encode(signature).decode("ascii")
    signature_header = f"{SIGNATURE_LABEL}=:{signature_b64}:"
    headers = {
        CONTENT_DIGEST_HEADER: digest_value,
        AUDIENCE_HEADER: audience,
        PUBLIC_KEY_HEADER: public_key_line.strip(),
        "Signature-Input": signature_input,
        "Signature": signature_header,
        "Content-Type": "application/json",
    }
    return _SignedRequest(
        headers=headers,
        method=method,
        path=path,
        signature_member=":" + signature_b64 + ":",
        signature_b64=signature_b64,
    )


def verify_request(
    method: str,
    path: str,
    body: bytes,
    request_headers: Mapping[str, str],
    expected_audience: str,
    authorized_key_lines: Sequence[str],
    now_unix: int,
) -> None:
    """Verify a signed request against the strict profile. Raises on failure.

    This mirrors the Go server's verifier (minus the nonce replay cache, which
    is stateful and a server concern). It is used to consume the request test
    vectors and could back a Python-side server in the future.
    """
    audience = _header(request_headers, AUDIENCE_HEADER)
    if not expected_audience or audience != expected_audience:
        raise OwnerExecResponseVerificationError("request audience does not match")
    public_key_line = _header(request_headers, PUBLIC_KEY_HEADER)
    if not public_key_line:
        raise OwnerExecResponseVerificationError("request is missing the public-key header")
    public_key = load_ed25519_public_key(public_key_line)
    authorized_raw = {
        load_ed25519_public_key(line).public_bytes(Encoding.Raw, PublicFormat.Raw)
        for line in authorized_key_lines
        if line.strip()
    }
    presented_raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    if presented_raw not in authorized_raw:
        raise OwnerExecResponseVerificationError("presented key is not authorized")

    signature_input = _header(request_headers, "Signature-Input")
    signature_header = _header(request_headers, "Signature")
    digest_header = _header(request_headers, CONTENT_DIGEST_HEADER)
    if not signature_input or not signature_header or not digest_header:
        raise OwnerExecResponseVerificationError("request is missing signature/digest headers")
    if not _tag_matches(signature_input, REQUEST_TAG):
        raise OwnerExecResponseVerificationError("request signature tag is wrong")
    key_id = ssh_fingerprint(public_key)
    if not _keyid_matches(signature_input, key_id):
        raise OwnerExecResponseVerificationError("request signature keyid does not match the presented key")

    created = _parse_created(signature_input)
    expires = _parse_param_int(signature_input, "expires")
    if abs(now_unix - created) > CREATED_WINDOW_SECONDS:
        raise OwnerExecResponseVerificationError("request created timestamp is outside the window")
    if now_unix >= expires:
        raise OwnerExecResponseVerificationError("request signature has expired")
    if not _validate_content_digest(digest_header, body):
        raise OwnerExecResponseVerificationError("request Content-Digest does not match the body")

    nonce = _parse_param_string(signature_input, "nonce")
    rebuilt_input, base = _request_signature_base(
        method, path, digest_header, audience, public_key_line, created, expires, nonce, key_id
    )
    if rebuilt_input != signature_input:
        raise OwnerExecResponseVerificationError("request signature-input does not match the strict profile")
    signature = _parse_signature_dict_member(signature_header)
    try:
        public_key.verify(signature, base)
    except InvalidSignature as e:
        raise OwnerExecResponseVerificationError("request signature does not verify") from e


def verify_response(
    status_code: int,
    response_headers: Mapping[str, str],
    response_body: bytes,
    signed_request: _SignedRequest,
    host_public_key: Ed25519PublicKey,
    host_key_id: str,
    now_unix: int,
) -> None:
    """Verify a signed response against the pinned host key. Raises on failure."""
    signature_input = _header(response_headers, "Signature-Input")
    signature_header = _header(response_headers, "Signature")
    digest_header = _header(response_headers, CONTENT_DIGEST_HEADER)
    if not signature_input or not signature_header or not digest_header:
        raise OwnerExecResponseVerificationError("response is missing signature/digest headers")

    created = _parse_created(signature_input)
    if not _tag_matches(signature_input, RESPONSE_TAG):
        raise OwnerExecResponseVerificationError("response signature tag is wrong")
    if not _keyid_matches(signature_input, host_key_id):
        raise OwnerExecResponseVerificationError("response signature keyid does not match the pinned host key")
    if abs(now_unix - created) > RESPONSE_CREATED_WINDOW_SECONDS:
        raise OwnerExecResponseVerificationError("response signature created timestamp is outside the window")
    if not _validate_content_digest(digest_header, response_body):
        raise OwnerExecResponseVerificationError("response Content-Digest does not match the body")

    base = _response_signature_base(
        status_code,
        digest_header,
        signed_request.method,
        signed_request.path,
        signed_request.signature_member,
        created,
        host_key_id,
    )
    signature = _parse_signature_dict_member(signature_header)
    try:
        host_public_key.verify(signature, base)
    except InvalidSignature as e:
        raise OwnerExecResponseVerificationError("response signature does not verify") from e


def verify_stream_trailer(
    stream_bytes: bytes,
    request_signature_b64: str,
    trailer: Mapping[str, object],
    host_public_key: Ed25519PublicKey,
    host_key_id: str,
    now_unix: int,
) -> None:
    """Verify a /run stream trailer against the pinned host key. Raises on failure."""
    created = trailer.get("created")
    signature_b64 = trailer.get("signature")
    if not isinstance(created, int) or not isinstance(signature_b64, str):
        raise OwnerExecResponseVerificationError("stream trailer is missing created/signature")
    if trailer.get("tag") != STREAM_TAG:
        raise OwnerExecResponseVerificationError("stream trailer tag is wrong")
    if trailer.get("keyid") != host_key_id:
        raise OwnerExecResponseVerificationError("stream trailer keyid does not match the pinned host key")
    if abs(now_unix - created) > RESPONSE_CREATED_WINDOW_SECONDS:
        raise OwnerExecResponseVerificationError("stream trailer created timestamp is outside the window")
    stream_digest = hashlib.sha256(stream_bytes).digest()
    base = _stream_trailer_base(stream_digest, request_signature_b64, host_key_id, created)
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError) as e:
        raise OwnerExecResponseVerificationError("stream trailer signature is not valid base64") from e
    try:
        host_public_key.verify(signature, base)
    except InvalidSignature as e:
        raise OwnerExecResponseVerificationError("stream trailer signature does not verify") from e


def sign_response_headers(
    status_code: int,
    response_body: bytes,
    signed_request: _SignedRequest,
    host_private_key: Ed25519PrivateKey,
    host_key_id: str,
    created: int,
) -> dict[str, str]:
    """Produce the signed response headers for a request. Symmetric with verify_response.

    Signs with the endpoint's SSH host key, bound to the request. Used by a
    Python-side owner-exec server and by the client's own tests.
    """
    digest = content_digest(response_body)
    base = _response_signature_base(
        status_code,
        digest,
        signed_request.method,
        signed_request.path,
        signed_request.signature_member,
        created,
        host_key_id,
    )
    signature = host_private_key.sign(base)
    return {
        CONTENT_DIGEST_HEADER: digest,
        "Signature-Input": f"{SIGNATURE_LABEL}={_response_params(created, host_key_id)}",
        "Signature": f"{SIGNATURE_LABEL}=:{base64.b64encode(signature).decode('ascii')}:",
    }


def sign_stream_trailer_event(
    stream_bytes: bytes,
    request_signature_b64: str,
    host_private_key: Ed25519PrivateKey,
    host_key_id: str,
    created: int,
) -> dict[str, object]:
    """Produce the signed ``/run`` trailer event. Symmetric with verify_stream_trailer."""
    stream_digest = hashlib.sha256(stream_bytes).digest()
    base = _stream_trailer_base(stream_digest, request_signature_b64, host_key_id, created)
    signature = host_private_key.sign(base)
    return {
        "type": "signature",
        "created": created,
        "keyid": host_key_id,
        "tag": STREAM_TAG,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _parse_created(signature_input: str) -> int:
    for token in signature_input.split(";"):
        stripped = token.strip()
        if stripped.startswith("created="):
            return int(stripped[len("created=") :])
    raise OwnerExecResponseVerificationError("signature-input is missing created")


def _parse_param_int(signature_input: str, name: str) -> int:
    for token in signature_input.split(";"):
        stripped = token.strip()
        if stripped.startswith(f"{name}="):
            return int(stripped[len(name) + 1 :])
    raise OwnerExecResponseVerificationError(f"signature-input is missing {name}")


def _parse_param_string(signature_input: str, name: str) -> str:
    marker = f'{name}="'
    start = signature_input.find(marker)
    if start < 0:
        raise OwnerExecResponseVerificationError(f"signature-input is missing {name}")
    start += len(marker)
    end = signature_input.find('"', start)
    return signature_input[start:end]


def _tag_matches(signature_input: str, tag: str) -> bool:
    return f'tag="{tag}"' in signature_input


def _keyid_matches(signature_input: str, key_id: str) -> bool:
    return f'keyid="{key_id}"' in signature_input


# --- the client ---


class OwnerExecClient(FrozenModel):
    """A signed client for one owner-exec endpoint (inner or vm)."""

    base_url: str = Field(description="The endpoint origin, e.g. https://vm-exec-ab12.<domain>")
    audience: str = Field(description="The endpoint audience, e.g. vm:host-<hex>")
    private_key_text: SecretStr = Field(description="OpenSSH Ed25519 private key text used to sign")
    public_key_line: str = Field(description="The matching OpenSSH public key line")
    host_public_key_line: str = Field(description="The endpoint's pinned SSH host public key line")
    fixed_now_unix: int | None = Field(
        default=None,
        description="When set, the clock used for signing/verification (tests); otherwise the wall clock",
    )

    def _now(self) -> int:
        if self.fixed_now_unix is not None:
            return self.fixed_now_unix
        return int(time.time())

    def _signing_key(self) -> Ed25519PrivateKey:
        return load_ed25519_private_key(self.private_key_text.get_secret_value())

    def _host_key(self) -> tuple[Ed25519PublicKey, str]:
        key = load_ed25519_public_key(self.host_public_key_line)
        return key, ssh_fingerprint(key)

    def _post_json(
        self, path: str, payload: Mapping[str, object]
    ) -> tuple[int, bytes, dict[str, str], _SignedRequest]:
        body = json.dumps(payload).encode("utf-8")
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: bytes) -> tuple[int, bytes, dict[str, str], _SignedRequest]:
        now = self._now()
        signed = sign_request(method, path, body, self.audience, self._signing_key(), self.public_key_line, now)
        started_at = time.monotonic()
        status, content, headers = self._send(method, path, body, signed.headers)
        elapsed = time.monotonic() - started_at
        if elapsed > _SLOW_REQUEST_WARNING_SECONDS:
            logger.warning("owner-exec {} {} succeeded but took {:.1f}s", method, path, elapsed)
        return status, content, headers, signed

    # The single HTTP round-trip seam (overridden in tests). Raises
    # OwnerExecRequestError on any transport failure.
    def _send(
        self, method: str, path: str, body: bytes, headers: Mapping[str, str]
    ) -> tuple[int, bytes, dict[str, str]]:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                content=body if method != "GET" else None,
                headers=dict(headers),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise OwnerExecRequestError(f"owner-exec {method} {path} failed: {e}") from e
        return response.status_code, response.content, dict(response.headers)

    def _verify(self, status_code: int, headers: Mapping[str, str], body: bytes, signed: _SignedRequest) -> None:
        host_key, host_key_id = self._host_key()
        verify_response(status_code, headers, body, signed, host_key, host_key_id, self._now())

    def read_file(self, path: str) -> tuple[bool, bytes]:
        """Read a file; returns (exists, content)."""
        status, body, headers, signed = self._post_json("/read-file", {"path": path})
        self._verify(status, headers, body, signed)
        if status == 404:
            return False, b""
        if status != 200:
            raise OwnerExecRequestError(f"read-file failed: HTTP {status}")
        parsed = json.loads(body)
        return bool(parsed.get("exists")), base64.b64decode(parsed.get("content_b64", ""))

    def write_file(self, path: str, content: bytes, mode: str | None = None) -> None:
        """Write a file atomically."""
        payload: dict[str, object] = {"path": path, "content_b64": base64.b64encode(content).decode("ascii")}
        if mode is not None:
            payload["mode"] = mode
        status, body, headers, signed = self._post_json("/write-file", payload)
        self._verify(status, headers, body, signed)
        if status != 200:
            raise OwnerExecRequestError(f"write-file failed: HTTP {status}")

    def run(self, command: Sequence[str], cwd: str | None = None, timeout_seconds: float | None = None) -> RunResult:
        """Run a command; verifies the stream trailer against the host key."""
        payload: dict[str, object] = {"command": list(command)}
        if cwd is not None:
            payload["cwd"] = cwd
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        body = json.dumps(payload).encode("utf-8")
        now = self._now()
        signed = sign_request("POST", "/run", body, self.audience, self._signing_key(), self.public_key_line, now)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        timed_out = False
        stream_accumulator = bytearray()
        trailer: dict[str, object] | None = None
        # A silent command can legitimately produce no stream traffic until the
        # server kills it at its timeout, so bound reads by that plus a margin.
        read_timeout = (
            timeout_seconds if timeout_seconds is not None else _DEFAULT_RUN_TIMEOUT_SECONDS
        ) + _RUN_TIMEOUT_MARGIN_SECONDS
        for line in self._iter_stream("/run", body, signed.headers, read_timeout):
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "signature":
                trailer = event
                continue
            # The trailer signs the exact stream bytes (each event line +
            # newline); accumulate them as the server did.
            stream_accumulator.extend((line + "\n").encode("utf-8"))
            match event.get("type"):
                case "stdout":
                    stdout_parts.append(str(event.get("data", "")))
                case "stderr":
                    stderr_parts.append(str(event.get("data", "")))
                case "exit":
                    code = event.get("code")
                    exit_code = code if isinstance(code, int) else None
                    timed_out = bool(event.get("timed_out"))
                case unknown_kind:
                    # Tolerated for forward compatibility; the bytes are still
                    # covered by the signed trailer hash above.
                    logger.debug("Ignoring unknown owner-exec stream event type: {}", unknown_kind)

        if trailer is None:
            raise OwnerExecResponseVerificationError("run stream ended without a signed trailer")
        host_key, host_key_id = self._host_key()
        verify_stream_trailer(
            bytes(stream_accumulator), signed.signature_b64, trailer, host_key, host_key_id, self._now()
        )
        return RunResult(
            stdout="".join(stdout_parts), stderr="".join(stderr_parts), exit_code=exit_code, timed_out=timed_out
        )

    def get_grants(self) -> GrantsDocument:
        """Read the sharing grants document (inner role only)."""
        status, body, headers, signed = self._request("GET", "/grants", b"")
        self._verify(status, headers, body, signed)
        if status != 200:
            raise OwnerExecRequestError(f"get-grants failed: HTTP {status}")
        parsed = json.loads(body)
        return GrantsDocument(grants_toml=parsed.get("grants_toml", ""), revision=parsed.get("revision", ""))

    def put_grants(self, grants_toml: str, base_revision: str | None = None) -> str:
        """Replace the grants document; returns the new revision. Raises GrantsConflictError on a stale CAS."""
        payload: dict[str, object] = {"grants_toml": grants_toml}
        if base_revision is not None:
            payload["base_revision"] = base_revision
        status, body, headers, signed = self._request("PUT", "/grants", json.dumps(payload).encode("utf-8"))
        self._verify(status, headers, body, signed)
        if status == 409:
            parsed = json.loads(body)
            raise GrantsConflictError(parsed.get("grants_toml", ""), parsed.get("revision", ""))
        if status != 200:
            raise OwnerExecRequestError(f"put-grants failed: HTTP {status}")
        parsed = json.loads(body)
        return parsed.get("revision", "")

    # The streaming HTTP seam for /run (overridden in tests): yields NDJSON
    # lines, raising OwnerExecRequestError on a transport failure or non-200.
    def _iter_stream(self, path: str, body: bytes, headers: Mapping[str, str], read_timeout: float) -> Iterator[str]:
        stream_timeout = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, read=read_timeout)
        try:
            with httpx.stream(
                "POST", f"{self.base_url}{path}", content=body, headers=dict(headers), timeout=stream_timeout
            ) as response:
                if response.status_code != 200:
                    raise OwnerExecRequestError(f"run failed: HTTP {response.status_code}")
                yield from response.iter_lines()
        except httpx.HTTPError as e:
            raise OwnerExecRequestError(f"run stream failed: {e}") from e

    def is_alive(self) -> bool:
        """Probe the unauthenticated /_alive endpoint."""
        return self._alive_status() == 200

    # The /_alive probe seam (overridden in tests): the status code, or None on
    # a transport failure.
    def _alive_status(self) -> int | None:
        try:
            response = httpx.get(f"{self.base_url}/_alive", timeout=_ALIVE_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            return None
        return response.status_code
