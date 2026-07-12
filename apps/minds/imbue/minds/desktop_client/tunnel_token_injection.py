"""Manage the Cloudflare tunnel token in a running agent's secrets directory.

Lives in its own module (rather than alongside the ``/api/v1`` router)
because the callers -- the Cloudflare tunnel sharing-handler flow, the
workspace-association handler, and the workspace-disassociation handler in
``app.py`` -- need these helpers but are not themselves REST endpoints, and
routing them through the router module made the dependency graph
unnecessarily fan-shaped.

The token lives at ``runtime/secrets/cloudflare_tunnel.env`` inside the agent.
``runtime/secrets/`` is a directory of per-secret ``*.env`` files (this token,
``restic.env`` for backups); each writer owns its
own file so they never clobber one another. The agent's cloudflare-tunnel
service (``libs/cloudflare_tunnel/.../runner.py``) watches this file: it starts
cloudflared when the token appears and stops it when the file is removed.
"""

import shlex
from typing import Final

from loguru import logger

from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# Path (relative to the agent's work_dir) the cloudflare-tunnel service watches.
_TUNNEL_TOKEN_FILE: Final[str] = "runtime/secrets/cloudflare_tunnel.env"

# Generous ceiling for the single ``mngr exec`` that writes/removes the token file.
_TUNNEL_TOKEN_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0


def inject_tunnel_token_into_agent(agent_id: AgentId, token: str, mngr_caller: MngrCaller) -> None:
    """Write the tunnel token to the agent's cloudflare_tunnel.env via mngr exec.

    This causes the cloudflare-tunnel service inside the agent to detect
    the token and start cloudflared. Overwrites any prior token in place. The
    ``mngr exec`` runs through the shared warm-process ``mngr_caller`` (the same
    one that drives the tunnel's ``mngr imbue_cloud`` calls).
    """
    safe_token = shlex.quote(token)
    result = mngr_caller.call(
        [
            "exec",
            str(agent_id),
            f"mkdir -p runtime/secrets && printf 'export CLOUDFLARE_TUNNEL_TOKEN=%s\\n' {safe_token} > {_TUNNEL_TOKEN_FILE}",
        ],
        timeout=_TUNNEL_TOKEN_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("Failed to inject tunnel token into agent {}: {}", agent_id, result.stderr.strip())


def clear_tunnel_token_from_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> None:
    """Remove the agent's cloudflare_tunnel.env via mngr exec.

    This causes the cloudflare-tunnel service inside the agent to detect the
    token's removal and stop cloudflared. Best-effort: a failure here only
    leaves a stale token file (cloudflared keeps running against a now-deleted
    tunnel until the agent stops), which is logged but not fatal. Runs through
    the shared warm-process ``mngr_caller``.
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"rm -f {_TUNNEL_TOKEN_FILE}"],
        timeout=_TUNNEL_TOKEN_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("Failed to clear tunnel token from agent {}: {}", agent_id, result.stderr.strip())
