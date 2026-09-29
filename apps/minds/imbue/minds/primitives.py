import os
import re
from enum import auto
from typing import Final
from typing import Self

from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.errors import ContentDomainError

# Canonical set of AWS regions the minds app offers for ``LaunchMode.AWS``.
# This is the single source of truth used both to write one
# ``[providers.aws-<region>]`` block per region into the mngr profile settings
# at startup (``imbue.minds.bootstrap``) and to populate the create form's AWS
# region dropdown (``imbue.minds.desktop_client.region_preference``). minds
# deliberately exposes only the US datacenters by default: every configured
# region adds a provider that ``mngr list`` fans out to on each discovery
# cycle, and the non-US regions roughly doubled listing latency for little
# benefit to the current user base. ``mngr_aws`` resolves the newest Debian
# AMI in whichever region a provider is configured for at create time, so this
# set can be widened without any ``mngr_aws`` change. Lives in ``primitives``
# (which never imports ``mngr``) so the early ``bootstrap`` module can read it
# without violating its no-mngr-on-import contract.
CONFIGURED_AWS_REGIONS: Final[tuple[str, ...]] = (
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
# Several families on purpose: new pay-as-you-go subscriptions are barred
# (SkuNotAvailable) from the cheap burstable B-series and the v3/v5 D-series in
# popular regions, even with unused vCPU quota; the v6 D-series (Dsv6 / Dadsv6)
# is what a fresh subscription is actually allowed to launch there, so it is the
# recommended default and the older families stay as cheaper options for
# established subscriptions.
CONFIGURED_AZURE_VM_SIZES: Final[tuple[tuple[str, str], ...]] = (
    ("Standard_B2s", "Standard_B2s — 2 vCPU / 4 GB (cheapest; often barred on new subscriptions)"),
    ("Standard_B2ms", "Standard_B2ms — 2 vCPU / 8 GB (burstable; often barred on new subscriptions)"),
    ("Standard_D2s_v6", "Standard_D2s_v6 — 2 vCPU / 8 GB (recommended)"),
    ("Standard_D2ads_v6", "Standard_D2ads_v6 — 2 vCPU / 8 GB (AMD, with local disk)"),
    (
        "Standard_D2s_v5",
        "Standard_D2s_v5 — 2 vCPU / 8 GB (Intel, previous generation; often barred on new subscriptions)",
    ),
    ("Standard_D4s_v6", "Standard_D4s_v6 — 4 vCPU / 16 GB"),
    ("Standard_D4ads_v6", "Standard_D4ads_v6 — 4 vCPU / 16 GB (AMD, with local disk)"),
    ("Standard_D8s_v6", "Standard_D8s_v6 — 8 vCPU / 32 GB"),
)
DEFAULT_AZURE_VM_SIZE: Final[str] = "Standard_D2s_v6"

# Curated placement choices for bring-your-own-key GCP / Azure accounts (GCE is
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


class DeviceId(RandomId):
    """Stable identity of one minds installation (one data directory on one device).

    Stamped on locally-hosted workspace records (``hosting_device_id``) so the
    sync reconcile can recognize this install's own rows. Values keep the legacy
    ``host-<32hex>`` shape -- early installs adopted the mngr local provider's
    host id as their device identity -- but a device id is NOT an mngr host id
    and must never be typed as one.
    """

    PREFIX = "host"


class CreateAttemptId(RandomId):
    """Internal handle, used only inside Imbue Studio, for an in-flight ``mngr create`` invocation.

    Returned by ``AgentCreator.create_agent_async`` so the desktop client
    UI has something to poll status / stream logs against immediately --
    *before* the inner ``mngr create`` returns and we know the canonical
    ``AgentId`` (the agent id is generated by mngr, not minds, since
    imbue_cloud lease-adoption forces it to the pool host's pre-baked id
    and pre-generating one minds-side led to confusion + bugs).

    Distinct ``"create-attempt-"`` prefix so it can never accidentally be
    typed-checked or string-compared against an ``AgentId``.
    """

    PREFIX = "create-attempt"


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
    # GCP / Azure are reachable ONLY through a bring-your-own-key cloud account
    # (``byok-gcp-<slug>`` / ``byok-azure-<slug>`` provider blocks written by the
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
    their own runtime. The create form defaults this to runc everywhere (see
    :func:`default_docker_runtime`) and lets the user opt into runsc under
    advanced settings.
    """

    RUNC = auto()
    RUNSC = auto()


# Env override for the create-form / create-API default runtime, consulted by
# ``default_docker_runtime``. Set it to a ``DockerRuntime`` value
# (case-insensitive) to force the default. This is the layer that decides
# whether the create stacks the ``docker_runsc`` template at all -- distinct
# from ``MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME``, which only overrides the
# mngr provider config and cannot override a template that was explicitly
# stacked.
_DEFAULT_DOCKER_RUNTIME_ENV_VAR: Final[str] = "MINDS_DOCKER_RUNTIME_DEFAULT"


def default_docker_runtime() -> DockerRuntime:
    """Return the default Docker container runtime for the create form / API.

    An explicit ``MINDS_DOCKER_RUNTIME_DEFAULT`` env override wins when set.
    Otherwise runc, on every platform: macOS has no gVisor at all, and a Linux
    daemon only has runsc registered when the machine's owner installed it, so
    it cannot be assumed on a fresh install. gVisor stays a per-create opt-in
    under the form's advanced settings.

    Raises ``ValueError`` if the override is set to a value that is not a
    ``DockerRuntime`` -- a misconfigured knob should fail loud, not silently
    fall back.
    """
    override = os.environ.get(_DEFAULT_DOCKER_RUNTIME_ENV_VAR)
    if override:
        return DockerRuntime(override.strip().upper())
    return DockerRuntime.RUNC


class BackupProvider(UpperCaseStrEnum):
    """How the workspace agent's restic backups are configured.

    Decoupled from both the compute and AI providers so any combination is
    valid. Backup setup runs asynchronously after the host is created; the
    same code path can be re-applied to an existing host later.

    - ``IMBUE_CLOUD`` -- create a per-workspace R2 bucket (named after the
      host id) + a scoped key against the selected account, then inject a
      ``data/.secrets/restic.env`` pointing restic at that bucket.
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


# One DNS label as the connector's share coordinates accept it: lowercase
# alphanumerics with single interior hyphens, at most 63 characters.
_DNS_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^(?=.{1,63}$)[a-z0-9]+(?:-[a-z0-9]+)*$")


class ContentDomain(NonEmptyStr):
    """The apex shared-workspace hostnames live under (e.g. ``personal-imbue.com``): dot-joined lowercase DNS labels."""

    def __new__(cls, value: str) -> Self:
        labels = value.split(".")
        if not all(_DNS_LABEL_RE.match(label) is not None for label in labels):
            raise ContentDomainError(
                f"content_domain must be dot-joined lowercase DNS labels (letters, digits, interior hyphens; "
                f"at most 63 characters each), got {value!r}"
            )
        return super().__new__(cls, value)
