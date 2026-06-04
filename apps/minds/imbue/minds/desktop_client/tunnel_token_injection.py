"""Manage the Cloudflare tunnel token in a running agent's secrets directory.

Lives in its own module (rather than alongside the ``/api/v1`` router)
because the callers -- the Cloudflare tunnel sharing-handler flow, the
workspace-association handler, and the workspace-disassociation handler in
``app.py`` -- need these helpers but are not themselves REST endpoints, and
routing them through the router module made the dependency graph
unnecessarily fan-shaped.

The token lives at ``runtime/secrets/cloudflare_tunnel.env`` inside the agent.
``runtime/secrets/`` is a directory of per-secret ``*.env`` files (this token,
``restic.env`` for backups, ``telegram.env`` for the bot); each writer owns its
own file so they never clobber one another. The agent's cloudflare-tunnel
service (``libs/cloudflare_tunnel/.../runner.py``) watches this file: it starts
cloudflared when the token appears and stops it when the file is removed.
"""

import shlex
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.mngr.primitives import AgentId

# Path (relative to the agent's work_dir) the cloudflare-tunnel service watches.
_TUNNEL_TOKEN_FILE: Final[str] = "runtime/secrets/cloudflare_tunnel.env"


def inject_tunnel_token_into_agent(agent_id: AgentId, token: str) -> None:
    """Write the tunnel token to the agent's cloudflare_tunnel.env via mngr exec.

    This causes the cloudflare-tunnel service inside the agent to detect
    the token and start cloudflared. Overwrites any prior token in place.
    """
    safe_token = shlex.quote(token)
    cg = ConcurrencyGroup(name="inject-tunnel-token")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                MNGR_BINARY,
                "exec",
                str(agent_id),
                f"mkdir -p runtime/secrets && printf 'export CLOUDFLARE_TUNNEL_TOKEN=%s\\n' {safe_token} > {_TUNNEL_TOKEN_FILE}",
            ],
            is_checked_after=False,
        )
    if result.returncode != 0:
        logger.warning("Failed to inject tunnel token into agent {}: {}", agent_id, result.stderr.strip())


def clear_tunnel_token_from_agent(agent_id: AgentId) -> None:
    """Remove the agent's cloudflare_tunnel.env via mngr exec.

    This causes the cloudflare-tunnel service inside the agent to detect the
    token's removal and stop cloudflared. Best-effort: a failure here only
    leaves a stale token file (cloudflared keeps running against a now-deleted
    tunnel until the agent stops), which is logged but not fatal.
    """
    cg = ConcurrencyGroup(name="clear-tunnel-token")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                MNGR_BINARY,
                "exec",
                str(agent_id),
                f"rm -f {_TUNNEL_TOKEN_FILE}",
            ],
            is_checked_after=False,
        )
    if result.returncode != 0:
        logger.warning("Failed to clear tunnel token from agent {}: {}", agent_id, result.stderr.strip())
