"""Unit tests for the Cloudflare tunnel token file path.

The path here must match the file the FCT cloudflare-tunnel runner watches
(``libs/cloudflare_tunnel/.../runner.py``: ``runtime/secrets/cloudflare_tunnel.env``).
Pinning it on both sides catches an accidental divergence of the contract.
"""

from imbue.minds.desktop_client.tunnel_token_injection import _TUNNEL_TOKEN_FILE


def test_tunnel_token_file_is_per_secret_env_in_runtime_secrets() -> None:
    assert _TUNNEL_TOKEN_FILE == "runtime/secrets/cloudflare_tunnel.env"
