from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_imbue_cloud.errors import InvalidBuildArgError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import DEFAULT_FAST_MODE
from imbue.mngr_imbue_cloud.primitives import FastMode
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import KNOWN_OVH_US_REGIONS
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.primitives import R2AccessKeyId
from imbue.mngr_imbue_cloud.primitives import R2BucketAccess
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId


class SliceTeardownTarget(FrozenModel):
    """A slice pool host to tear down: its lima resources and the box that hosts them."""

    pool_host_row_id: str = Field(description="The pool_hosts row id (deleted after the VM is torn down)")
    lima_instance_name: str = Field(description="The slice's lima instance name on the box")
    lima_disk_name: str | None = Field(default=None, description="The slice's lima data-disk name, if recorded")
    box_public_address: str = Field(description="SSH-reachable address of the bare-metal box hosting the slice")
    lima_service_user: str = Field(description="The box's non-root lima user that owns the VMs")
    box_host_public_key: str | None = Field(
        default=None, description="The box's sshd host public key, pinned for the teardown SSH"
    )


class PaidListEntry(FrozenModel):
    """One row of a connector paid-list table (a domain or an email).

    ``value`` holds the domain (e.g. ``imbue.com``) or full email; the
    connector normalizes it to lowercase on write. Rows are never hard
    deleted -- ``is_paid`` flips to False on removal and ``updated_at``
    records when that happened.
    """

    value: str = Field(description="The allowed domain or email (lowercased)")
    is_paid: bool = Field(description="Whether this entry currently grants paid access")
    created_at: str = Field(description="When the row was first inserted")
    updated_at: str = Field(description="When is_paid was last changed")


class LeaseAttributes(FrozenModel):
    """Attributes describing what kind of pool host a request needs.

    Sent in the body of POST /hosts/lease as a flexible JSONB-matched dict.
    Only fields explicitly set are included in the request, so the connector
    will not constrain on fields the caller does not care about.
    """

    repo_url: str | None = Field(default=None, description="Repository URL the agent will run from")
    repo_branch_or_tag: str | None = Field(default=None, description="Branch or tag the host was provisioned with")
    cpus: int | None = Field(default=None, description="Number of vCPUs")
    memory_gb: int | None = Field(default=None, description="Memory in GB")
    gpu_count: int | None = Field(default=None, description="Number of GPUs (0 for CPU-only)")

    def to_request_dict(self) -> dict[str, Any]:
        """Drop None values so the connector treats them as 'unconstrained'."""
        return {k: v for k, v in self.model_dump().items() if v is not None}

    def relaxed(self) -> "LeaseAttributes":
        """Drop the version/repo constraints, keeping only resource constraints.

        Used by the slow path: it rebuilds the host from scratch, so the pool
        host's pre-baked ``repo_url`` / ``repo_branch_or_tag`` no longer need to
        match. Keeping ``cpus`` / ``memory_gb`` / ``gpu_count`` ensures the user
        still lands on adequately-sized hardware.
        """
        return LeaseAttributes(
            repo_url=None,
            repo_branch_or_tag=None,
            cpus=self.cpus,
            memory_gb=self.memory_gb,
            gpu_count=self.gpu_count,
        )


class ParsedImbueCloudBuildArgs(FrozenModel):
    """Result of splitting ``mngr create -b`` entries for the imbue_cloud provider.

    The imbue_cloud provider consumes the lease/control keys it recognizes and
    forwards everything else (e.g. ``--file=Dockerfile``, ``.``) verbatim to the
    delegated vps_docker build that the slow path runs.
    """

    attributes: "LeaseAttributes" = Field(description="Lease-attribute filter for the connector")
    account_override: str | None = Field(default=None, description="``-b account=<email>`` override, if any")
    fast_mode: FastMode = Field(description="Whether the fast/adopt path is required or prevented")
    region: str | None = Field(
        default=None,
        description=(
            "``-b region=<dc>`` hard region requirement: only lease a host in this OVH datacenter, "
            "or fail with a clear no-capacity error."
        ),
    )
    passthrough_build_args: tuple[str, ...] = Field(
        default=(),
        description="Unrecognized -b entries forwarded verbatim to the delegated vps_docker build",
    )


