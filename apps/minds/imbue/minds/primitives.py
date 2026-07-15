import os
import platform
from enum import auto
from typing import Final

from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr

# Canonical set of AWS regions the minds app offers for ``LaunchMode.AWS``.
# This is the single source of truth used both to write one
# ``[providers.aws-<region>]`` block per region into the mngr profile settings
# at startup (``imbue.minds.bootstrap``) and to populate the create form's AWS
# region dropdown (``imbue.minds.desktop_client.region_preference``). minds
# deliberately exposes only the US datacenters by default: every configured
# region adds a provider that ``mngr list`` fans out to on each discovery
# cycle, and the non-US regions roughly doubled listing latency for little
# benefit to the current user base. ``mngr_aws`` still ships pinned default
# AMIs for more regions, so this set can be widened later without other
# changes; any region added here must have an AMI in ``mngr_aws`` or it would
# fail AMI resolution at create time. Lives in ``primitives`` (which never
# imports ``mngr``) so the early ``bootstrap`` module can read it without
# violating its no-mngr-on-import contract.
CONFIGURED_AWS_REGIONS: Final[tuple[str, ...]] = (
    # Exactly the regions with a pinned AMI in mngr_aws's DEFAULT_AMI_BY_REGION
    # -- the one hard constraint on this list (a region without an AMI fails at
    # create). Widen the AMI table first to widen this.
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-northeast-1",
)

# Hardcoded fallback AWS region for the create form when there is no stored
# last-used value and IP geolocation has not (yet) resolved. Must be a member
# of ``CONFIGURED_AWS_REGIONS``.
DEFAULT_AWS_REGION: Final[str] = "us-east-1"

# EC2 instance types the create form offers, (value, label) pairs. Floor is
# 4 GB: the forever-claude-template build (uv sync + npm ci/build) is
# documented to OOM/thrash on 2 GB (see ``_AWS_DEFAULT_INSTANCE_TYPE`` in
# ``bootstrap``), so nothing smaller is offered. t3.large (8 GB) is the
# known-good default. Values feed the ``--aws-instance-type=`` build arg.
CONFIGURED_AWS_INSTANCE_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("t3.medium", "t3.medium — 2 vCPU / 4 GB (cheapest; heavy builds may be slow)"),
    ("t3.large", "t3.large — 2 vCPU / 8 GB (recommended)"),
    ("t3a.large", "t3a.large — 2 vCPU / 8 GB (AMD; slightly cheaper)"),
    ("m6i.large", "m6i.large — 2 vCPU / 8 GB (non-burstable)"),
    ("t3.xlarge", "t3.xlarge — 4 vCPU / 16 GB"),
    ("m6i.xlarge", "m6i.xlarge — 4 vCPU / 16 GB (non-burstable)"),
    ("t3.2xlarge", "t3.2xlarge — 8 vCPU / 32 GB"),
)
DEFAULT_AWS_INSTANCE_TYPE: Final[str] = "t3.large"

# GCP / Azure analogs of the AWS machine-size list (same 4 GB floor, same
# 8 GB recommended default). Values feed ``--gcp-machine-type=`` /
# ``--azure-vm-size=`` build args.
CONFIGURED_GCP_MACHINE_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("e2-medium", "e2-medium — 2 vCPU / 4 GB (cheapest; heavy builds may be slow)"),
    ("e2-standard-2", "e2-standard-2 — 2 vCPU / 8 GB (recommended)"),
    ("n2-standard-2", "n2-standard-2 — 2 vCPU / 8 GB (non-shared-core pool)"),
    ("e2-standard-4", "e2-standard-4 — 4 vCPU / 16 GB"),
    ("n2-standard-4", "n2-standard-4 — 4 vCPU / 16 GB (non-shared-core pool)"),
    ("e2-standard-8", "e2-standard-8 — 8 vCPU / 32 GB"),
)
DEFAULT_GCP_MACHINE_TYPE: Final[str] = "e2-standard-2"
# Two families on purpose: new pay-as-you-go subscriptions frequently hit
# SkuNotAvailable capacity restrictions on the cheap burstable B-series in
# popular regions; the Dsv5/Dasv5 families draw from different hardware pools
# and often have capacity where B-series is gated.
CONFIGURED_AZURE_VM_SIZES: Final[tuple[tuple[str, str], ...]] = (
    ("Standard_B2s", "Standard_B2s — 2 vCPU / 4 GB (cheapest; heavy builds may be slow)"),
    ("Standard_B2ms", "Standard_B2ms — 2 vCPU / 8 GB (recommended)"),
    ("Standard_D2as_v5", "Standard_D2as_v5 — 2 vCPU / 8 GB (AMD; try if B-series is unavailable)"),
    ("Standard_D2s_v5", "Standard_D2s_v5 — 2 vCPU / 8 GB (Intel; try if B-series is unavailable)"),
    ("Standard_B4ms", "Standard_B4ms — 4 vCPU / 16 GB"),
    ("Standard_D4as_v5", "Standard_D4as_v5 — 4 vCPU / 16 GB (AMD)"),
    ("Standard_B8ms", "Standard_B8ms — 8 vCPU / 32 GB"),
)
DEFAULT_AZURE_VM_SIZE: Final[str] = "Standard_B2ms"

