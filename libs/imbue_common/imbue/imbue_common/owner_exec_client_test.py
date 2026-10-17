import base64
import json
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import NoEncryption
from cryptography.hazmat.primitives.serialization import PrivateFormat
from pydantic import SecretStr

from imbue.imbue_common.owner_exec_client import GrantsConflictError
from imbue.imbue_common.owner_exec_client import OwnerExecClient
from imbue.imbue_common.owner_exec_client import OwnerExecError
from imbue.imbue_common.owner_exec_client import OwnerExecRequestError
from imbue.imbue_common.owner_exec_client import OwnerExecResponseVerificationError
from imbue.imbue_common.owner_exec_client import _SignedRequest
from imbue.imbue_common.owner_exec_client import load_ed25519_private_key
from imbue.imbue_common.owner_exec_client import load_ed25519_public_key
from imbue.imbue_common.owner_exec_client import public_key_line_for
from imbue.imbue_common.owner_exec_client import sign_request
from imbue.imbue_common.owner_exec_client import sign_response_headers
from imbue.imbue_common.owner_exec_client import sign_stream_trailer_event
from imbue.imbue_common.owner_exec_client import ssh_fingerprint
from imbue.imbue_common.owner_exec_client import verify_request
from imbue.imbue_common.owner_exec_client import verify_response
from imbue.imbue_common.owner_exec_client import verify_stream_trailer

_VECTORS_PATH = Path(__file__).parent / "owner_exec_vectors" / "vectors.json"


def _load_vectors() -> dict:
    return json.loads(_VECTORS_PATH.read_text())


def test_request_vectors_verify_as_expected() -> None:
    vectors = _load_vectors()
    assert vectors["requests"], "expected at least one request vector"
    for vector in vectors["requests"]:
        method = vector["method"]
        path = urlsplit(vector["url"]).path
        body = base64.b64decode(vector["body_b64"])
        headers = vector["headers"]
        expect_valid = vector["expect_valid"]
        if expect_valid:
            verify_request(
                method=method,
                path=path,
                body=body,
                request_headers=headers,
                expected_audience=vector["audience"],
                authorized_key_lines=[vector["authorized_key_line"]],
                now_unix=vector["verify_at"],
            )
        else:
            with pytest.raises(OwnerExecResponseVerificationError):
                verify_request(
                    method=method,
                    path=path,
                    body=body,
                    request_headers=headers,
                    expected_audience=vector["audience"],
                    authorized_key_lines=[vector["authorized_key_line"]],
                    now_unix=vector["verify_at"],
                )


def test_response_vectors_verify_as_expected() -> None:
    vectors = _load_vectors()
    assert vectors["responses"], "expected at least one response vector"
    for vector in vectors["responses"]:
        host_key = load_ed25519_public_key(vector["host_key_line"])
        request_signature_header = vector["request_headers"]["Signature"]
        signature_b64 = request_signature_header.split(":")[1]
        signed_request = _SignedRequest(
            headers={},
            method=vector["request_method"],
            path=urlsplit(vector["request_url"]).path,
            signature_member=":" + signature_b64 + ":",
            signature_b64=signature_b64,
        )
        body = base64.b64decode(vector["body_b64"])
        if vector["expect_valid"]:
            verify_response(
                status_code=vector["status_code"],
                response_headers=vector["headers"],
                response_body=body,
                signed_request=signed_request,
                host_public_key=host_key,
                host_key_id=vector["host_key_id"],
                now_unix=vector["verify_at"],
            )
        else:
            with pytest.raises(OwnerExecResponseVerificationError):
                verify_response(
                    status_code=vector["status_code"],
                    response_headers=vector["headers"],
                    response_body=body,
                    signed_request=signed_request,
                    host_public_key=host_key,
                    host_key_id=vector["host_key_id"],
                    now_unix=vector["verify_at"],
                )


def test_stream_vectors_verify_as_expected() -> None:
    vectors = _load_vectors()
    assert vectors["streams"], "expected at least one stream vector"
    for vector in vectors["streams"]:
        host_key = load_ed25519_public_key(vector["host_key_line"])
        stream_bytes = base64.b64decode(vector["stream_bytes_b64"])
        trailer = {
            "type": "signature",
            "created": vector["created"],
            "keyid": vector["host_key_id"],
            "tag": "imbue-owner-exec-stream",
            "signature": vector["signature"],
        }
        if vector["expect_valid"]:
            verify_stream_trailer(
                stream_bytes=stream_bytes,
                request_signature_b64=vector["request_signature"],
                trailer=trailer,
                host_public_key=host_key,
                host_key_id=vector["host_key_id"],
                now_unix=vector["verify_at"],
            )
        else:
            with pytest.raises(OwnerExecResponseVerificationError):
                verify_stream_trailer(
                    stream_bytes=stream_bytes,
                    request_signature_b64=vector["request_signature"],
                    trailer=trailer,
                    host_public_key=host_key,
                    host_key_id=vector["host_key_id"],
                    now_unix=vector["verify_at"],
                )