_LEASE_ATTRIBUTE_KEYS: frozenset[str] = frozenset(LeaseAttributes.model_fields.keys())
_INTEGER_ATTRIBUTE_KEYS: frozenset[str] = frozenset({"cpus", "memory_gb", "gpu_count"})


def parse_imbue_cloud_build_args(build_args: Sequence[str] | None) -> ParsedImbueCloudBuildArgs:
    """Split mngr's ``-b KEY=VALUE`` entries into lease/control knobs and pass-through args.

    Recognized lease-attribute keys (``repo_url``, ``repo_branch_or_tag``,
    ``cpus``, ``memory_gb``, ``gpu_count``) populate the ``LeaseAttributes``
    filter. ``account`` selects the Imbue Cloud session. ``fast_mode`` selects
    the create path (``require`` / ``prevent``; defaults to
    :data:`DEFAULT_FAST_MODE`). ``region`` is a hard datacenter requirement
    (validated against
    :data:`~imbue.mngr_imbue_cloud.primitives.KNOWN_OVH_US_REGIONS`). Every other
    entry -- including bare positionals like ``.`` and docker flags like
    ``--file=Dockerfile`` -- is preserved verbatim as a pass-through build arg for
    the delegated vps_docker build.

    Raises ``ValueError`` on a malformed recognized key (e.g. a non-integer
    ``cpus``, an unknown ``fast_mode``, or an unknown ``region`` value).
    """
    if not build_args:
        return ParsedImbueCloudBuildArgs(attributes=LeaseAttributes(), fast_mode=DEFAULT_FAST_MODE)
    parsed_attributes: dict[str, Any] = {}
    account_override: str | None = None
    fast_mode = DEFAULT_FAST_MODE
    region: str | None = None
    passthrough: list[str] = []
    for entry in build_args:
        key, separator, value = entry.partition("=")
        key = key.strip()
        value = value.strip()
        if separator and key == "account":
            if not value:
                raise InvalidBuildArgError("build_arg account=<email> requires a non-empty value")
            account_override = value
        elif separator and key == "region":
            # Validate the region against the known OVH-US datacenters so a typo
            # fails fast at create time instead of silently leasing a non-matching
            # (or no) host. An empty value is also rejected here (it's not in the
            # set). ValueError matches the rest of this parser's contract -- the
            # caller (instance.create_host) catches ValueError and wraps it.
            if value not in KNOWN_OVH_US_REGIONS:
                allowed = sorted(KNOWN_OVH_US_REGIONS)
                raise InvalidBuildArgError(f"build_arg region={value!r} must be one of {allowed}")
            region = value
        elif separator and key == "fast_mode":
            try:
                fast_mode = FastMode(value.upper())
            except ValueError as exc:
                allowed = sorted(mode.value.lower() for mode in FastMode)
                raise InvalidBuildArgError(f"build_arg fast_mode={value!r} must be one of {allowed}") from exc
        elif separator and key in _INTEGER_ATTRIBUTE_KEYS:
            try:
                parsed_attributes[key] = int(value)
            except ValueError as exc:
                raise InvalidBuildArgError(f"build_arg {key}={value!r} must be an integer") from exc
        elif separator and key in _LEASE_ATTRIBUTE_KEYS:
            parsed_attributes[key] = value
        else:
            # Unrecognized entry: forward verbatim to the delegated vps_docker
            # build (e.g. ``--file=Dockerfile`` or the ``.`` build context).
            passthrough.append(entry)
    return ParsedImbueCloudBuildArgs(
        attributes=LeaseAttributes(**parsed_attributes),
        account_override=account_override,
        fast_mode=fast_mode,
        region=region,
        passthrough_build_args=tuple(passthrough),
    )


