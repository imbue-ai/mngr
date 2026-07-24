"""Unit tests for the Cloudflare tunnel token file path.

The path here must match the file the DEFAULT_WORKSPACE_TEMPLATE cloudflare-tunnel runner watches
(``libs/cloudflare_tunnel/.../runner.py``: ``runtime/secrets/cloudflare_tunnel.env``).
Pinning it on both sides catches an accidental divergence of the contract.
"""

from imbue.minds.desktop_client.tunnel_token_injection import _TUNNEL_TOKEN_FILE
from imbue.minds.desktop_client.tunnel_token_injection import clear_tunnel_token_from_agent
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


def test_tunnel_token_file_is_per_secret_env_in_runtime_secrets() -> None:
    assert _TUNNEL_TOKEN_FILE == "runtime/secrets/cloudflare_tunnel.env"


def test_clear_tunnel_token_never_starts_a_stopped_host() -> None:
    """Clearing the token file must not boot a stopped workspace.

    ``mngr exec`` auto-starts the host by default; deleting a token file from a
    stopped container is pointless (the stale file is explicitly harmless while
    stopped), so the cleanup exec passes --no-start.
    """
    caller = RecordingMngrCaller()
    clear_tunnel_token_from_agent(AgentId.generate(), caller)
    assert len(caller.calls) == 1
    assert caller.calls[0][0] == "exec"
    assert "--no-start" in caller.calls[0]
