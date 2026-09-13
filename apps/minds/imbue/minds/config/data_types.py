import json
import re
from collections.abc import Mapping
from enum import auto
from functools import cached_property
from ipaddress import IPv4Address
from ipaddress import IPv4Network
from pathlib import Path
from typing import Final

from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import computed_field
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.minds.errors import DeployLifecycleConfigError
from imbue.minds.errors import MalformedMngrOutputError
from imbue.minds.errors import ManagementPlaneConfigError
from imbue.minds.errors import OriginsConfigError
from imbue.minds.errors import SshCaConfigError
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId

DEFAULT_DESKTOP_CLIENT_HOST: Final[str] = "127.0.0.1"

DEFAULT_DESKTOP_CLIENT_PORT: Final[int] = 8420

# `uv run --active` puts the venv bin on PATH, so bare `mngr` resolves.
MNGR_BINARY: Final[str] = "mngr"


class InstallationPaths(FrozenModel):
    """Resolved filesystem paths of one minds installation (one data directory on one device)."""

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
      is committed to the repo, so it must never carry a secret. What keeps
      it that way is ``write_client_config`` in ``envs/local_store.py``:
      it names every key it emits, one at a time, rather than serializing
      whatever this model happens to hold.
    * Dev envs: ``~/.minds-<env-name>/client.toml`` (chmod 0644) is
      written by ``minds-admin env deploy <name>``; secrets land in a separate
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
    lima_image_base_url: AnyUrl | None = Field(
        default=None,
        description=(
            "Root URL of the pre-baked Lima image chunk store / CDN for this env. "
            "When set together with `lima_image_minisign_public_key`, the desktop app prefetches the "
            "current release's image and points Lima at it for fast local creates. None disables the "
            "pre-baked path (the app always builds the workspace in-VM). Validated as a URL at config "
            "load so a malformed value fails fast rather than at first fetch."
        ),
    )
    lima_image_minisign_public_key: str | None = Field(
        default=None,
        description=(
            "Minisign public key (single-line 'RW...' form) the pre-baked image's signed root manifest "
            "is verified against. Required alongside `lima_image_base_url`."
        ),
    )
    update_feed_base_url: AnyUrl | None = Field(
        default=None,
        description=(
            "Root URL serving the release-channel manifests for this env, every channel "
            "including stable. A build carrying no value here falls back to ToDesktop's own "
            "feed and offers stable only; production always sets one, so that fallback is a "
            "guard rather than a path anything takes. Set together with the promotion job "
            "that writes those manifests; see specs/minds-release-channels/spec.md."
        ),
    )
    accounts_base_url: AnyUrl | None = Field(
        default=None,
        description=(
            "Base URL of the accounts broker shared-workspace visitors sign in through "
            "(production: https://accounts.imbue.com). None falls back to `connector_url` "
            "(the broker is served by the connector app, so the plain connector URL works "
            "wherever no dedicated accounts domain exists yet, e.g. dev envs)."
        ),
    )

    def accounts_origin_url(self) -> str:
        """The tier's browser accounts origin, without a trailing slash.

        Prefers ``accounts_base_url`` (production: https://accounts.imbue.com)
        and falls back to ``connector_url``, which serves the same pages on
        tiers without a dedicated accounts domain.
        """
        if self.accounts_base_url is not None:
            return str(self.accounts_base_url).rstrip("/")
        return str(self.connector_url).rstrip("/")


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

    Read by ``minds-admin env deploy`` and threaded into each ``modal deploy``
    invocation as the matching ``MINDS_<APP>_MIN_CONTAINERS`` env var.
    The Modal app reads its value at module load (which is the moment
    ``modal deploy`` serializes the function spec) so the deployed
    function pin includes the configured warm-pool size.

    Defaults are zero so a tier that omits the block (or omits a
    specific service) gets the cheapest possible warm pool. Staging /
    production override to ``1`` in their committed ``deploy.toml`` so
    the desktop client doesn't pay a cold-boot penalty on auth / lease
    / share hits.
    """

    connector: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="Warm containers to keep alive for ``rsc-<tier>`` (remote-service-connector).",
    )
    litellm_proxy: NonNegativeInt = Field(
        default=NonNegativeInt(0),
        description="Warm containers to keep alive for ``llm-<tier>`` (LiteLLM proxy).",
    )


class AnalyticsDeployConfig(FrozenModel):
    """Whether the tier deploys the analytics app (``analytics-<tier>``).

    The tier default: off everywhere until the once-per-tier bringup runbook
    (apps/analytics/docs/bringup.md) has provisioned the Neon project, R2
    buckets, and Vault entry the app needs. Dynamic dev envs override the
    tier default with the sticky ``minds env deploy --with-analytics`` /
    ``--without-analytics`` flag (persisted in the env's local state).
    """

    is_deployed: bool = Field(
        default=False,
        description="Deploy the analytics app (push its Modal Secret, run its ops migrations, `modal deploy`).",
    )


class ScaledownWindowConfig(FrozenModel):
    """Idle-before-scaledown windows (seconds) for each Modal app the tier ships.

    Read by ``minds-admin env deploy`` and threaded into each ``modal deploy``
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


class StorageDeployConfig(FrozenModel):
    """The ``[storage]`` block of a ``deploy.toml`` -- git-owned workspace-storage knobs.

    The tier's storage *credentials* stay in Vault (the ``storage`` service
    entry); this block carries only the deploy-time-owned settings stamped
    over them into the pushed Modal Secret.
    """

    stop_retention_seconds: NonNegativeInt | None = Field(
        default=None,
        description=(
            "Seconds a stopped workspace's halted VM lingers on its box for instant "
            "restart-in-place before the retention finalize frees the slot. Stamped as "
            "``WORKSPACE_STOP_RETENTION_SECONDS`` over the Vault entry at deploy time; "
            "unset defers to the Vault value (or the connector's 3600s default). ci/dev "
            "set this low so stop/start tests finish in minutes rather than waiting out "
            "an hour-long window."
        ),
    )


class PaidDefaultsConfig(FrozenModel):
    """Default paid-access entries seeded into the connector's paid tables on deploy.

    After the pool-hosts schema migrations run, ``minds-admin env deploy`` seeds
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


class PlanQuotasConfig(FrozenModel):
    """One plan's default quota entitlements, as committed in deploy.toml.

    Written (overwriting) into the connector's ``plans`` table on every
    deploy -- deploy.toml is the source of truth for plan definitions, while
    per-user entitlement rows (copied from the plan at assignment) are only
    ever edited via the admin API. Storage is configured in GB for
    readability; the writer converts to bytes for the BIGINT column.
    """

    max_remote_workspaces: NonNegativeInt = Field(description="Max running remote workspaces")
    max_total_workspaces: NonNegativeInt = Field(description="Max total remote workspaces, running + stopped")
    max_buckets: NonNegativeInt = Field(description="Max R2 buckets")
    max_total_bucket_gb: NonNegativeInt = Field(description="Max total GB across all the account's buckets")
    monthly_llm_spend_usd: NonNegativeFloat = Field(
        description="Monthly LLM spend cap in USD (rolling; 0 disables imbue-cloud key minting)"
    )
    max_active_synced_workspaces: NonNegativeInt = Field(description="Max ACTIVE synced workspace records")
    max_active_machine_units: NonNegativeInt = Field(
        description="Max machine units (1 unit = 1GiB guest RAM) summed across running remote machines"
    )
    max_total_machine_disk_gb: NonNegativeInt = Field(
        description="Max machine data-disk GB summed across running + stopped remote machines"
    )

    def to_plan_row(self) -> dict[str, float]:
        """The connector-table column values for this plan (storage converted to bytes)."""
        return {
            "max_remote_workspaces": int(self.max_remote_workspaces),
            "max_total_workspaces": int(self.max_total_workspaces),
            "max_buckets": int(self.max_buckets),
            "max_total_bucket_bytes": int(self.max_total_bucket_gb) * 1024**3,
            "monthly_llm_spend_usd": float(self.monthly_llm_spend_usd),
            "max_active_synced_workspaces": int(self.max_active_synced_workspaces),
            "max_active_machine_units": int(self.max_active_machine_units),
            "max_total_machine_disk_gb": int(self.max_total_machine_disk_gb),
        }


class WebWorkspacesConfig(FrozenModel):
    """The ``[web_workspaces]`` block of a ``deploy.toml`` -- the tier's pinned web-create template.

    Read by ``minds-admin env deploy`` and pushed into the connector's per-deploy
    Modal Secret as ``MINDS_WEB_TEMPLATE_*`` / ``MINDS_WEB_SHAPE_*`` env vars.
    The connector's ``POST /hosts/claim`` (browser-driven workspace creation)
    leases only pool hosts whose baked attributes match this pin exactly.
    Tiers without the block have web workspace creation disabled.

    There is deliberately no ``template_ref`` field: a committed ref pin
    silently goes stale the moment the pool is re-baked at a newer version
    (a dev tier shipped exactly that bug). Shared tiers (staging /
    production) always track the app's pinned release tag
    (``FALLBACK_BRANCH``) -- the same tag the pool is re-baked from -- and
    dev-tier deploys must state the ref explicitly via the
    ``MINDS_WEB_TEMPLATE_REF`` env var (which also overrides the default on
    every other tier). ``MINDS_WEB_TEMPLATE_REPO`` likewise overrides
    ``template_repo`` at deploy time.
    """

    template_repo: NonEmptyStr | None = Field(
        default=None,
        description=(
            "Canonical repo key the pool bake stamps into row attributes "
            "(``host/org/repo``, e.g. ``github.com/imbue-ai/default-workspace-template``). "
            "Unset resolves to the canonical default-workspace-template key."
        ),
    )
    cpus: NonNegativeInt | None = Field(
        default=None,
        description="Blessed vCPU count for web creates; unset leaves the lease unconstrained on cpus.",
    )
    memory_gb: NonNegativeInt | None = Field(
        default=None,
        description="Blessed memory (GB) for web creates; unset leaves the lease unconstrained on memory.",
    )
    gpu_count: NonNegativeInt | None = Field(
        default=None,
        description="Blessed GPU count for web creates; unset leaves the lease unconstrained on GPUs.",
    )


class OriginsConfig(FrozenModel):
    """The ``[origins]`` block of a ``deploy.toml`` -- the tier's user-facing custom domains.

    Declares the browser-facing origin layout the sharing redesign specifies:
    the hosted accounts surface (``accounts_origin``) and the web chrome
    (``chrome_origin``) are Modal custom domains on the connector app, and one
    SuperTokens browser session crosses the two hosts via a cookie scoped to
    ``cookie_domain`` (their shared registrable apex). The apex must be
    first-party only -- untrusted workspace content lives on the tier's
    separate content domain -- and each tier uses its own apex so sessions can
    never cross tiers. Tiers without the block (dev/ci) stay on the bare
    connector URL with host-only cookies.
    """

    accounts_origin: AnyUrl = Field(
        description=(
            "Origin of the hosted accounts surface (sign-in/sign-up, the share broker), "
            "e.g. ``https://accounts.imbue.com``. Drives AUTH_WEBSITE_DOMAIN and "
            "ACCOUNTS_BASE_URL at deploy time, and is attached to the connector as a "
            "Modal custom domain."
        ),
    )
    chrome_origin: AnyUrl = Field(
        description=(
            "Origin of the hosted web chrome (served at ``/web``), e.g. "
            "``https://minds.imbue.com``. Drives SHARE_CHROME_ORIGIN at deploy time, and "
            "is attached to the connector as a Modal custom domain."
        ),
    )
    cookie_domain: NonEmptyStr = Field(
        description=(
            "Registrable apex both origins live under (e.g. ``imbue.com``); the accounts "
            "session cookie is scoped to it so the session crosses the two hosts."
        ),
    )

    @model_validator(mode="after")
    def _check_origins_are_https_hosts_under_the_cookie_domain(self) -> "OriginsConfig":
        for label, origin in (("accounts_origin", self.accounts_origin), ("chrome_origin", self.chrome_origin)):
            if origin.scheme != "https":
                raise OriginsConfigError(f"[origins] {label} must be https, got {origin}")
            if origin.path not in (None, "", "/") or origin.query is not None or origin.fragment is not None:
                raise OriginsConfigError(
                    f"[origins] {label} must be a bare origin (no path/query/fragment), got {origin}"
                )
            host = origin.host or ""
            if not host.endswith("." + str(self.cookie_domain)):
                raise OriginsConfigError(
                    f"[origins] {label} host {host!r} is not a subdomain of cookie_domain {self.cookie_domain!r}"
                )
        return self


# The gen-2 management overlay addressing plan (specs/slice-fleet-gen2 and the
# slice-fleet canary follow-ups): every tier is its own isolated WireGuard
# network, and all tiers' overlays are carved DISJOINT from one reserved
# supernet -- identical numbers across tiers would be ambiguous on an operator
# machine holding several tiers' tunnels at once, even though the tiers never
# meet on the wire. The supernet deliberately avoids the Tailscale/CGNAT range
# (100.64.0.0/10 -- in use on operator machines), the box-local 10.201.0.0/16
# per-slice /30 range, Docker's 172.17+ pools, GCP default-VPC space
# (10.128.0.0/9), and the low 10.0.x/10.8.x home-router/OpenVPN defaults;
# overlapping Kubernetes' default ranges is accepted (not in use here).
MANAGEMENT_OVERLAY_SUPERNET_CIDR: Final[str] = "10.64.0.0/10"

# Per-tier overlay allocations. Production takes the /11 (the only tier whose
# box count can plausibly grow without bound); each small tier sits at the
# BASE of its own reserved /13 so it can later widen 8x by changing this one
# value -- a superset widening is a pure config-converge (re-prep / peer
# sync), no box ever changes address. 10.120.0.0/13 stays spare for a future
# tier. Keys are tier names as defined by mngr_imbue_cloud's
# ``tier_for_env_name``. Within each allocation, operators are hand-assigned
# inside the first /24 in the ``[management_plane]`` table of the tier's
# committed ``deploy.toml``;
# boxes are assigned sequentially above it at prep, so the two never collide.
MANAGEMENT_OVERLAY_CIDR_BY_TIER: Final[Mapping[str, str]] = {
    "production": "10.64.0.0/11",
    "staging": "10.96.0.0/16",
    "ci": "10.104.0.0/16",
    "dev": "10.112.0.0/16",
}


class ManagementOverlayAllocation(FrozenModel):
    """One tier's management-overlay address allocation (its own isolated WireGuard network)."""

    tier: NonEmptyStr = Field(description="The tier this allocation belongs to.")
    overlay: IPv4Network = Field(description="The tier's overlay network (operators + boxes).")

    @computed_field
    @cached_property
    def operator_block(self) -> IPv4Network:
        # The reserved operator /24 at the base of the overlay; boxes are
        # assigned above it at prep, so the two can never collide.
        return next(iter(self.overlay.subnets(new_prefix=24)))


def management_overlay_for_tier(tier: str) -> ManagementOverlayAllocation:
    """The tier's overlay allocation. Raises ManagementPlaneConfigError for an unknown tier."""
    cidr = MANAGEMENT_OVERLAY_CIDR_BY_TIER.get(tier)
    if cidr is None:
        known = sorted(MANAGEMENT_OVERLAY_CIDR_BY_TIER)
        raise ManagementPlaneConfigError(f"no management overlay is allocated for tier {tier!r} (have: {known})")
    overlay = IPv4Network(cidr)
    if not overlay.subnet_of(IPv4Network(MANAGEMENT_OVERLAY_SUPERNET_CIDR)):
        raise ManagementPlaneConfigError(
            f"tier {tier!r} overlay {cidr} lies outside the reserved supernet {MANAGEMENT_OVERLAY_SUPERNET_CIDR}"
        )
    return ManagementOverlayAllocation(tier=NonEmptyStr(tier), overlay=overlay)


# WireGuard keys are base64; anything outside this charset (in particular
# whitespace and newlines) cannot be a key and would corrupt the rendered
# wg0.conf, which is written inside a root-executed prep heredoc.
_WIREGUARD_PUBLIC_KEY_CHARSET_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9+/=]+$")


