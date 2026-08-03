"""Local-CA-backed TLS for ``mngr forward`` (persistent CA + hypercorn config).

Used only when ``--use-http2`` is set: the proxy terminates TLS and negotiates
HTTP/2 (via ALPN), which multiplexes many streams over a single connection.

The trust anchor is a persistent, machine-local certificate authority stored
under ``$MNGR_HOST_DIR/plugin/forward/ca/`` (mkcert-style). Server leaf
certificates are regenerated every startup and cover only loopback names
(``localhost``, ``*.localhost``, ``127.0.0.1``); they are signed by the local
CA so that a browser which trusts the CA once (see ``trust.py``) accepts every
later leaf without interstitials. Clients that trust programmatically (the
minds Electron shell) keep working without any CA install.

Service-per-origin hostnames (``<name>.host-<hex>.localhost`` and deeper)
cannot be covered by a static SAN list: ``*.localhost`` matches exactly one
label, host ids are discovered at runtime, and services register while the
proxy runs. Instead of regenerating the server cert, the server context
carries an SNI callback that mints (and caches) a per-hostname certificate on
first handshake, signed by the same local CA. Any nested origin depth is
covered, without a restart.
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
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID
from hypercorn.config import Config
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_forward.errors import ForwardTLSError

# HTTP/2 first, HTTP/1.1 fallback. WebSocket upgrades negotiate http/1.1 on
# their own connection via this same list; h2 carries the plain HTTP requests
# that were the constrained resource.
ALPN_PROTOCOLS: Final[list[str]] = ["h2", "http/1.1"]

# macOS refuses TLS server certs (from any CA, including user-installed
# roots) whose validity exceeds 825 days, so leaves are capped there. Leaves
# are regenerated every startup anyway. The CA itself carries a long
# validity: it is installed into trust stores once and should not churn.
_LEAF_VALIDITY_DAYS: Final[int] = 825
_CA_VALIDITY_DAYS: Final[int] = 3650
_RSA_KEY_SIZE: Final[int] = 2048

CA_CERT_FILENAME: Final[str] = "ca_cert.pem"
_CA_KEY_FILENAME: Final[str] = "ca_key.pem"

# Upper bound on distinct SNI hostnames we mint certs for. Prevents a
# misbehaving local client from growing the cache without bound; real use
# is a handful of service origins per workspace.
_MAX_MINTED_CONTEXTS: Final[int] = 1024


# X.509 caps the CommonName attribute at 64 characters. Deep service origins
# (e.g. ``uuid.openvscode.host-<32hex>.localhost``) exceed that, so certs for
# long names are SAN-only (empty subject) -- clients verify against SANs and
# ignore the CN anyway.
_MAX_COMMON_NAME_LENGTH: Final[int] = 64


class LocalCertificateAuthority(FrozenModel):
    """The persistent machine-local CA that signs the proxy's leaf certificates."""

    cert_pem: bytes = Field(description="PEM of the CA certificate (the installable trust anchor)")
    key_pem: bytes = Field(description="PEM of the CA private key (never leaves the plugin state dir)")


def _generate_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)


def _key_to_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _load_rsa_key(key_pem: bytes) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ForwardTLSError(f"Expected an RSA private key, got {type(key).__name__}")
    return key


def _build_ca_certificate(ca_key: rsa.RSAPrivateKey) -> bytes:
    """Return PEM for a self-signed CA certificate over ``ca_key``."""
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mngr forward local CA")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def load_or_create_local_ca(ca_dir: Path) -> LocalCertificateAuthority:
    """Load the persistent local CA from ``ca_dir``, creating it on first use.

    The key file is written 0600. The CA persists across restarts so a
    browser that installed it once (via ``trust.py``) keeps trusting every
    later leaf.
    """
    cert_path = ca_dir / CA_CERT_FILENAME
    key_path = ca_dir / _CA_KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        try:
            return LocalCertificateAuthority(cert_pem=cert_path.read_bytes(), key_pem=key_path.read_bytes())
        except OSError as e:
            raise ForwardTLSError(f"Could not read the local CA from {ca_dir}") from e
    try:
        ca_dir.mkdir(parents=True, exist_ok=True)
        ca_key = _generate_rsa_key()
        ca_key_pem = _key_to_pem(ca_key)
        ca_cert_pem = _build_ca_certificate(ca_key)
        key_path.touch(mode=0o600, exist_ok=True)
        key_path.write_bytes(ca_key_pem)
        cert_path.write_bytes(ca_cert_pem)
    except OSError as e:
        raise ForwardTLSError(f"Could not create the local CA under {ca_dir}") from e
    logger.debug("Created a new local CA at {}", ca_dir)
    return LocalCertificateAuthority(cert_pem=ca_cert_pem, key_pem=ca_key_pem)


