import json
from pathlib import Path
from typing import Final

from pydantic import AnyUrl
from pydantic import ConfigDict
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
    """Per-env runtime config read by ``minds run``.

    The non-secret half of an env's on-disk state. Used in two places:

    * Staging / production: ``apps/minds/imbue/minds/config/envs/<tier>/client.toml``
      is committed to the repo. The deploy writer is forbidden from ever
      adding fields beyond the URLs declared here -- a separate
      :class:`PublicClientEnvConfig` type and a runtime guard in
      ``envs/local_store.py`` make sure no secret can sneak into a
      committed file.
    * Dev envs: ``~/.minds-<env-name>/client.toml`` (chmod 0644) is
      written by ``minds env deploy <name>``; secrets land in a separate
      chmod-0600 ``secrets.toml`` next to it (see :class:`DevEnvSecretsModel`
      in ``envs/local_store.py``).

    Unknown top-level fields are rejected so a misconfigured tier file
    fails fast rather than silently dropping unsupported knobs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=False)

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
    modal_env: NonEmptyStr = Field(
        default=NonEmptyStr("main"),
        description=(
            "Modal *environment* name to deploy this tier's apps into. Only consulted for "
            "staging / production deploys -- dev-env deploys always pin the Modal env to the "
            "activated dev env name (so two devs never share one Modal env). Defaults to ``main`` "
            "(the convention staging / production both follow today)."
        ),
    )
    vault_path_prefix: NonEmptyStr = Field(
        description="HCP Vault path prefix for this tier's secrets, e.g. `secrets/minds/production`."
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
