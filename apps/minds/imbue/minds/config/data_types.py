import json
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId

DEFAULT_DESKTOP_CLIENT_HOST: Final[str] = "127.0.0.1"

DEFAULT_DESKTOP_CLIENT_PORT: Final[int] = 8420

MNGR_BINARY: Final[str] = "mngr"

DEFAULT_CLOUDFLARE_FORWARDING_URL: Final[str] = "https://joshalbrecht--cloudflare-forwarding-fastapi-app.modal.run"

DEFAULT_SUPERTOKENS_CONNECTION_URI: Final[str] = (
    "https://st-dev-aba73a80-3754-11f1-9afe-f5bb4fa720bc.aws.supertokens.io"
)


class WorkspacePaths(FrozenModel):
    """Resolved filesystem paths for minds data storage."""

    data_dir: Path = Field(description="Root directory for minds data (e.g. ~/.minds)")

    @property
    def auth_dir(self) -> Path:
        """Directory for authentication data (signing key, one-time codes)."""
        return self.data_dir / "auth"

    @property
    def mngr_host_dir(self) -> Path:
        """Directory where mngr stores agent state for this minds install (e.g. ~/.minds/mngr)."""
        return self.data_dir / "mngr"

    def workspace_dir(self, agent_id: AgentId) -> Path:
        """Directory for a specific workspace's repo (e.g. ~/.minds/<agent-id>/)."""
        return self.data_dir / str(agent_id)


class MindsConfig(FrozenModel):
    """Minds application configuration.

    Loaded from ``<data_dir>/config.toml`` with TOML keys matching field names.
    Each field's ``alias`` names the environment variable that overrides the
    file value. Precedence is env > file > built-in default.
    """

    cloudflare_forwarding_url: AnyUrl = Field(
        default=AnyUrl(DEFAULT_CLOUDFLARE_FORWARDING_URL),
        alias="CLOUDFLARE_FORWARDING_URL",
        description="Base URL of the Cloudflare forwarding API (defaults to the dev deployment)",
    )
    supertokens_connection_uri: AnyUrl = Field(
        default=AnyUrl(DEFAULT_SUPERTOKENS_CONNECTION_URI),
        alias="SUPERTOKENS_CONNECTION_URI",
        description="URI of the SuperTokens core (defaults to the dev deployment)",
    )

    model_config = {**FrozenModel.model_config, "populate_by_name": True}


def parse_agents_from_mngr_output(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from ``mngr list --format json`` stdout.

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
                logger.trace("Failed to parse JSON from mngr list output line: {}", stripped[:200])
                continue
    return []
