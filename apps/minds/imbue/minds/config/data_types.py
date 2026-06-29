import json
from enum import auto
from pathlib import Path
from typing import Final

from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.errors import DeployLifecycleConfigError
from imbue.minds.errors import MalformedMngrOutputError
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId

DEFAULT_DESKTOP_CLIENT_HOST: Final[str] = "127.0.0.1"

DEFAULT_DESKTOP_CLIENT_PORT: Final[int] = 8420

# `uv run --active` puts the venv bin on PATH, so bare `mngr` resolves.
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

    @property
    def log_dir(self) -> Path:
        """Directory for log files (e.g. ~/.minds/logs).

        Mirrors the Electron shell's ``getLogDir()``: the Python backend's JSONL
        log (``minds-events.jsonl``, via ``--log-file``) and the Electron
        main-process log (``minds.log``) both live here.
        """
        return self.data_dir / "logs"

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
        description="Base URL of the `llm` (LiteLLM proxy) Modal app for this env. Used as the default `ANTHROPIC_BASE_URL` for IMBUE_CLOUD-mode agents."
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


class ModalEnvStrategy(UpperCaseStrEnum):
    """How a tier picks the Modal environment its apps deploy into.

    * ``PER_ENV`` -- the Modal env name equals the activated dev env
      name (e.g. ``dev-josh-1``), so two devs never share one Modal env.
      Used by the ``dev`` tier today.
    * ``SHARED`` -- the Modal env name comes from ``deploy.toml``'s
      ``modal_env`` field (``main`` by convention). Used by
      ``staging`` / ``production``.
    """

    PER_ENV = auto()
    SHARED = auto()


class DeployLifecycleConfig(FrozenModel):
    """Tier-shape flags that drive the unified ``deploy_env`` / ``destroy_env`` paths.

    Every tier declares all four flags explicitly (no defaults) so a
    misconfigured ``deploy.toml`` fails fast on load instead of
    silently routing through a wrong branch. The matrix today:

    +------------+--------------------+--------------------+---------------------+--------------------+
    | tier       | creates_resources  | modal_env_strategy | writes_local_state  | tracks_generation  |
    +============+====================+====================+=====================+====================+
    | dev        | true               | per_env            | true                | false              |
    +------------+--------------------+--------------------+---------------------+--------------------+
    | staging    | false              | shared             | false               | true               |
    +------------+--------------------+--------------------+---------------------+--------------------+
    | production | false              | shared             | false               | true               |
    +------------+--------------------+--------------------+---------------------+--------------------+
    """

    creates_resources: bool = Field(
        description=(
            "Whether the deploy provisions the per-env Modal env, Neon project, and "
            "SuperTokens app outright. ``false`` means the operator brings already-existing "
            "resources via Vault, and the deploy code refuses to call any create/delete "
            "endpoint for those providers."
        ),
    )
    modal_env_strategy: ModalEnvStrategy = Field(
        description=(
            "How to pick the Modal environment the apps deploy into. ``per_env`` uses the "
            "activated dev env name; ``shared`` uses ``deploy_config.modal_env``."
        ),
    )
    writes_local_state: bool = Field(
        description=(
            "Whether the deploy writes ``~/.minds-<env>/client.toml`` + "
            "``secrets.toml`` after a successful deploy. ``false`` for shared tiers "
            "whose ``client.toml`` is committed in-repo."
        ),
    )
    tracks_generation: bool = Field(
        description=(
            "Whether the tier mints + exposes a per-tier generation id (used by activate-time "
            "auto-wipe across developers when the tier gets destroyed + redeployed). Only "
            "useful for shared tiers where multiple developers share one deployment AND "
            "destroy is a real possibility."
        ),
    )

    @model_validator(mode="after")
    def _check_writes_local_state_implies_creates_resources(self) -> "DeployLifecycleConfig":
        """``writes_local_state`` and ``creates_resources`` are coupled today.

        ``deploy_env`` populates the local ``client.toml`` / ``secrets.toml``
        from the records returned by ``providers.create_neon_project`` and
        ``providers.create_supertokens_app`` -- both of which only fire
        when ``creates_resources`` is true. So a tier configured with
        ``writes_local_state=true`` + ``creates_resources=false`` would
        AssertionError partway through deploy, AFTER both Modal apps had
        already been deployed.

        Catching the misconfiguration at ``deploy.toml`` parse time is
        cheaper than letting the deploy run halfway and then bail. The
        coupling is intentional rather than fundamental: if a future tier
        ever needs ``writes_local_state=true`` with operator-managed
        cloud resources, ``deploy_env``'s "Step 6b: local state" branch
        would need to source the DSNs + SuperTokens connection URI from
        Vault (via ``providers.read_per_env_secret_values("neon", ...)``
        and similar) instead of from the create_* records. That's a
        straightforward refactor but not done today, so we keep the
        coupling explicit here.
        """
        if self.writes_local_state and not self.creates_resources:
            raise DeployLifecycleConfigError(
                "deploy.toml [lifecycle] writes_local_state=true requires creates_resources=true. "
                "The combination 'creates_resources=false + writes_local_state=true' is rejected "
                "because deploy_env writes the local client.toml / secrets.toml from the records "
                "returned by create_neon_project / create_supertokens_app, both of which only run "
                "when creates_resources=true. If you need this combination, extend deploy_env's "
                "'Step 6b: local state' to source the DSNs + SuperTokens URI from Vault, then "
                "drop this validator. (See the docstring on this model for details.)"
            )
        return self