def test_host_key_id_matches_vector_fingerprint() -> None:
    vectors = _load_vectors()
    response_vector = vectors["responses"][0]
    host_key = load_ed25519_public_key(response_vector["host_key_line"])
    assert ssh_fingerprint(host_key) == response_vector["host_key_id"]


# --- client-method tests against a fake HTTP seam returning signed responses ---

_NOW = 1_755_300_000
_HOST_SEED = 9


def _host_keypair() -> tuple[Ed25519PrivateKey, str, str]:
    private = Ed25519PrivateKey.from_private_bytes(bytes([_HOST_SEED]) * 32)
    return private, public_key_line_for(private.public_key()), ssh_fingerprint(private.public_key())


def _client_private_key_pem() -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    return private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode("ascii")


def _client_public_line() -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    return public_key_line_for(private.public_key())


def _signed_from_headers(method: str, path: str, headers: Mapping[str, str]) -> _SignedRequest:
    signature_b64 = headers["Signature"][len("sig1=:") : -1]
    return _SignedRequest(
        headers=dict(headers),
        method=method,
        path=path,
        signature_member=":" + signature_b64 + ":",
        signature_b64=signature_b64,
    )


class _FakeExecClient(OwnerExecClient):
    """Returns host-key-signed canned responses through the client's HTTP seams."""

    response_by_path: dict[str, tuple[int, dict]] = {}
    stream_events: list[dict] = []

    def _send(
        self, method: str, path: str, body: bytes, headers: Mapping[str, str]
    ) -> tuple[int, bytes, dict[str, str]]:
        signed = _signed_from_headers(method, path, headers)
        status, payload = self.response_by_path[path]
        response_body = json.dumps(payload).encode("utf-8")
        host_private, _line, host_key_id = _host_keypair()
        response_headers = sign_response_headers(status, response_body, signed, host_private, host_key_id, _NOW)
        return status, response_body, response_headers

    def _iter_stream(self, path: str, body: bytes, headers: Mapping[str, str], read_timeout: float) -> Iterator[str]:
        signed = _signed_from_headers("POST", path, headers)
        lines = [json.dumps(event) for event in self.stream_events]
        stream_text = "".join(line + "\n" for line in lines)
        host_private, _line, host_key_id = _host_keypair()
        trailer = sign_stream_trailer_event(
            stream_text.encode("utf-8"), signed.signature_b64, host_private, host_key_id, _NOW
        )
        for line in lines:
            yield line
        yield json.dumps(trailer)


def _make_client(
    response_by_path: dict[str, tuple[int, dict]] | None = None,
    stream_events: list[dict] | None = None,
) -> _FakeExecClient:
    _host_private, host_line, _kid = _host_keypair()
    return _FakeExecClient(
        base_url="https://vm-exec.example.com",
        audience="vm:host-abcd",
        private_key_text=SecretStr(_client_private_key_pem()),
        public_key_line=_client_public_line(),
        host_public_key_line=host_line,
        fixed_now_unix=_NOW,
        response_by_path=response_by_path or {},
        stream_events=stream_events or [],
    )


def test_client_read_file_returns_content() -> None:
    client = _make_client({"/read-file": (200, {"exists": True, "content_b64": base64.b64encode(b"hello").decode()})})
    exists, content = client.read_file("data/x")
    assert exists is True
    assert content == b"hello"


def test_client_read_file_missing_returns_false() -> None:
    client = _make_client({"/read-file": (404, {"exists": False})})
    exists, content = client.read_file("data/missing")
    assert exists is False
    assert content == b""


def test_client_write_file_succeeds() -> None:
    client = _make_client({"/write-file": (200, {"written": True})})
    client.write_file("data/x", b"payload", mode="600")


def test_client_write_file_raises_on_error_status() -> None:
    client = _make_client({"/write-file": (400, {"error": "bad path"})})
    with pytest.raises(OwnerExecRequestError):
        client.write_file("data/x", b"payload")


def test_client_get_grants_returns_document() -> None:
    client = _make_client({"/grants": (200, {"grants_toml": "[grants]\nowner = true\n", "revision": "r1"})})
    document = client.get_grants()
    assert document.grants_toml == "[grants]\nowner = true\n"
    assert document.revision == "r1"


def test_client_put_grants_returns_revision() -> None:
    client = _make_client({"/grants": (200, {"written": True, "revision": "r2"})})
    assert client.put_grants("[grants]\nowner = false\n", base_revision="r1") == "r2"


def test_client_put_grants_conflict_raises_with_current_document() -> None:
    client = _make_client(
        {"/grants": (409, {"error": "stale", "revision": "r9", "grants_toml": "[grants]\nowner = true\n"})}
    )
    with pytest.raises(GrantsConflictError) as excinfo:
        client.put_grants("[grants]\nowner = false\n", base_revision="stale")
    assert excinfo.value.revision == "r9"
    assert "owner = true" in excinfo.value.grants_toml


