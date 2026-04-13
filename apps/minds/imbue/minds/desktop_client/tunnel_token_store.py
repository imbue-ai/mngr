"""Storage for Cloudflare tunnel tokens, keyed by agent ID.

Tunnel tokens are stored on disk so they can be re-injected into agents
when the desktop client restarts or when agents are rediscovered.
"""

from pathlib import Path

from loguru import logger

from imbue.mngr.primitives import AgentId


def _tunnel_token_path(data_dir: Path, agent_id: AgentId) -> Path:
    return data_dir / "agents" / str(agent_id) / "tunnel_token"


def save_tunnel_token(data_dir: Path, agent_id: AgentId, token: str) -> None:
    """Write the tunnel token to the per-agent token file."""
    token_path = _tunnel_token_path(data_dir, agent_id)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token)
    logger.debug("Saved tunnel token for agent {}", agent_id)


def load_tunnel_token(data_dir: Path, agent_id: AgentId) -> str | None:
    """Read the tunnel token for an agent, or None if not stored."""
    token_path = _tunnel_token_path(data_dir, agent_id)
    if not token_path.is_file():
        return None
    try:
        token = token_path.read_text().strip()
        return token if token else None
    except OSError as e:
        logger.debug("Failed to read tunnel token for agent {}: {}", agent_id, e)
        return None


def clear_tunnel_token(data_dir: Path, agent_id: AgentId) -> None:
    """Remove the stored tunnel token for an agent."""
    token_path = _tunnel_token_path(data_dir, agent_id)
    if token_path.is_file():
        token_path.unlink()
        logger.debug("Cleared tunnel token for agent {}", agent_id)