class WireguardOperatorConfig(FrozenModel):
    """One operator peer on a tier's gen-2 management WireGuard overlay.

    All values are public (the private key stays on the operator's machine),
    so the tier's committed ``deploy.toml`` can carry them and peer changes
    are ordinary PRs.
    """

    name: NonEmptyStr = Field(description="Human label for the operator (used in rendered config comments).")
    public_key: NonEmptyStr = Field(description="The operator's WireGuard public key (base64).")
    address: IPv4Address = Field(
        description=(
            "The operator's assigned overlay address: a /32 inside the operator block (first /24) of the "
            "operator's tier overlay. Membership is validated at load time, where the tier is known."
        )
    )

    @model_validator(mode="after")
    def _check_values_render_as_single_config_lines(self) -> "WireguardOperatorConfig":
        # Both values are rendered verbatim into the box's wg0.conf inside a
        # root-executed prep heredoc (and into operator client configs), so a
        # value spanning lines could corrupt -- or inject lines into -- the
        # rendered script. Reject at parse time.
        if "\n" in self.name or "\r" in self.name:
            raise ManagementPlaneConfigError(f"operator name {str(self.name)!r} must be a single line")
        if not _WIREGUARD_PUBLIC_KEY_CHARSET_PATTERN.match(self.public_key):
            raise ManagementPlaneConfigError(
                f"operator '{self.name}' public_key {str(self.public_key)!r} must contain only "
                "base64 characters (a WireGuard public key)"
            )
        return self


