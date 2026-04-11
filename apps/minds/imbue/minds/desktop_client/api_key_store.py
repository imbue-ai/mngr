"""API key generation, hashing, and lookup for agent authentication.

Each agent receives a UUID4 API key at creation time. Only the SHA-256
hash is stored on disk, keyed by agent ID. On each request the server
hashes the provided key and scans hash files to identify the caller.
"""

import hashlib
import uuid
from pathlib import Path

from loguru import logger

from imbue.minds.primitives import ApiKeyHash
from imbue.mngr.primitives import AgentId


def generate_api_key() -> str:
    """Generate a new UUID4 API key string."""
    return str(uuid.uuid4())


def hash_api_key(key: str) -> ApiKeyHash:
    """Compute the SHA-256 hex digest of an API key."""
    return ApiKeyHash(hashlib.sha256(key.encode()).hexdigest())


def _api_key_hash_path(data_dir: Path, agent_id: AgentId) -> Path:
    return data_dir / "agents" / str(agent_id) / "api_key_hash"


def save_api_key_hash(
    data_dir: Path,
    agent_id: AgentId,
    key_hash: ApiKeyHash,
) -> None:
    """Write the API key hash to the per-agent hash file."""
    hash_path = _api_key_hash_path(data_dir, agent_id)
    hash_path.parent.mkdir(parents=True, exist_ok=True)
    hash_path.write_text(key_hash)


def find_agent_by_api_key(data_dir: Path, key: str) -> AgentId | None:
    """Hash the key and scan all per-agent hash files for a match.

    Returns the matching AgentId, or None if no match is found.
    """
    key_hash = hash_api_key(key)
    agents_dir = data_dir / "agents"
    if not agents_dir.is_dir():
        return None
    for agent_dir in agents_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        hash_file = agent_dir / "api_key_hash"
        if not hash_file.is_file():
            continue
        try:
            agent_id = AgentId(agent_dir.name)
        except ValueError:
            logger.debug("Skipping non-agent directory in agents/: {}", agent_dir.name)
            continue
        try:
            stored_hash = hash_file.read_text().strip()
        except OSError as e:
            logger.debug("Failed to read API key hash file {}: {}", hash_file, e)
            continue
        if stored_hash == key_hash:
            return agent_id
    return None
