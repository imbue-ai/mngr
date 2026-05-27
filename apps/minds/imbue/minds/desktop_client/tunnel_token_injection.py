"""Inject a Cloudflare tunnel token into a running agent's ``runtime/secrets``.

Lives in its own module (rather than alongside the ``/api/v1`` router)
because the two callers -- the Cloudflare tunnel sharing-handler flow
and the workspace-association handler in ``app.py`` -- need this helper
but are not themselves REST endpoints, and routing it through the
router module made the dependency graph unnecessarily fan-shaped.
"""

import shlex

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.mngr.primitives import AgentId


def inject_tunnel_token_into_agent(agent_id: AgentId, token: str) -> None:
    """Write the tunnel token to the agent's runtime/secrets via mngr exec.

    This causes the cloudflare-tunnel service inside the agent to detect
    the token and start cloudflared.
    """
    safe_token = shlex.quote(token)
    cg = ConcurrencyGroup(name="inject-tunnel-token")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                MNGR_BINARY,
                "exec",
                str(agent_id),
                f"mkdir -p runtime && printf 'export CLOUDFLARE_TUNNEL_TOKEN=%s\\n' {safe_token} > runtime/secrets",
            ],
            is_checked_after=False,
        )
    if result.returncode != 0:
        logger.warning("Failed to inject tunnel token into agent {}: {}", agent_id, result.stderr.strip())
