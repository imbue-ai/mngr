"""Unit tests for the local-CA-backed TLS helpers."""

import ipaddress
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from imbue.mngr_forward.testing import make_in_memory_test_ca
from imbue.mngr_forward.tls import InMemoryTLSConfig
from imbue.mngr_forward.tls import _MAX_MINTED_CONTEXTS
from imbue.mngr_forward.tls import _SNICertMinter
from imbue.mngr_forward.tls import _is_covered_by_static_sans
from imbue.mngr_forward.tls import build_server_ssl_context
from imbue.mngr_forward.tls import generate_server_credentials
from imbue.mngr_forward.tls import load_or_create_local_ca


def _leaf_certificate(chain_pem: bytes) -> x509.Certificate:
    """The first certificate in a PEM chain (the server leaf)."""
    return x509.load_pem_x509_certificates(chain_pem)[0]


def test_load_or_create_local_ca_persists_across_calls(tmp_path: Path) -> None:
    """The CA is created once and re-loaded byte-identical thereafter.

    Persistence is what makes the one-time browser trust install durable: a
    regenerating CA would invalidate the install on every restart.
    """
    ca_dir = tmp_path / "ca"
    first = load_or_create_local_ca(ca_dir)
    second = load_or_create_local_ca(ca_dir)
    assert first.cert_pem == second.cert_pem
    assert first.key_pem == second.key_pem
    certificate = x509.load_pem_x509_certificate(first.cert_pem)
    basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca is True


def test_generate_server_credentials_has_expected_sans_and_chain() -> None:
    """The leaf must cover `localhost`, `*.localhost`, and `127.0.0.1`, chained to the CA.

    `*.localhost` is required for the bare `host-<id>.localhost` workspace
    origins (the wildcard does not match the bare `localhost` label, so both
    entries are needed); `127.0.0.1` covers loopback probes that dial the IP.
    Nested service origins are covered by per-SNI minting, not this cert.
    """
    ca = make_in_memory_test_ca()
    chain_pem, key_pem = generate_server_credentials(ca)
    assert b"PRIVATE KEY" in key_pem
    certificates = x509.load_pem_x509_certificates(chain_pem)
    assert len(certificates) == 2
    leaf = certificates[0]
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"localhost", "*.localhost"}
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
    # macOS requires serverAuth EKU on certs from user-installed roots.
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    # The leaf must chain to the CA (issuer == CA subject).
    ca_certificate = x509.load_pem_x509_certificate(ca.cert_pem)
    assert leaf.issuer == ca_certificate.subject


def _server_context() -> ssl.SSLContext:
    ca = make_in_memory_test_ca()
    chain_pem, key_pem = generate_server_credentials(ca)
    return build_server_ssl_context(chain_pem, key_pem, ca)


def _handshake(
    server_context: ssl.SSLContext,
    client_offers: list[str],
    server_hostname: str,
) -> tuple[ssl.SSLObject, ssl.SSLObject]:
    """Drive a full TLS handshake in-memory (no sockets); return (client, server)."""
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    client_context.set_alpn_protocols(client_offers)

    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=server_hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)

    # Pump both directions until both sides finish the handshake. A bounded loop
    # guards against a stuck handshake instead of spinning forever.
    for _ in range(20):
        for endpoint, out_bio, peer_in in ((client, client_out, server_in), (server, server_out, client_in)):
            try:
                endpoint.do_handshake()
            except ssl.SSLWantReadError:
                pass
            pending = out_bio.read()
            if pending:
                peer_in.write(pending)
        if client.selected_alpn_protocol() is not None and server.selected_alpn_protocol() is not None:
            break
    return client, server


def _negotiate_alpn(server_context: ssl.SSLContext, client_offers: list[str]) -> str | None:
    _client, server = _handshake(server_context, client_offers, server_hostname="localhost")
    return server.selected_alpn_protocol()