def test_client_run_collects_streams_and_verifies_trailer() -> None:
    events = [
        {"type": "stdout", "data": "out\n"},
        {"type": "stderr", "data": "err\n"},
        {"type": "exit", "code": 3},
    ]
    client = _make_client(stream_events=events)
    result = client.run(["sh", "-c", "echo out"])
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.exit_code == 3
    assert result.timed_out is False


# --- key-loading and transport-error branches ---


def test_load_private_key_rejects_non_ssh_text() -> None:
    with pytest.raises(OwnerExecError):
        load_ed25519_private_key("not a private key")


def test_load_public_key_rejects_garbage() -> None:
    with pytest.raises(OwnerExecError):
        load_ed25519_public_key("ssh-ed25519 not-base64")


def _unreachable_client() -> OwnerExecClient:
    # Port 1 on loopback is not listening, so the httpx call fails fast.
    _host_private, host_line, _kid = _host_keypair()
    return OwnerExecClient(
        base_url="http://127.0.0.1:1",
        audience="vm:host-abcd",
        private_key_text=SecretStr(_client_private_key_pem()),
        public_key_line=_client_public_line(),
        host_public_key_line=host_line,
        fixed_now_unix=_NOW,
    )


def test_real_client_is_alive_false_when_unreachable() -> None:
    assert _unreachable_client().is_alive() is False


def test_real_client_read_file_raises_when_unreachable() -> None:
    with pytest.raises(OwnerExecRequestError):
        _unreachable_client().read_file("data/x")


def test_real_client_run_raises_when_unreachable() -> None:
    with pytest.raises(OwnerExecRequestError):
        _unreachable_client().run(["true"], timeout_seconds=1.0)


# --- verify-function guard-clause branches ---


def _valid_request_vector() -> dict:
    return next(v for v in _load_vectors()["requests"] if v["expect_valid"])


def test_verify_request_missing_public_key_header() -> None:
    vector = _valid_request_vector()
    headers = dict(vector["headers"])
    del headers["X-Exec-Public-Key"]
    with pytest.raises(OwnerExecResponseVerificationError, match="public-key"):
        verify_request(
            method=vector["method"],
            path=urlsplit(vector["url"]).path,
            body=base64.b64decode(vector["body_b64"]),
            request_headers=headers,
            expected_audience=vector["audience"],
            authorized_key_lines=[vector["authorized_key_line"]],
            now_unix=vector["verify_at"],
        )


def test_verify_request_missing_signature_headers() -> None:
    vector = _valid_request_vector()
    headers = dict(vector["headers"])
    del headers["Signature-Input"]
    with pytest.raises(OwnerExecResponseVerificationError, match="signature/digest"):
        verify_request(
            method=vector["method"],
            path=urlsplit(vector["url"]).path,
            body=base64.b64decode(vector["body_b64"]),
            request_headers=headers,
            expected_audience=vector["audience"],
            authorized_key_lines=[vector["authorized_key_line"]],
            now_unix=vector["verify_at"],
        )


def _signed_response(status: int, body: bytes) -> tuple[dict[str, str], _SignedRequest]:
    _host_private, host_line, host_key_id = _host_keypair()

    private = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    signed = sign_request("POST", "/read-file", b"{}", "vm:host-abcd", private, _client_public_line(), _NOW)
    headers = sign_response_headers(status, body, signed, _host_private, host_key_id, _NOW)
    return headers, signed


def test_verify_response_missing_headers() -> None:
    _headers, signed = _signed_response(200, b'{"ok": true}')
    host_key = load_ed25519_public_key(_host_keypair()[1])
    with pytest.raises(OwnerExecResponseVerificationError, match="missing"):
        verify_response(200, {}, b'{"ok": true}', signed, host_key, _host_keypair()[2], _NOW)


def test_verify_response_wrong_keyid() -> None:
    body = b'{"ok": true}'
    headers, signed = _signed_response(200, body)
    host_key = load_ed25519_public_key(_host_keypair()[1])
    with pytest.raises(OwnerExecResponseVerificationError, match="keyid"):
        verify_response(200, headers, body, signed, host_key, "SHA256:not-the-pinned-key", _NOW)


def test_verify_stream_trailer_wrong_tag_and_bad_base64() -> None:
    host_key = load_ed25519_public_key(_host_keypair()[1])
    host_key_id = _host_keypair()[2]
    with pytest.raises(OwnerExecResponseVerificationError, match="tag"):
        verify_stream_trailer(
            b"stream",
            "AAAA",
            {"type": "signature", "created": _NOW, "keyid": host_key_id, "tag": "wrong", "signature": "AAAA"},
            host_key,
            host_key_id,
            _NOW,
        )
    with pytest.raises(OwnerExecResponseVerificationError):
        verify_stream_trailer(
            b"stream",
            "AAAA",
            {
                "type": "signature",
                "created": _NOW,
                "keyid": host_key_id,
                "tag": "imbue-owner-exec-stream",
                "signature": "!not-base64!",
            },
            host_key,
            host_key_id,
            _NOW,
        )