class MinContainersConfig(FrozenModel):
    """Warm-pool sizes for each Modal app the tier deploy ships.

    Read by ``minds env deploy`` and threaded into each ``modal deploy``
    invocation as the matching ``MINDS_<APP>_MIN_CONTAINERS`` env var.
    The Modal app reads its value at module load (which is the moment
    ``modal deploy`` serializes the function spec) so the deployed
    function pin includes the configured warm-pool size.

    Defaults are zero so a tier that omits the block (or omits a
    specific service) gets the cheapest possible warm pool. Staging /
    production override to ``1`` in their committed ``deploy.toml`` so
    the desktop client doesn't pay a cold-boot penalty on auth / lease
    / tunnel hits.
    """

    connector: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="Warm containers to keep alive for ``rsc-<tier>`` (remote-service-connector).",
    )
    litellm_proxy: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="Warm containers to keep alive for ``llm-<tier>`` (LiteLLM proxy).",
    )


class ScaledownWindowConfig(FrozenModel):
    """Idle-before-scaledown windows (seconds) for each Modal app the tier ships.

    Read by ``minds env deploy`` and threaded into each ``modal deploy``
    invocation as the matching ``MINDS_<APP>_SCALEDOWN_WINDOW`` env var,
    which the Modal app reads at module load and passes to its function's
    ``scaledown_window``. This keeps a container alive for the configured
    idle window after its last request before Modal scales it down.

    Defaults are ``0`` -- meaning "don't pin it; use Modal's own default
    scaledown window" (Modal requires the value > 0, so the apps normalize
    ``0`` to ``None``). Dev tiers raise this to ~10 minutes so their
    no-warm-pool apps (``min_containers = 0``) stay hot across a dev session
    instead of cold-booting on every request. Staging / production leave it
    at ``0`` and rely on ``min_containers`` instead, and the ci/test tier
    leaves it at ``0`` so test containers tear down promptly.
    """

    connector: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="Idle seconds before ``rsc-<tier>`` scales a container down (0 = Modal default).",
    )
    litellm_proxy: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="Idle seconds before ``llm-<tier>`` scales a container down (0 = Modal default).",
    )


class PaidDefaultsConfig(FrozenModel):
    """Default paid-access entries seeded into the connector's paid tables on deploy.

    After the pool-hosts schema migrations run, ``minds env deploy`` seeds
    these into ``paid_domains`` / ``paid_emails`` (as ``is_paid = true``)
    using ``INSERT ... ON CONFLICT DO NOTHING`` -- i.e. **seed-if-absent**:
    it sets the tier's initial default but never re-activates an entry an
    operator later soft-removed, so a redeploy doesn't fight manual changes.
    Values are lowercased to match the connector's normalized lookups.
    Empty lists (the default) seed nothing.
    """

    domains: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="Domains seeded into paid_domains (e.g. ``imbue.com``); exact-domain match grants paid access.",
    )
    emails: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="Full email addresses seeded into paid_emails.",
    )


class DeployEnvConfig(FrozenModel):
    """Per-tier deploy-time config read by deploy scripts and `minds env create`.

    Names the Modal workspace + tier-specific Vault path prefix and the
    list of services whose ``.minds/template/<service>.sh`` schemas must
    be pulled from Vault and pushed into Modal as ``<service>-<tier>``.
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
    secrets: DeploySecretsConfig = Field(
        description="Which `.minds/template/*.sh`-shaped services the deploy step pulls from Vault and pushes to Modal."
    )
    lifecycle: DeployLifecycleConfig = Field(
        description=(
            "Tier-shape flags that drive ``deploy_env`` / ``destroy_env`` branching. All "
            "four flags are required (no defaults) so a misconfigured deploy.toml fails "
            "fast on load."
        ),
    )
    min_containers: MinContainersConfig = Field(
        default_factory=MinContainersConfig,
        description=(
            "Per-service warm-pool sizes for the Modal apps this tier ships. "
            "Each entry is threaded into the matching ``modal deploy`` as an env var "
            "(``MINDS_CONNECTOR_MIN_CONTAINERS`` / ``MINDS_LITELLM_PROXY_MIN_CONTAINERS``) "
            "so the deployed function pin honors the tier's config."
        ),
    )
    scaledown_window: ScaledownWindowConfig = Field(
        default_factory=ScaledownWindowConfig,
        description=(
            "Per-service idle-before-scaledown windows (seconds) for the Modal apps this "
            "tier ships. Threaded into the matching ``modal deploy`` as an env var "
            "(``MINDS_CONNECTOR_SCALEDOWN_WINDOW`` / ``MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW``); "
            "0 means use Modal's own default."
        ),
    )
    paid: PaidDefaultsConfig = Field(
        default_factory=PaidDefaultsConfig,
        description=(
            "Default paid-access entries seeded (seed-if-absent) into the connector's "
            "paid_domains / paid_emails tables after migrations on each deploy."
        ),
    )


def parse_agents_from_mngr_output(stdout: str) -> list[dict[str, object]]:
    """Extract agent records from the first JSON object line of ``mngr list --format json`` stdout.

    Raises ``MalformedMngrOutputError`` when the first non-empty line is not a
    JSON object, when stdout is empty/blank, or when the parsed object lacks an
    ``agents`` key. stdout is reserved for JSON data; if log lines or SSH errors
    are leaking onto it, fix the underlying process rather than papering over
    it here. ``mngr list --format json`` always serializes its result set as a
    ``{"agents": [...]}`` object (zero agents is ``{"agents": []}``), so empty
    stdout means the command produced no output at all rather than "no agents".
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
        if "agents" not in data:
            raise MalformedMngrOutputError(f"mngr output JSON object missing 'agents' key: {stripped[:200]!r}")
        return data["agents"]
    raise MalformedMngrOutputError("Expected a JSON object in mngr output, but stdout was empty/blank")
