"""Ephemeral in-memory TLS for ``mngr forward`` (self-signed cert + hypercorn config).

Used only when ``--use-http2`` is set: the proxy terminates TLS and negotiates
HTTP/2 (via ALPN), which multiplexes many streams over a single connection.
The certificate is self-signed, regenerated every startup, and covers only
loopback names (``localhost``, ``*.localhost``, ``127.0.0.1``), so it is
trusted only by clients that opt in -- no OS trust store or CA install is
involved.

Service-per-origin hostnames (``<name>.agent-<hex>.localhost`` and deeper)
cannot be covered by a static SAN list: ``*.localhost`` matches exactly one
label, agent IDs are discovered at runtime, and services register while the
proxy runs. Instead of regenerating the server cert, the server context
carries an SNI callback that mints (and caches) a per-hostname certificate on
first handshake, signed with the same ephemeral key. Any nested origin depth
is covered, forever, with no restart.
"""

import ipaddress
import os
import ssl
import tempfile
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from hypercorn.config import Config
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_forward.errors import ForwardTLSError

# HTTP/2 first, HTTP/1.1 fallback. WebSocket upgrades negotiate http/1.1 on
# their own connection via this same list; h2 carries the plain HTTP requests
# that were the constrained resource.
ALPN_PROTOCOLS: Final[list[str]] = ["h2", "http/1.1"]

_CERT_VALIDITY_DAYS: Final[int] = 3650
_RSA_KEY_SIZE: Final[int] = 2048

# Upper bound on distinct SNI hostnames we mint certs for. Prevents a
# misbehaving local client from growing the cache without bound; real use
# is a handful of service origins per workspace.
_MAX_MINTED_CONTEXTS: Final[int] = 1024


def _build_certificate(key: rsa.RSAPrivateKey, dns_names: Sequence[str]) -> bytes:
    """Return PEM for a self-signed cert over ``dns_names`` (+ loopback IP)."""
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    now = datetime.now(timezone.utc)
    subject_alt_name = x509.SubjectAlternativeName(
        [x509.DNSName(name) for name in dns_names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=_CERT_VALIDITY_DAYS))
        .add_extension(subject_alt_name, critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def generate_self_signed_cert() -> tuple[bytes, bytes]:
    """Return ``(cert_pem, key_pem)`` for a fresh self-signed loopback cert.

    The static SANs cover ``localhost``, ``*.localhost`` (the bare
    ``agent-<id>.localhost`` workspace origins -- the wildcard does not match
    the bare label, so both are required), and ``127.0.0.1``. Nested service
    origins are covered dynamically per SNI name by
    ``build_server_ssl_context``, not by this cert.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)
    cert_pem = _build_certificate(key, ["localhost", "*.localhost"])
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _load_context_from_pem(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    """Build a TLS-server context (ALPN h2/http1.1, TLS >= 1.2) from in-memory PEM.

    Python's ``SSLContext.load_cert_chain`` only accepts filesystem paths, so
    the PEM is written to a private temp file (``mkstemp`` creates it 0600),
    loaded, and unlinked in the same call -- it is never persisted.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(ALPN_PROTOCOLS)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        os.write(fd, cert_pem + b"\n" + key_pem)
        os.close(fd)
        context.load_cert_chain(certfile=path)
    finally:
        os.unlink(path)
    return context


def _is_covered_by_static_sans(server_name: str) -> bool:
    """True when the static cert (``localhost`` / ``*.localhost``) matches."""
    if server_name == "localhost":
        return True
    return server_name.endswith(".localhost") and server_name.count(".") == 1


class _SNICertMinter(MutableModel):
    """SNI callback that mints (and caches) per-hostname certs on demand.

    Names the static SANs cover pass through untouched; any deeper
    ``.localhost`` name gets a certificate for that exact hostname, signed
    with the same ephemeral key, swapped in via ``ssl_socket.context``.
    """

    key_pem: bytes = Field(description="PEM of the ephemeral private key that signs minted certs")

    _key: rsa.RSAPrivateKey | None = PrivateAttr(default=None)
    _minted_contexts: dict[str, ssl.SSLContext] = PrivateAttr(default_factory=dict)
    _mint_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _get_key(self) -> rsa.RSAPrivateKey:
        if self._key is None:
            key = serialization.load_pem_private_key(self.key_pem, password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ForwardTLSError(f"Expected an RSA private key, got {type(key).__name__}")
            self._key = key
        return self._key

    def _mint_context_for(self, server_name: str) -> ssl.SSLContext:
        minted_cert_pem = _build_certificate(self._get_key(), [server_name])
        return _load_context_from_pem(minted_cert_pem, self.key_pem)

    def __call__(self, ssl_socket: ssl.SSLObject, server_name: str | None, _: ssl.SSLContext) -> None:
        if server_name is None:
            return
        name = server_name.lower()
        if _is_covered_by_static_sans(name) or not name.endswith(".localhost"):
            return
        with self._mint_lock:
            minted = self._minted_contexts.get(name)
            if minted is None:
                if len(self._minted_contexts) >= _MAX_MINTED_CONTEXTS:
                    return
                minted = self._mint_context_for(name)
                self._minted_contexts[name] = minted
        ssl_socket.context = minted


def build_server_ssl_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    """Build the serving ``SSLContext``: static loopback cert + per-SNI minting.

    Handshakes for names the static SANs cover (``localhost``, one-label
    ``*.localhost``) use the static cert unchanged. Deeper names -- service
    origins like ``svc.agent-<hex>.localhost`` and their subtrees -- get a
    certificate minted for that exact hostname on first handshake (cached
    thereafter), signed with the same ephemeral key. This is what lets
    services registered after startup be served over TLS without a restart.
    """
    context = _load_context_from_pem(cert_pem, key_pem)
    context.set_servername_callback(_SNICertMinter(key_pem=key_pem))
    return context


class InMemoryTLSConfig(Config):
    """Hypercorn ``Config`` that serves TLS from an in-memory ``SSLContext``.

    Hypercorn's stock ``Config`` gates TLS on ``certfile``/``keyfile`` paths and
    rebuilds the context by loading those files. We hold the context directly
    and override the two hooks hypercorn consults: ``ssl_enabled`` (so
    ``create_sockets`` routes the listen socket into the secure bucket) and
    ``create_ssl_context`` (so ``worker_serve`` uses our context). This keeps
    the cert and key off disk beyond the transient load in
    ``build_server_ssl_context``.
    """

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        super().__init__()
        self._ssl_context = ssl_context

    @property
    def ssl_enabled(self) -> bool:
        return True

    def create_ssl_context(self) -> ssl.SSLContext:
        return self._ssl_context
