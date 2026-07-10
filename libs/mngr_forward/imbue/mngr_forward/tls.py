"""Ephemeral in-memory TLS for ``mngr forward`` (self-signed cert + hypercorn config).

Used only when ``--use-http2`` is set: the proxy terminates TLS and negotiates
HTTP/2 (via ALPN), which multiplexes many streams over a single connection.
The certificate is self-signed, regenerated every startup, and covers only
loopback names (``localhost``, ``*.localhost``, ``127.0.0.1``), so it is
trusted only by clients that opt in -- no OS trust store or CA install is
involved.
"""

import ipaddress
import os
import ssl
import tempfile
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

# HTTP/2 first, HTTP/1.1 fallback. WebSocket upgrades negotiate http/1.1 on
# their own connection via this same list; h2 carries the plain HTTP requests
# that were the constrained resource.
ALPN_PROTOCOLS: Final[list[str]] = ["h2", "http/1.1"]

_CERT_VALIDITY_DAYS: Final[int] = 3650
_RSA_KEY_SIZE: Final[int] = 2048


def generate_self_signed_cert() -> tuple[bytes, bytes]:
    """Return ``(cert_pem, key_pem)`` for a fresh self-signed loopback cert.

    The SANs cover ``localhost``, ``*.localhost`` (the ``agent-<id>.localhost``
    workspace subdomains -- the wildcard does not match the bare label, so both
    are required), and ``127.0.0.1`` (loopback probes that dial the IP host).
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    subject_alt_name = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.DNSName("*.localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
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
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def build_server_ssl_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    """Build a server ``SSLContext`` (ALPN h2/http1.1, TLS >= 1.2) from in-memory PEM.

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