# Curated placement choices for bring-your-own GCP / Azure accounts (GCE is
# zonal, so GCP offers zones; Azure offers regions). Small US-centric lists,
# mirroring CONFIGURED_AWS_REGIONS; the account's pinned default comes first
# in the create form via the option's data-default-region.
CONFIGURED_GCP_ZONES: Final[tuple[str, ...]] = (
    "us-west1-a",
    "us-central1-a",
    "us-east1-b",
    "us-east4-a",
    "europe-west1-b",
    "europe-west4-a",
    "asia-southeast1-a",
    "asia-northeast1-a",
    "australia-southeast1-a",
)
DEFAULT_GCP_ZONE: Final[str] = "us-west1-a"
# eastus2 first: new-subscription capacity restrictions bite hardest in the
# oldest/most popular regions (westus, eastus); eastus2 / centralus /
# northcentralus / westus3 are the commonly-recommended less-congested US picks,
# and less-used non-US regions are often the easiest of all for new subs.
# Offered only in the add-account form: an Azure account entry is pinned to one
# region for life (its resource group / vnet live there); add another entry for
# another region.
CONFIGURED_AZURE_REGIONS: Final[tuple[str, ...]] = (
    "eastus2",
    "centralus",
    "northcentralus",
    "westus2",
    "westus3",
    "westus",
    "eastus",
    "canadacentral",
    "northeurope",
    "westeurope",
    "uksouth",
    "swedencentral",
    "australiaeast",
    "southeastasia",
    "japaneast",
    "koreacentral",
    "centralindia",
)
DEFAULT_AZURE_REGION: Final[str] = "eastus2"


class CreationId(RandomId):
    """Minds-internal handle for an in-flight ``mngr create`` invocation.

    Returned by ``AgentCreator.create_agent_async`` so the desktop client
    UI has something to poll status / stream logs against immediately --
    *before* the inner ``mngr create`` returns and we know the canonical
    ``AgentId`` (the agent id is generated by mngr, not minds, since
    imbue_cloud lease-adoption forces it to the pool host's pre-baked id
    and pre-generating one minds-side led to confusion + bugs).

    Distinct ``"creation-"`` prefix so it can never accidentally be
    typed-checked or string-compared against an ``AgentId``.
    """

    PREFIX = "creation"


class OutputFormat(UpperCaseStrEnum):
    """Output format for command results on stdout."""

    HUMAN = auto()
    JSON = auto()
    JSONL = auto()


class LaunchMode(UpperCaseStrEnum):
    """How a workspace agent should be launched."""

    DOCKER = auto()
    VULTR = auto()
    LIMA = auto()
    IMBUE_CLOUD = auto()
    AWS = auto()
    # Runs the agent in a Modal sandbox using the local machine's own Modal token
    # (``modal token new``) -- resolves the ``modal`` provider instance. Modal
    # sandboxes are ephemeral (~1 day max), so it is surfaced as "Modal (1-day
    # ephemeral)" and is testing-only.
    MODAL = auto()
    # GCP / Azure are reachable ONLY through a bring-your-own cloud account
    # (``byo-gcp-<slug>`` / ``byo-azure-<slug>`` provider blocks written by the
    # accounts modal); the create form does not render them as ambient options,
    # so no ambient region tables / provider blocks exist for them.
    GCP = auto()
    AZURE = auto()


class DockerRuntime(UpperCaseStrEnum):
    """Container runtime for the local Docker compute provider (``LaunchMode.DOCKER``).

    - ``RUNC`` -- Docker's default runtime. Works everywhere, including macOS.
    - ``RUNSC`` -- gVisor, which intercepts the container's syscalls to shrink
      the host kernel attack surface for untrusted agents. Requires ``runsc`` to
      be installed and registered with the local Docker daemon (Linux in
      practice); it is unavailable on macOS.

    Only meaningful for the Docker compute provider; the other launch modes pin
    their own runtime. The create form defaults this to the platform-appropriate
    value (see :func:`default_docker_runtime`) and lets the user override it
    under advanced settings.
    """

    RUNC = auto()
    RUNSC = auto()