class ManagementWireguardConfig(FrozenModel):
    """The ``[management_plane.wireguard]`` table of a tier's ``deploy.toml``: the operator peer list.

    Gen-2 box prep renders these peers into the box's ``wg0`` config, and the
    ``minds-admin wireguard sync-peers`` command pushes changes to the live fleet.
    """

    listen_port: PositiveInt = Field(
        default=PositiveInt(51820),
        description="UDP port each gen-2 box's WireGuard endpoint listens on.",
    )
    operators: tuple[WireguardOperatorConfig, ...] = Field(
        default=(),
        description="The tier's operator peers. Empty means no operator currently has overlay access.",
    )

    @model_validator(mode="after")
    def _check_listen_port_is_a_valid_udp_port(self) -> "ManagementWireguardConfig":
        # The port is rendered into every box's wg0.conf and every operator
        # client config's Endpoint; an out-of-range value would only surface
        # on-box when wg-quick rejects the rendered file, so reject it at
        # parse time like every other rendered value in this config.
        if int(self.listen_port) > 65535:
            raise ManagementPlaneConfigError(
                f"[management_plane.wireguard] listen_port {int(self.listen_port)} is not a valid UDP port (must be <= 65535)"
            )
        return self

    @model_validator(mode="after")
    def _check_operator_identities_are_unique(self) -> "ManagementWireguardConfig":
        # Names too: `minds-admin wireguard config --operator <name>` selects by name,
        # so a duplicate would silently resolve to whichever entry comes first.
        names = [str(operator.name) for operator in self.operators]
        addresses = [str(operator.address) for operator in self.operators]
        public_keys = [str(operator.public_key) for operator in self.operators]
        if len(set(names)) != len(names):
            raise ManagementPlaneConfigError("[management_plane.wireguard] operator names must be unique")
        if len(set(addresses)) != len(addresses):
            raise ManagementPlaneConfigError("[management_plane.wireguard] operator addresses must be unique")
        if len(set(public_keys)) != len(public_keys):
            raise ManagementPlaneConfigError("[management_plane.wireguard] operator public keys must be unique")
        return self


