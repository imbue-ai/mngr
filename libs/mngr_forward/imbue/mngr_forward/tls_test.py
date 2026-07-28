"""Unit tests for the ephemeral in-memory TLS helpers."""

import ipaddress
import ssl

from cryptography import x509

from imbue.mngr_forward.tls import InMemoryTLSConfig
from imbue.mngr_forward.tls import build_server_ssl_context
from imbue.mngr_forward.tls import generate_self_signed_cert


def test_generate_self_signed_cert_has_expected_sans() -> None:
    """The cert must cover `localhost`, `*.localhost`, and `127.0.0.1`.

    `*.localhost` is required for the `agent-<id>.localhost` workspace
    subdomains (the wildcard does not match the bare `localhost` label, so both
    entries are needed); `127.0.0.1` covers loopback probes that dial the IP.
    """
    cert_pem, key_pem = generate_self_signed_cert()
    assert b"BEGIN CERTIFICATE" in cert_pem
    assert b"PRIVATE KEY" in key_pem
    certificate = x509.load_pem_x509_certificate(cert_pem)
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addresses = san.get_values_for_type(x509.IPAddress)
    assert set(dns_names) == {"localhost", "*.localhost"}
    assert ipaddress.ip_address("127.0.0.1") in ip_addresses


def _handshake_in_memory(
    server_context: ssl.SSLContext,
    server_hostname: str = "localhost",
    client_alpn_offers: list[str] | None = None,
) -> tuple[ssl.SSLObject, ssl.SSLObject]:
    """Drive a full TLS handshake in-memory (no sockets); return the ``(client, server)`` SSL objects."""
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    if client_alpn_offers is not None:
        client_context.set_alpn_protocols(client_alpn_offers)

    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=server_hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)

    # Pump both directions until both sides finish the handshake. A bounded loop
    # guards against a stuck handshake instead of spinning forever.
    for _ in range(20):
        is_handshake_done = True
        for endpoint, out_bio, peer_in in ((client, client_out, server_in), (server, server_out, client_in)):
            try:
                endpoint.do_handshake()
            except ssl.SSLWantReadError:
                is_handshake_done = False
            pending = out_bio.read()
            if pending:
                peer_in.write(pending)
        if is_handshake_done:
            return client, server
    raise AssertionError("handshake did not complete")


def _negotiate_alpn(server_context: ssl.SSLContext, client_offers: list[str]) -> str | None:
    """Full in-memory handshake offering ``client_offers``; return the server's ALPN choice."""
    _, server = _handshake_in_memory(server_context, client_alpn_offers=client_offers)
    return server.selected_alpn_protocol()


def test_build_server_ssl_context_negotiates_h2_when_offered() -> None:
    """A client offering h2 must be given h2 (the whole point of the cert path)."""
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert _negotiate_alpn(context, ["h2", "http/1.1"]) == "h2"


def test_build_server_ssl_context_falls_back_to_http1_for_ws_clients() -> None:
    """A client that only offers http/1.1 (e.g. a WebSocket upgrade) gets http/1.1."""
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    assert _negotiate_alpn(context, ["http/1.1"]) == "http/1.1"


def _handshake_and_get_server_cert(server_context: ssl.SSLContext, server_hostname: str) -> x509.Certificate:
    """Full in-memory handshake against ``server_hostname``; return the leaf cert served."""
    client, _ = _handshake_in_memory(server_context, server_hostname=server_hostname)
    der = client.getpeercert(binary_form=True)
    assert der is not None
    return x509.load_der_x509_certificate(der)


def _cert_dns_names(certificate: x509.Certificate) -> set[str]:
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(san.get_values_for_type(x509.DNSName))


def test_sni_mints_cert_for_nested_service_origin() -> None:
    """A service origin (two labels deep, beyond `*.localhost`) gets a cert
    minted for that exact hostname on first handshake -- the mechanism that
    covers services registered at any time without a proxy restart."""
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    served = _handshake_and_get_server_cert(context, "svc.agent-deadbeef.localhost")
    assert "svc.agent-deadbeef.localhost" in _cert_dns_names(served)


def test_sni_mints_cert_for_deep_sub_origin() -> None:
    """Arbitrary depth (a service's own sub-origin space) is covered too."""
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    served = _handshake_and_get_server_cert(context, "deep.svc.agent-deadbeef.localhost")
    assert "deep.svc.agent-deadbeef.localhost" in _cert_dns_names(served)


def test_sni_uses_static_cert_for_bare_workspace_origin() -> None:
    """One-label hosts stay on the static `*.localhost` cert (no minting)."""
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    served = _handshake_and_get_server_cert(context, "agent-deadbeef.localhost")
    assert _cert_dns_names(served) == {"localhost", "*.localhost"}


def test_sni_minted_context_still_negotiates_h2() -> None:
    """The swapped-in per-hostname context must keep the ALPN offer (h2)."""
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    _, server = _handshake_in_memory(
        context,
        server_hostname="svc.agent-deadbeef.localhost",
        client_alpn_offers=["h2", "http/1.1"],
    )
    assert server.selected_alpn_protocol() == "h2"


def test_in_memory_tls_config_enables_ssl_and_returns_context() -> None:
    """The Config subclass must report TLS enabled and hand back our context.

    Hypercorn gates the secure socket bucket on `ssl_enabled` and builds the
    listener's TLS from `create_ssl_context()`, so both hooks must reflect the
    in-memory context rather than the stock certfile/keyfile path behaviour.
    """
    cert_pem, key_pem = generate_self_signed_cert()
    context = build_server_ssl_context(cert_pem, key_pem)
    config = InMemoryTLSConfig(context)
    assert config.ssl_enabled is True
    assert config.create_ssl_context() is context