# Env override for the create-form / create-API default runtime, consulted by
# ``default_docker_runtime``. Set it to a ``DockerRuntime`` value
# (case-insensitive) to force the default. CI and the e2e snapshot build set it
# to ``RUNC`` because their Docker daemon has no gVisor (runsc) registered; a
# real deployment leaves it unset, so Linux still defaults to the hardened
# runsc. This is the layer that decides whether the create stacks the
# ``docker_runsc`` template at all -- distinct from
# ``MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME``, which only overrides the mngr
# provider config and cannot override a template that was explicitly stacked.
_DEFAULT_DOCKER_RUNTIME_ENV_VAR: Final[str] = "MINDS_DOCKER_RUNTIME_DEFAULT"


def default_docker_runtime() -> DockerRuntime:
    """Return the default Docker container runtime for the create form / API.

    An explicit ``MINDS_DOCKER_RUNTIME_DEFAULT`` env override wins when set
    (CI uses it to pin runc, having no gVisor). Otherwise the platform default:
    macOS has no gVisor so it must use runc; Linux defaults to the
    gVisor-hardened runsc (which the minds app assumes is installed there).

    Raises ``ValueError`` if the override is set to a value that is not a
    ``DockerRuntime`` -- a misconfigured knob should fail loud, not silently
    fall back.
    """
    override = os.environ.get(_DEFAULT_DOCKER_RUNTIME_ENV_VAR)
    if override:
        return DockerRuntime(override.strip().upper())
    return DockerRuntime.RUNC if platform.system() == "Darwin" else DockerRuntime.RUNSC


class AIProvider(UpperCaseStrEnum):
    """How the workspace agent should obtain its Anthropic credentials.

    Decoupled from the compute provider so any combination is valid: e.g.
    a user can run on a local container while still using an imbue_cloud-
    minted LiteLLM key for inference.

    - ``IMBUE_CLOUD`` -- mint a LiteLLM virtual key against the selected
      account and inject it as ``ANTHROPIC_API_KEY`` (with the matching
      ``ANTHROPIC_BASE_URL``). Requires a selected account.
    - ``API_KEY`` -- use a user-supplied Anthropic API key directly; no
      ``ANTHROPIC_BASE_URL`` is set so the agent talks to the official
      Anthropic API.
    - ``SUBSCRIPTION`` -- inject neither key nor base URL; the user will
      sign in to Claude interactively once the workspace starts.
    """

    IMBUE_CLOUD = auto()
    API_KEY = auto()
    SUBSCRIPTION = auto()


class BackupProvider(UpperCaseStrEnum):
    """How the workspace agent's restic backups are configured.

    Decoupled from both the compute and AI providers so any combination is
    valid. Backup setup runs asynchronously after the host is created; the
    same code path can be re-applied to an existing host later.

    - ``IMBUE_CLOUD`` -- create a per-workspace R2 bucket (named after the
      host id) + a scoped key against the selected account, then inject a
      ``runtime/secrets/restic.env`` pointing restic at that bucket.
      Requires a selected account.
    - ``API_KEY`` -- inject a user-supplied ``KEY=VALUE`` block verbatim
      into ``restic.env``; the user owns ``RESTIC_REPOSITORY`` and any
      backend credentials.
    - ``CONFIGURE_LATER`` -- inject nothing now. Backups stay dormant until
      the same provisioning path is invoked against the host later.
    """

    IMBUE_CLOUD = auto()
    API_KEY = auto()
    CONFIGURE_LATER = auto()


class OneTimeCode(NonEmptyStr):
    """A single-use authentication code for workspace access."""

    ...


class CookieSigningKey(SecretStr):
    """Secret key used for signing authentication cookies."""

    ...


class ServiceName(NonEmptyStr):
    """Name of a service run by an agent (e.g. 'web', 'api')."""

    ...


class GitUrl(NonEmptyStr):
    """A git URL to clone (local path, file://, https://, or ssh)."""

    ...


class GitBranch(NonEmptyStr):
    """A git branch name to clone."""

    ...


class GitCommitHash(NonEmptyStr):
    """A full git commit hash (40 hex characters)."""

    ...