def _served_dns_names(server_context: ssl.SSLContext, server_hostname: str) -> list[str]:
    """Handshake against ``server_hostname`` and return the served cert's DNS SANs."""
    client, _server = _handshake(server_context, ["http/1.1"], server_hostname=server_hostname)
    cert_der = client.getpeercert(binary_form=True)
    assert cert_der is not None
    certificate = x509.load_der_x509_certificate(cert_der)
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return list(san.get_values_for_type(x509.DNSName))


def test_build_server_ssl_context_negotiates_h2_when_offered() -> None:
    """A client offering h2 must be given h2 (the whole point of the cert path)."""
    context = _server_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert _negotiate_alpn(context, ["h2", "http/1.1"]) == "h2"


def test_build_server_ssl_context_falls_back_to_http1_for_ws_clients() -> None:
    """A client that only offers http/1.1 (e.g. a WebSocket upgrade) gets http/1.1."""
    context = _server_context()
    assert _negotiate_alpn(context, ["http/1.1"]) == "http/1.1"


def test_static_cert_served_for_bare_workspace_origin() -> None:
    """One-label ``host-<hex>.localhost`` names are covered by the static ``*.localhost`` SAN."""
    context = _server_context()
    served = _served_dns_names(context, "host-0123456789abcdef0123456789abcdef.localhost")
    assert set(served) == {"localhost", "*.localhost"}


def test_sni_minting_covers_nested_service_origin() -> None:
    """A nested service origin gets a cert minted for that exact hostname on handshake."""
    context = _server_context()
    hostname = "terminal.host-0123456789abcdef0123456789abcdef.localhost"
    assert _served_dns_names(context, hostname) == [hostname]


def test_sni_minting_covers_arbitrary_depth() -> None:
    """Deep sub-origins (multi-origin apps) are also minted per exact hostname."""
    context = _server_context()
    hostname = "uuid1234.openvscode.host-0123456789abcdef0123456789abcdef.localhost"
    assert _served_dns_names(context, hostname) == [hostname]
    # A second handshake for the same name serves the cached cert unchanged.
    assert _served_dns_names(context, hostname) == [hostname]


def test_sni_minting_evicts_oldest_when_cap_reached() -> None:
    """A full mint cache evicts its oldest entry instead of denying new origins.

    The SNI callback runs pre-authentication, so a misbehaving local client
    could fill the cache; new legitimate origins must still get a minted cert.
    """
    ca = make_in_memory_test_ca()
    _chain_pem, key_pem = generate_server_credentials(ca)
    minter = _SNICertMinter(server_key_pem=key_pem, ca=ca)
    placeholder = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    for index in range(_MAX_MINTED_CONTEXTS):
        minter._minted_contexts[f"x{index}.host-a.localhost"] = placeholder
    stub_socket = cast(ssl.SSLObject, SimpleNamespace(context=None))
    hostname = "svc.host-0123456789abcdef0123456789abcdef.localhost"
    minter(stub_socket, hostname, placeholder)
    assert stub_socket.context is minter._minted_contexts[hostname]
    assert stub_socket.context is not placeholder
    # The oldest entry made room; the cache stays at the cap.
    assert "x0.host-a.localhost" not in minter._minted_contexts
    assert len(minter._minted_contexts) == _MAX_MINTED_CONTEXTS


def test_is_covered_by_static_sans() -> None:
    assert _is_covered_by_static_sans("localhost")
    assert _is_covered_by_static_sans("host-abc.localhost")
    assert not _is_covered_by_static_sans("svc.host-abc.localhost")
    assert not _is_covered_by_static_sans("example.com")


def test_in_memory_tls_config_enables_ssl_and_returns_context() -> None:
    """The Config subclass must report TLS enabled and hand back our context.

    Hypercorn gates the secure socket bucket on `ssl_enabled` and builds the
    listener's TLS from `create_ssl_context()`, so both hooks must reflect the
    in-memory context rather than the stock certfile/keyfile path behaviour.
    """
    context = _server_context()
    config = InMemoryTLSConfig(context)
    assert config.ssl_enabled is True
    assert config.create_ssl_context() is context
