import json
from pathlib import Path
from typing import Final

from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import MalformedMngrOutputError
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId

DEFAULT_DESKTOP_CLIENT_HOST: Final[str] = "127.0.0.1"

DEFAULT_DESKTOP_CLIENT_PORT: Final[int] = 8420

MNGR_BINARY: Final[str] = "mngr"


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


class ClientEnvConfig(FrozenModel):
    """Per-tier runtime config read by ``minds run`` (and by per-dev override files).

    This is the *small* file: the desktop client only needs to know which
    remote services it talks to. The matching ``deploy.toml`` carries the
    larger set of values the deploy pipeline needs.

    A dynamic dev env's ``~/.minds/envs/<dev-name>.toml`` uses the same
    shape (it is a self-contained snapshot, not a layered override), and
    may additionally carry a ``[secrets]`` subtable -- captured by
    :class:`LocalDevEnvConfig`.
    """

    connector_url: AnyUrl = Field(description="Base URL of the `remote_service_connector` Modal app for this env.")
    litellm_proxy_url: AnyUrl = Field(
        description="Base URL of the `litellm-proxy` Modal app for this env. Used as the default `ANTHROPIC_BASE_URL` for IMBUE_CLOUD-mode agents."
    )


class DeploySecretsConfig(FrozenModel):
    """The ``[secrets]`` subtable of a ``deploy.toml`` -- which Vault-backed services this tier needs.

    Kept as a nested model so the TOML can be the ergonomic
    ``[secrets]\\nservices = [...]`` shape rather than a flat
    ``secrets_services = [...]`` at top level.
    """

    services: tuple[ServiceName, ...] = Field(
        description=(
            "Service names whose `.minds/template/<service>.sh` schema defines the keys that must be "
            "present at `<vault_path_prefix>/<service>` in Vault. The deploy script iterates this list."
        )
    )


class DeployEnvConfig(FrozenModel):
    """Per-tier deploy-time config read by deploy scripts and `minds env create`.

    Names the Modal workspace + tier-specific Vault path prefix and the
    list of services whose ``.minds/template/<service>.sh`` schemas must
    be pulled from Vault and pushed into Modal as ``<service>-<tier>``.

    OAuth client IDs are not secrets and live here; client secrets stay
    in the ``supertokens`` Vault entry.
    """

    modal_workspace: NonEmptyStr = Field(description="Modal workspace (Modal team/account) this tier deploys into.")
    modal_env: NonEmptyStr | None = Field(
        default=None,
        description=(
            "Optional Modal environment within the workspace. Only meaningful for the dev tier; "
            "dynamic dev envs deploy via `modal deploy --env=<dev-name>`."
        ),
    )
    vault_path_prefix: NonEmptyStr = Field(
        description="HCP Vault path prefix for this tier's secrets, e.g. `secrets/kv/minds/production`."
    )
    cloudflare_domain: NonEmptyStr = Field(
        description="Cloudflare zone domain used by this tier (informational; the connector also reads this from its own Vault entry)."
    )
    oauth_google_client_id: str = Field(
        default="",
        description="Public Google OAuth client id for this tier. Client secret lives in the `supertokens` Vault entry.",
    )
    oauth_github_client_id: str = Field(
        default="",
        description="Public GitHub OAuth client id for this tier. Client secret lives in the `supertokens` Vault entry.",
    )
    secrets: DeploySecretsConfig = Field(
        description="Which `.minds/template/*.sh`-shaped services the deploy step pulls from Vault and pushes to Modal."
    )


def parse_agents_from_mngr_output(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from the first JSON object line of ``mngr list --format json`` stdout.

    Raises ``MalformedMngrOutputError`` when the first non-empty line is not a
    JSON object. stdout is reserved for JSON data; if log lines or SSH errors
    are leaking onto it, fix the underlying process rather than papering over
    it here.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            raise MalformedMngrOutputError(
                f"Expected JSON object on first non-empty mngr output line, got: {stripped[:200]!r}"
            )
        data = json.loads(stripped)
        return data["agents"]
    return []
