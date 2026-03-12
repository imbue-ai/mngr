from enum import auto
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
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


class DeploymentProvider(UpperCaseStrEnum):
    """Where the mind can be deployed."""

    LOCAL = auto()
    MODAL = auto()
    DOCKER = auto()


class SelfDeployChoice(UpperCaseStrEnum):
    """Whether the mind can launch its own agents."""

    YES = auto()
    NOT_NOW = auto()


def get_default_data_dir() -> Path:
    """Return the default data directory for minds (~/.minds)."""
    return Path.home() / DEFAULT_DATA_DIR_NAME