class ManagementModalProxyConfig(FrozenModel):
    """The ``[management_plane.modal_proxy]`` table of a tier's ``deploy.toml``.

    Names the tier's Modal Proxy (attached to the connector's functions at
    deploy so all connector egress leaves from its static IPs) and lists those
    static IPs (allowlisted on gen-2 box ``:22`` by the prep-installed
    management nftables policy). Both halves must be kept in step with the
    proxy configured in the tier's Modal workspace.
    """

    proxy_name: NonEmptyStr = Field(
        description="Name of the Modal Proxy in the tier's Modal workspace (dashboard -> Proxies)."
    )
    environment_name: NonEmptyStr | None = Field(
        default=None,
        description=(
            "The Modal environment the proxy lives in. Proxy lookup is environment-scoped and a "
            "workspace holds at most one proxy, so a tier whose apps deploy into per-env Modal "
            "environments (dev) keeps its shared proxy in one environment (typically 'main') and "
            "names it here; None resolves the proxy in each deploy's own environment."
        ),
    )
    static_ips: tuple[IPv4Address, ...] = Field(
        description="The proxy's static egress IPs, allowlisted for box management SSH."
    )

    @model_validator(mode="after")
    def _check_static_ips_present(self) -> "ManagementModalProxyConfig":
        # An empty allowlist with a named proxy would lock the connector out of
        # every gen-2 box the moment prep applies the policy.
        if not self.static_ips:
            raise ManagementPlaneConfigError(
                "[management_plane.modal_proxy] static_ips must not be empty when a proxy is named"
            )
        return self