def _build_leaf_certificate(
    server_public_key: rsa.RSAPublicKey,
    dns_names: Sequence[str],
    ca: LocalCertificateAuthority,
) -> bytes:
    """Return PEM for a CA-signed server leaf over ``dns_names`` (+ loopback IP)."""
    if len(dns_names[0]) <= _MAX_COMMON_NAME_LENGTH:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    else:
        subject = x509.Name([])
    ca_certificate = x509.load_pem_x509_certificate(ca.cert_pem)
    ca_key = _load_rsa_key(ca.key_pem)
    now = datetime.now(timezone.utc)
    subject_alt_name = x509.SubjectAlternativeName(
        [x509.DNSName(name) for name in dns_names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_certificate.subject)
        .public_key(server_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=_LEAF_VALIDITY_DAYS))
        .add_extension(subject_alt_name, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def generate_server_credentials(ca: LocalCertificateAuthority) -> tuple[bytes, bytes]:
    """Return ``(chain_pem, server_key_pem)`` for a fresh CA-signed loopback leaf.

    The static SANs cover ``localhost``, ``*.localhost`` (the bare
    ``host-<hex>.localhost`` workspace origins -- the wildcard does not match
    the bare label, so both are required), and ``127.0.0.1``. Nested service
    origins are covered dynamically per SNI name by
    ``build_server_ssl_context``, not by this leaf. The chain carries the CA
    certificate so clients can build the path without an AIA fetch.
    """
    server_key = _generate_rsa_key()
    leaf_pem = _build_leaf_certificate(server_key.public_key(), ["localhost", "*.localhost"], ca)
    return leaf_pem + ca.cert_pem, _key_to_pem(server_key)


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
        try:
            os.write(fd, cert_pem + b"\n" + key_pem)
        finally:
            os.close(fd)
        context.load_cert_chain(certfile=path)
    finally:
        os.unlink(path)
    return context


def _is_covered_by_static_sans(server_name: str) -> bool:
    """True when the static cert (``localhost`` / one-label ``*.localhost``) matches."""
    if server_name == "localhost":
        return True
    return server_name.endswith(".localhost") and server_name.count(".") == 1


class _SNICertMinter(MutableModel):
    """SNI callback that mints (and caches) per-hostname certs on demand.

    Names the static SANs cover pass through untouched; any deeper
    ``.localhost`` name gets a certificate for that exact hostname, issued by
    the same local CA over the same server key, swapped in via
    ``ssl_socket.context``.
    """

    server_key_pem: bytes = Field(description="PEM of the per-startup server key minted leaves are issued over")
    ca: LocalCertificateAuthority = Field(description="The local CA that signs minted leaves")

    _server_key: rsa.RSAPrivateKey | None = PrivateAttr(default=None)
    _minted_contexts: dict[str, ssl.SSLContext] = PrivateAttr(default_factory=dict)
    _mint_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _get_server_key(self) -> rsa.RSAPrivateKey:
        if self._server_key is None:
            self._server_key = _load_rsa_key(self.server_key_pem)
        return self._server_key

    def _mint_context_for(self, server_name: str) -> ssl.SSLContext:
        minted_leaf_pem = _build_leaf_certificate(self._get_server_key().public_key(), [server_name], self.ca)
        return _load_context_from_pem(minted_leaf_pem + self.ca.cert_pem, self.server_key_pem)

    def __call__(
        self,
        ssl_socket: "ssl.SSLSocket | ssl.SSLObject",
        server_name: str | None,
        _: ssl.SSLContext,
    ) -> int | None:
        # Signature matches CPython's SNI ``servername_callback`` contract
        # (returning None means "proceed"); we never abort the handshake here.
        if server_name is None:
            return None
        name = server_name.lower()
        if _is_covered_by_static_sans(name) or not name.endswith(".localhost"):
            return None
        with self._mint_lock:
            minted = self._minted_contexts.get(name)
            if minted is None:
                if len(self._minted_contexts) >= _MAX_MINTED_CONTEXTS:
                    # Evict the oldest minted name (insertion order) rather
                    # than refusing to mint: the callback runs pre-auth, so a
                    # misbehaving client could otherwise fill the cache and
                    # permanently deny TLS to every later legitimate origin.
                    # An evicted origin simply re-mints on its next handshake.
                    evicted_name = next(iter(self._minted_contexts))
                    del self._minted_contexts[evicted_name]
                    logger.warning(
                        "SNI cert-mint cap ({}) reached; evicting the cert for {} to mint one for {}",
                        _MAX_MINTED_CONTEXTS,
                        evicted_name,
                        name,
                    )
                minted = self._mint_context_for(name)
                self._minted_contexts[name] = minted
        ssl_socket.context = minted
        return None


def build_server_ssl_context(
    chain_pem: bytes,
    server_key_pem: bytes,
    ca: LocalCertificateAuthority,
) -> ssl.SSLContext:
    """Build the serving ``SSLContext``: static loopback leaf + per-SNI minting.

    Handshakes for names the static SANs cover (``localhost``, one-label
    ``*.localhost``) use the static leaf unchanged. Deeper names -- service
    origins like ``svc.host-<hex>.localhost`` and their subtrees -- get a
    certificate minted for that exact hostname on first handshake (cached
    thereafter), issued by the same local CA. This is what lets services
    registered after startup be served over TLS without a restart.
    """
    context = _load_context_from_pem(chain_pem, server_key_pem)
    # typeshed's servername_callback signature types the third argument as
    # SSLSocket, but CPython passes the original SSLContext (per the ssl docs);
    # _SNICertMinter matches the real contract.
    context.set_servername_callback(_SNICertMinter(server_key_pem=server_key_pem, ca=ca))  # ty: ignore[invalid-argument-type]
    return context


class InMemoryTLSConfig(Config):
    """Hypercorn ``Config`` that serves TLS from an in-memory ``SSLContext``.

    Hypercorn's stock ``Config`` gates TLS on ``certfile``/``keyfile`` paths and
    rebuilds the context by loading those files. We hold the context directly
    and override the two hooks hypercorn consults: ``ssl_enabled`` (so
    ``create_sockets`` routes the listen socket into the secure bucket) and
    ``create_ssl_context`` (so ``worker_serve`` uses our context). This keeps
    the cert and key off disk beyond the transient load in
    ``_load_context_from_pem``.
    """

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        super().__init__()
        self._ssl_context = ssl_context

    @property
    def ssl_enabled(self) -> bool:
        return True

    def create_ssl_context(self) -> ssl.SSLContext:
        return self._ssl_context