class LeaseResult(FrozenModel):
    """Server response from POST /hosts/lease."""

    host_db_id: LeaseDbId = Field(description="Database id of the leased host (UUID)")
    vps_address: str = Field(
        description=(
            "SSH-reachable address of the VPS -- either a public IPv4 or a DNS hostname, "
            "depending on what the host's provider returned at bake time. OVH-backed "
            "rows are DNS hostnames like ``vps-eec8860b.vps.ovh.us``."
        )
    )
    ssh_port: int = Field(description="SSH port for the VPS itself (root)")
    ssh_user: str = Field(description="SSH username on the VPS")
    container_ssh_port: int = Field(description="Port that maps to the docker container's sshd")
    agent_id: str = Field(description="Pre-baked mngr agent id on the host")
    host_id: str = Field(description="Pre-baked mngr host id")
    host_name: str = Field(description="User-chosen friendly name for the leased host")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Attributes the row was matched against")
    outer_host_public_key: str | None = Field(
        default=None,
        description=(
            "The VPS/VM-root sshd host public key (port ssh_port). Pinned for strict host-key "
            "checking on the outer connection; None only against a connector too old to return it."
        ),
    )
    container_host_public_key: str | None = Field(
        default=None,
        description=(
            "The docker container sshd host public key (port container_ssh_port). Pinned for the "
            "agent connection on the fast/adopt path; None only against a connector too old to return it."
        ),
    )


class LeasedHostInfo(FrozenModel):
    """One entry from GET /hosts."""

    host_db_id: LeaseDbId
    vps_address: str = Field(
        description=(
            "SSH-reachable address of the VPS. Public IPv4 for Vultr-backed rows, "
            "DNS hostname (e.g. ``vps-eec8860b.vps.ovh.us``) for OVH-backed rows."
        )
    )
    ssh_port: int
    ssh_user: str
    container_ssh_port: int
    agent_id: str
    host_id: str
    host_name: str = Field(description="User-chosen friendly name for the leased host")
    attributes: dict[str, Any] = Field(default_factory=dict)
    leased_at: str = Field(description="ISO-8601 timestamp")
    outer_host_public_key: str | None = Field(
        default=None, description="The VPS/VM-root sshd host public key, if known"
    )
    container_host_public_key: str | None = Field(
        default=None, description="The docker container sshd host public key, if known"
    )


class AuthUser(FrozenModel):
    """User information returned by signin/signup/oauth callbacks."""

    user_id: SuperTokensUserId
    email: ImbueCloudAccount
    display_name: str | None = None


class AuthSession(FrozenModel):
    """Persisted session entry, written to disk per user_id."""

    user_id: SuperTokensUserId
    email: ImbueCloudAccount
    display_name: str | None = None
    access_token: SecretStr = Field(description="SuperTokens JWT access token")
    refresh_token: SecretStr | None = Field(default=None, description="SuperTokens refresh token")
    access_token_expires_at: datetime | None = Field(
        default=None,
        description="UTC datetime at which the access token expires (decoded from JWT exp)",
    )


class LiteLLMKeyMaterial(FrozenModel):
    """Key + base URL returned by POST /keys/create."""

    key: SecretStr
    base_url: AnyUrl


class LiteLLMKeyInfo(FrozenModel):
    """Metadata about a LiteLLM virtual key."""

    token: str
    key_alias: str | None = None
    key_name: str | None = None
    spend: Decimal = Decimal("0")
    max_budget: Decimal | None = None
    budget_duration: str | None = None
    user_id: str | None = None


class TunnelInfo(FrozenModel):
    """A Cloudflare tunnel record."""

    tunnel_name: str
    tunnel_id: str
    token: SecretStr | None = None
    services: tuple[str, ...] = ()


class ServiceInfo(FrozenModel):
    """A service forwarded over a Cloudflare tunnel."""

    service_name: str
    service_url: str
    hostname: str


class AuthPolicy(FrozenModel):
    """Cloudflare Access policy expressed as allowed emails / IDPs."""

    emails: tuple[str, ...] = ()
    email_domains: tuple[str, ...] = ()
    require_idp: tuple[str, ...] = ()


class R2BucketInfo(FrozenModel):
    """Metadata about an R2 bucket owned by the account."""

    bucket_name: str = Field(description="Full R2 bucket name (<user_id_prefix>--<slug>)")
    s3_endpoint: AnyUrl = Field(description="S3-compatible endpoint for this account")