class ManagementPlaneConfig(FrozenModel):
    """The optional ``[management_plane]`` table of a tier's ``deploy.toml``: its gen-2 management plane.

    All values are public (operator WireGuard public keys, the tier's Modal
    Proxy name and its static egress IPs), so changes are ordinary
    PR-reviewed edits like the rest of the file. The box
    ``:22`` lockdown is applied by gen-2 prep only when ``modal_proxy`` is
    configured -- without it the connector would have no allowlisted egress
    address and every workspace on the box would break.
    """

    wireguard: ManagementWireguardConfig = Field(
        default_factory=ManagementWireguardConfig,
        description="Operator WireGuard peers for the tier's management overlay.",
    )
    modal_proxy: ManagementModalProxyConfig | None = Field(
        default=None,
        description=(
            "The tier's Modal Proxy (connector egress + box :22 allowlist). None means the tier "
            "has no proxy yet: the connector egresses from dynamic IPs and gen-2 prep installs "
            "no :22 lockdown."
        ),
    )


class SshCaConfig(FrozenModel):
    """The ``[ssh_ca]`` block of a tier's ``deploy.toml``: the public half of the tier's SSH CA in Vault.

    Management SSH into gen-2 boxes, slice VMs, and workspace containers is by
    short-lived certificate signed by the tier's Vault SSH CA
    (imbue-ai/mngr-internal#850). The public key is committed so box prep, the
    slice bake, and the cutover render CA trust without a Vault read, and so
    the deploy can check Vault still holds the CA the fleet trusts.
    """

    public_key: NonEmptyStr = Field(
        description="The tier CA's OpenSSH public key line (``vault read -field=public_key minds-<tier>-ssh/config/ca``)."
    )

    @model_validator(mode="after")
    def _validate_public_key_shape(self) -> "SshCaConfig":
        fields = str(self.public_key).split()
        if len(fields) < 2 or not fields[0].startswith("ssh-"):
            raise SshCaConfigError(
                f"ssh_ca.public_key must be an OpenSSH public key line ('ssh-ed25519 AAAA... comment'), "
                f"got {str(self.public_key)[:40]!r}"
            )
        return self


