import json
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.primitives import AgentId

DEFAULT_DATA_DIR_NAME: Final[str] = ".minds"

DEFAULT_FORWARDING_SERVER_HOST: Final[str] = "127.0.0.1"

DEFAULT_FORWARDING_SERVER_PORT: Final[int] = 8420

MNG_BINARY: Final[str] = "mng"


class MindPaths(FrozenModel):
    """Resolved filesystem paths for minds data storage."""

    data_dir: Path = Field(description="Root directory for minds data (e.g. ~/.minds)")

    @property
    def auth_dir(self) -> Path:
        """Directory for authentication data (signing key, one-time codes)."""
        return self.data_dir / "auth"

    def mind_dir(self, agent_id: AgentId) -> Path:
        """Directory for a specific mind's repo (e.g. ~/.minds/<agent-id>/)."""
        return self.data_dir / str(agent_id)


def get_default_data_dir() -> Path:
    """Return the default data directory for minds (~/.minds)."""
    return Path.home() / DEFAULT_DATA_DIR_NAME


def parse_agents_from_mng_output(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from ``mng list --format json`` stdout.

    The stdout may contain non-JSON lines (e.g. SSH error tracebacks)
    mixed with the JSON. Finds the first line starting with ``{`` and
    parses the ``agents`` array from it.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                return list(data.get("agents", []))
            except json.JSONDecodeError:
                logger.trace("Failed to parse JSON from mng list output line: {}", stripped[:200])
                continue
    return []