class R2KeyMaterial(FrozenModel):
    """A bucket-scoped S3 credential, returned once at key creation."""

    access_key_id: R2AccessKeyId = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    secret_access_key: SecretStr = Field(description="S3 Secret Access Key (shown once, never persisted by us)")
    s3_endpoint: AnyUrl = Field(description="S3-compatible endpoint for this account")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: R2BucketAccess = Field(description="Access scope: 'read' or 'readwrite'")


class R2KeyInfo(FrozenModel):
    """Metadata about a bucket key (never includes the secret)."""

    access_key_id: R2AccessKeyId = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: R2BucketAccess = Field(description="Access scope: 'read' or 'readwrite'")
    alias: str | None = Field(default=None, description="Human-readable alias")
    created_at: str = Field(description="ISO 8601 timestamp when the key was created")


class R2BucketCreateResult(FrozenModel):
    """Result of creating a bucket: the bucket plus its minted default key."""

    bucket: R2BucketInfo = Field(description="The created bucket")
    key: R2KeyMaterial = Field(description="The default key minted alongside the bucket")


class BareMetalServer(FrozenModel):
    """A rented OVH bare-metal server that we carve into lima-VM slices.

    Mirrors one ``bare_metal_servers`` row. Resource fields and ``raid_level`` /
    ``lima_service_user`` / ``ovh_service_name`` / ``public_address`` are filled
    in as the box advances through its lifecycle, so they are optional until the
    box reaches the state that populates them.
    """

    id: BareMetalServerDbId = Field(description="Database id (server-side UUID)")
    ovh_order_id: str | None = Field(default=None, description="OVH order id captured at checkout")
    ovh_service_name: str | None = Field(default=None, description="OVH dedicated serviceName (set on delivery)")
    plan_code: str = Field(description="Catalog planCode the box was ordered as")
    region: str = Field(description="OVH datacenter code (e.g. 'vin')")
    public_address: str | None = Field(default=None, description="SSH-reachable public address (set once known)")
    cpu_cores: int | None = Field(default=None, description="Physical CPU cores (detected during install)")
    cpu_threads: int | None = Field(default=None, description="CPU threads (detected during install)")
    ram_gb: int | None = Field(default=None, description="Total RAM in GB (detected during install)")
    disk_gb: int | None = Field(default=None, description="Usable disk in GB for slice data (detected/provided)")
    memory_per_slice_gb: int | None = Field(
        default=None, description="RAM (GB) each slice on this box advertises; sets slot_count and per-slice sizing"
    )
    cpu_overcommit_ratio: float | None = Field(
        default=None, description="CPU overcommit factor used to size each slice's vCPUs on this box"
    )
    slot_count: int = Field(description="Number of slices this box holds (floor(ram_gb / memory_per_slice_gb))")
    raid_level: str | None = Field(default=None, description="RAID level set at OS-install time (e.g. 'RAID1')")
    lima_service_user: str | None = Field(default=None, description="Non-root OS user that owns the box's lima VMs")
    box_host_public_key: str | None = Field(
        default=None,
        description=(
            "The box's sshd host public key (port 22), injected by us at OS reinstall so it is "
            "deterministically known. Pinned by admin tooling, the lima slice client, and the connector's "
            "slice teardown. None until set at provision (or by the one-time keyscan backfill)."
        ),
    )
    status: BareMetalServerStatus = Field(description="Lifecycle state: ordered/delivered/installing/ready/failed")
    created_at: datetime = Field(description="When the row was created")
    updated_at: datetime = Field(description="When the row was last updated")


class BareMetalServerCapacity(FrozenModel):
    """A bare-metal server plus its slice-slot accounting, for the admin list view."""

    server: BareMetalServer = Field(description="The bare-metal server")
    used_slots: int = Field(description="Number of baked slices currently on this server")
    free_slots: int = Field(description="Slots still available to bake (slot_count - used_slots)")


class PriceLineItem(FrozenModel):
    """One priced component of an OVH order: the plan itself or a selected add-on."""

    plan_code: str = Field(description="OVH planCode of this component (the plan or an add-on)")
    description: str = Field(description="Human-readable label (the catalog invoiceName)")
    monthly: Decimal = Field(description="Recurring month-to-month price in USD (no commitment)")
    one_time_setup: Decimal = Field(description="One-time setup/installation fee in USD (month-to-month term)")