class DeployEnvConfig(FrozenModel):
    """Per-tier deploy-time config read by deploy scripts and `minds-admin env create`.

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
    analytics: AnalyticsDeployConfig = Field(
        default_factory=AnalyticsDeployConfig,
        description=(
            "Whether this tier deploys the analytics app. Off by default (and in every "
            "committed deploy.toml) until the tier's analytics bringup has run; dynamic dev "
            "envs override via the sticky --with-analytics deploy flag."
        ),
    )
    paid: PaidDefaultsConfig = Field(
        default_factory=PaidDefaultsConfig,
        description=(
            "Default paid-access entries seeded (seed-if-absent) into the connector's "
            "paid_domains / paid_emails tables after migrations on each deploy."
        ),
    )
    plans: dict[str, PlanQuotasConfig] = Field(
        default_factory=dict,
        description=(
            "Plan definitions (plan name -> quota entitlements) written -- overwriting -- into the "
            "connector's plans table after migrations on every deploy. Git is the source of truth "
            "for plan defaults; per-user entitlement rows are managed via the admin API instead."
        ),
    )
    web_workspaces: WebWorkspacesConfig | None = Field(
        default=None,
        description=(
            "Pinned template + blessed compute shape for browser-driven workspace creation "
            "(the connector's POST /hosts/claim). None (the default) disables web creates on the tier."
        ),
    )
    storage: StorageDeployConfig | None = Field(
        default=None,
        description=(
            "Git-owned workspace-storage knobs stamped over the Vault ``storage`` entry at "
            "deploy time. None (the default) leaves the Vault values untouched."
        ),
    )
    origins: OriginsConfig | None = Field(
        default=None,
        description=(
            "User-facing custom-domain origin layout (accounts surface + web chrome + shared "
            "cookie apex). None (the default) keeps the tier on the bare connector URL with "
            "host-only cookies (dev/ci)."
        ),
    )
    ssh_ca: SshCaConfig | None = Field(
        default=None,
        description=(
            "The tier's SSH CA public key (its Vault ``minds-<tier>-ssh`` mount's CA). None means the tier's CA has "
            "not been brought up yet: gen-2 box prep and slice bakes refuse, since a gen-2 host has no other "
            "management-access path (see apps/minds/docs/deploy/setup/tier-bringup.md)."
        ),
    )
    management_plane: ManagementPlaneConfig | None = Field(
        default=None,
        description=(
            "The tier's gen-2 management plane: operator WireGuard peers and the Modal Proxy whose static "
            "egress IPs gen-2 box prep allowlists on ``:22``. None means the tier has none yet -- no operator "
            "peers, no proxy, no box lockdown -- exactly like an absent ``ssh_ca`` (see "
            "apps/minds/docs/deploy/gen2-management-plane.md)."
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