class OrderPricing(FrozenModel):
    """Full month-to-month pricing for an OVH plan plus its selected add-ons.

    The point of this type is that ``recurring_monthly`` already includes every
    selected add-on delta (RAM/storage/bandwidth upgrades), so callers can never
    mistake the catalog's bare base price for the true recurring cost.
    """

    plan_code: str = Field(description="OVH planCode of the ordered plan")
    line_items: tuple[PriceLineItem, ...] = Field(
        description="The plan plus each selected add-on, individually priced"
    )
    recurring_monthly: Decimal = Field(
        description="True monthly cost in USD: base plan plus all selected add-on deltas"
    )
    one_time_setup: Decimal = Field(description="Total one-time setup fee in USD (waived on committed terms)")
    first_payment: Decimal = Field(description="Amount charged at checkout in USD: recurring_monthly + one_time_setup")


class SliceStorageOption(FrozenModel):
    """One orderable storage config for a server, expressed as a per-slice disk upgrade over the base."""

    storage_plan_code: str = Field(description="OVH storage add-on planCode (full, plan-suffixed)")
    label: str = Field(description="Short storage label parsed from the planCode (e.g. '2x1920nvme')")
    raid_level: str = Field(
        description="Mirror-based RAID level assumed for usable capacity (RAID1/RAID10/RAID5/MIXED)"
    )
    usable_disk_gb: int = Field(description="Usable disk in GB after RAID, for the whole server")
    extra_disk_gb_per_slice: int = Field(description="Additional usable disk per slice vs the row's base storage")
    extra_monthly_usd: Decimal = Field(description="Additional month-to-month cost in USD vs the row's base storage")
    dollars_per_extra_gb: Decimal = Field(
        description="Marginal USD per added usable GB vs base (same per-slice or whole-server, since slots cancel)"
    )


class SlicePricingRow(FrozenModel):
    """Pricing + effective slice sizing for one (server x RAM config), for the operator pricing table.

    Each row is the product of a bare-metal plan and one of its memory configs, priced
    month-to-month with the setup fee amortized over a year, divided across the slices the
    config yields. Storage stays a per-row list of upgrade options rather than its own product axis.
    """

    plan_code: str = Field(description="OVH planCode of the bare-metal server")
    server_model: str = Field(description="CPU / server description (e.g. 'Intel Xeon-E 2388G')")
    region: str = Field(description="OVH datacenter code this row is priced for (e.g. 'vin', 'hil')")
    delivery_hours: int = Field(
        description="Fastest advertised delivery time in hours for the base config (from OVH availability; lower = sooner)"
    )
    stock_level: str = Field(
        description="Stock level for that fastest option ('high'/'low'), or '' when OVH reports only a delivery time"
    )
    server_ram_gb: int = Field(description="Total server RAM in GB for this row's memory config")
    cpu_cores: int = Field(description="Physical CPU cores")
    cpu_threads: int = Field(description="CPU threads")
    memory_per_slice_gb: int = Field(description="RAM (GB) each slice advertises (the requested slice size)")
    slot_count: int = Field(description="Slices this server holds = floor(server_ram_gb / memory_per_slice_gb)")
    cpus_per_slice: int = Field(description="vCPUs per slice after CPU overcommit")
    disk_gb_per_slice: int = Field(
        description="Total usable disk per slice with the base (cheapest in-region) storage"
    )
    base_storage_label: str = Field(description="The cheapest in-region storage backing the base price/disk columns")
    recurring_monthly_usd: Decimal = Field(
        description="True month-to-month cost: base plan + RAM + base-storage deltas"
    )
    one_time_setup_usd: Decimal = Field(description="One-time setup fee in USD")
    amortized_monthly_usd: Decimal = Field(description="recurring_monthly + setup/12 (setup amortized over one year)")
    price_per_slice_usd: Decimal = Field(description="amortized_monthly / slot_count -- the primary sort key")
    storage_options: tuple[SliceStorageOption, ...] = Field(
        description="Other in-region storage configs as per-slice disk upgrades (not splatted into their own rows)"
    )
