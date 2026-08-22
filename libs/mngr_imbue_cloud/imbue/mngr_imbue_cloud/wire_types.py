"""Wire models for remote_service_connector responses.

Everything in this file describes a connector HTTP response shape and MUST
inherit :class:`~imbue.mngr_imbue_cloud.wire.WireModel` (and enums here
:class:`~imbue.mngr_imbue_cloud.wire.WireEnum`), so already-shipped clients
tolerate additive server changes. Internal models (CLI reports, DB mirrors,
request-side objects) do not belong here -- they stay strict in
``data_types.py``. See ``wire.py`` for the forward-compatibility contract.
"""

from decimal import Decimal
from enum import auto
from typing import Any

from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.primitives import R2AccessKeyId
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.wire import WireEnum
from imbue.mngr_imbue_cloud.wire import WireModel

# The newest workspace-record semantics this client understands. A record
# whose wire ``record_format`` exceeds this is treated as read-only ("update
# the app to manage this machine"): no pushes, no tombstone/destroy, no
# release, no disassociation. Bumped only for semantically breaking record
# changes -- purely additive display fields ride the server's
# preserve-on-absent merge without a bump.
SUPPORTED_RECORD_FORMAT: int = 1


class WorkspaceStatus(WireEnum):
    """Wire lifecycle status of a remote workspace (GET /workspaces).

    ``running`` maps from the connector-internal ``leased``. ``stopping``
    means the VM is halted and its upload is in flight (the connector
    refuses starts until it lands on ``stopped``); ``stopped`` means the
    artifact is in object storage, with the halted local VM kept through the
    retention window for a restart in place before the slot is freed;
    ``starting`` means a supervisor is restoring it; ``crashed`` means an
    operator abandoned it (recover from backup).
    ``unknown`` is never sent by the server: it is the client-side coercion
    of a status value this client version does not recognize (a newer
    server), rendered as "shown but not actionable".
    """

    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    STARTING = auto()
    CRASHED = auto()
    UNKNOWN = auto()


class R2BucketAccess(WireEnum):
    """Access scope of an R2 bucket key: 'read' or 'readwrite' (lowercase wire form).

    ``unknown`` is the client-side coercion of a scope this client version
    does not recognize; consumers treat it exactly like ``read`` (the
    conservative interpretation).
    """

    READ = auto()
    READWRITE = auto()
    UNKNOWN = auto()


class AuthRawResponse(WireModel):
    """Subset of ``/auth/*`` response that we care about.

    The connector's response shape is:
    ``{status, message, user, tokens, needs_email_verification}``.
    """

    status: str
    message: str | None = None
    user: dict[str, Any] | None = None
    tokens: dict[str, Any] | None = None
    needs_email_verification: bool = False


class PaidListEntry(WireModel):
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


class LeaseResult(WireModel):
    """Server response from POST /hosts/lease."""

    host_db_id: LeaseDbId = Field(description="Database id of the leased host (UUID)")
    vps_address: str = Field(
        description=(
            "SSH-reachable address of the leased host's bare-metal box (the box's public "
            "address that the slice VM is reached through)."
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


class WorkspaceInfo(WireModel):
    """One entry from GET /workspaces: a workspace in any lifecycle state.

    Placement fields (``vps_address`` and the two ports) stay set on a
    just-stopped workspace through the retention window (its halted local VM
    is kept for a restart in place) and are None once the retention finalize
    frees the slot -- the VM then exists only as encrypted objects in the
    tier's storage bucket. ``status`` uses the wire lifecycle vocabulary
    (:class:`WorkspaceStatus`).
    """

    host_db_id: LeaseDbId = Field(description="Durable workspace identity (the connector row id)")
    status: WorkspaceStatus = Field(
        description=(
            "Lifecycle status: running/stopping/stopped/starting/crashed on the wire; an "
            "unrecognized value (a newer server) coerces to UNKNOWN client-side"
        )
    )
    vps_address: str | None = Field(
        default=None, description="Box address (None once fully stopped; see class docstring)"
    )
    ssh_port: int | None = Field(
        default=None, description="VM-root forwarded port (None once fully stopped; see class docstring)"
    )
    ssh_user: str = Field(default="root", description="SSH user on the VM")
    container_ssh_port: int | None = Field(
        default=None, description="Container forwarded port (None once fully stopped; see class docstring)"
    )
    agent_id: str = Field(description="Pre-baked mngr agent id")
    host_id: str = Field(description="mngr host id")
    host_name: str = Field(description="User-chosen friendly name")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Lease attributes")
    leased_at: str = Field(default="", description="ISO-8601 lease timestamp")
    stop_requested_at: str | None = Field(default=None, description="When the current/last stop was requested")
    stopped_at: str | None = Field(default=None, description="When the workspace reached stopped")
    transition_error: str | None = Field(default=None, description="Last stop/start failure, if any")
    outer_host_public_key: str | None = Field(default=None, description="Pinned VM-root sshd host key")
    container_host_public_key: str | None = Field(default=None, description="Pinned container sshd host key")


class LeasedHostInfo(WireModel):
    """One entry from GET /hosts."""

    host_db_id: LeaseDbId
    vps_address: str = Field(
        description="SSH-reachable address of the leased host's bare-metal box (reaches the slice VM)."
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


class LiteLLMKeyMaterial(WireModel):
    """Key + base URL returned by POST /keys/create."""

    key: SecretStr
    base_url: AnyUrl


class LiteLLMKeyInfo(WireModel):
    """Metadata about a LiteLLM virtual key."""

    token: str
    key_alias: str | None = None
    key_name: str | None = None
    spend: Decimal = Decimal("0")
    max_budget: Decimal | None = None
    budget_duration: str | None = None
    user_id: str | None = None


class ShareRelayEndpoint(WireModel):
    """One relay a shared workspace tunnels to: its registered id and tunnel-control endpoint."""

    relay_id: str = Field(description="The relay's registered id (relay-<hex>)")
    endpoint: str = Field(description="host:port the workspace's frpc dials")


class ShareRelayLogin(WireModel):
    """One relay's last tunnel Login stamp for a share (from the status document)."""

    relay_id: str = Field(description="The relay's registered id (relay-<hex>)")
    last_login_at: str | None = Field(default=None, description="When the share's tunnel last logged into this relay")


class ShareInfo(WireModel):
    """One workspace's self-hosted share record (the relay-based sharing model)."""

    host_id: str = Field(description="The workspace's host coordinate (host-<32hex>)")
    workspace_domain: str = Field(description="The share's registrable base, host-<hex>.<user>.<region>.<domain>")
    region: str = Field(description="Relay region code the share is served from")
    state: str = Field(description="'active' while shared; 'inactive' after unshare")
    relay_endpoints: tuple[ShareRelayEndpoint, ...] = Field(
        default=(), description="Every relay of the share's region the workspace tunnels to"
    )
    relays: tuple[ShareRelayLogin, ...] = Field(
        default=(), description="Per-relay tunnel login stamps (status documents only; empty elsewhere)"
    )
    relay_token: SecretStr | None = Field(
        default=None, description="Opaque per-share relay token; returned once at share-enable"
    )
    last_tunnel_login_at: str | None = Field(default=None, description="Last relay tunnel Login stamp (any relay)")
    cert_not_after: str | None = Field(default=None, description="Expiry of the newest issued certificate")


class ShareRelayMap(WireModel):
    """The relay fleet as reported by the connector: region -> tunnel-control endpoints."""

    relay_endpoints_by_region: dict[str, tuple[str, ...]] = Field(
        description="Relay tunnel-control endpoints (host:port) per region code"
    )


class RelayAdminInfo(WireModel):
    """One relay row from the connector's fleet inventory (admin API)."""

    relay_id: str = Field(description="The relay's registered id (relay-<hex>)")
    region: str = Field(description="Region code the relay serves")
    tunnel_endpoint: str = Field(description="host:port the workspaces' frpc dials")
    ip_address: str = Field(description="Public IPv4 (DNS answer + healthz probe target)")
    instance_name: str = Field(description="Human-readable OVH instance name")
    is_active: bool = Field(description="False once retired")
    health: str = Field(description="'healthy' / 'unhealthy' per the connector's sweep")
    consecutive_probe_failures: int = Field(description="Failed healthz probes since the last success")


class R2BucketInfo(WireModel):
    """Metadata about an R2 bucket owned by the account."""

    bucket_name: str = Field(description="Full R2 bucket name (<user_id_prefix>--<slug>)")
    s3_endpoint: AnyUrl = Field(description="S3-compatible endpoint for this account")


class R2KeyMaterial(WireModel):
    """A bucket-scoped S3 credential, returned once at key creation."""

    access_key_id: R2AccessKeyId = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    secret_access_key: SecretStr = Field(description="S3 Secret Access Key (shown once, never persisted by us)")
    s3_endpoint: AnyUrl = Field(description="S3-compatible endpoint for this account")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: R2BucketAccess = Field(description="Access scope: 'read' or 'readwrite'")


class R2KeyInfo(WireModel):
    """Metadata about a bucket key (never includes the secret)."""

    access_key_id: R2AccessKeyId = Field(description="S3 Access Key ID (= the Cloudflare token id)")
    bucket_name: str = Field(description="Full R2 bucket name this key is scoped to")
    access: R2BucketAccess = Field(description="Access scope: 'read' or 'readwrite'")
    alias: str | None = Field(default=None, description="Human-readable alias")
    created_at: str = Field(description="ISO 8601 timestamp when the key was created")
    enforced_access: str | None = Field(
        default=None,
        description=(
            "Storage-quota enforcement state from the connector: 'read' when the sweep downgraded this "
            "key because the account is over its storage quota; None when the live token policy matches "
            "the intended access."
        ),
    )


class R2BucketCreateResult(WireModel):
    """Result of creating a bucket: the bucket plus its minted default key."""

    bucket: R2BucketInfo = Field(description="The created bucket")
    key: R2KeyMaterial = Field(description="The default key minted alongside the bucket")


class StorageCleanupGrant(WireModel):
    """Result of requesting a storage-cleanup grant (POST /account/storage-cleanup-grant)."""

    status: str = Field(description="'granted' when a grant is active (new or pre-existing), 'not_needed' otherwise")
    expires_at: str | None = Field(default=None, description="When the active grant expires")
    baseline_bytes: int | None = Field(default=None, description="Live usage recorded at grant time")
    keys: tuple[R2KeyInfo, ...] = Field(default=(), description="The account's bucket keys after the grant")


class StorageRecheckResult(WireModel):
    """Result of an on-demand storage recheck (POST /account/storage-recheck)."""

    usage_bytes: int = Field(description="Live total bucket bytes (real-time)")
    limit_bytes: int = Field(description="The account's max_total_bucket_bytes entitlement")
    is_over_quota: bool = Field(description="Whether live usage exceeds the limit")
    is_grant_settled: bool = Field(description="Whether this recheck settled an outstanding cleanup grant")
    keys: tuple[R2KeyInfo, ...] = Field(default=(), description="The account's bucket keys after enforcement")


class AccountEntitlementValues(WireModel):
    """The quota values an account currently holds (mirrors the connector's PlanEntitlements)."""

    max_remote_workspaces: int = Field(description="Max running remote workspaces (leased/stopping/starting)")
    max_total_workspaces: int = Field(description="Max total remote workspaces, running + stopped")
    max_buckets: int = Field(description="Max R2 buckets")
    max_total_bucket_bytes: int = Field(description="Max total bytes across all the account's buckets")
    monthly_llm_spend_usd: float = Field(description="Monthly LLM spend cap in USD (rolling)")
    max_active_synced_workspaces: int = Field(description="Max ACTIVE synced workspace records")


class AccountUsageInfo(WireModel):
    """Live usage numbers for an account (mirrors the connector's AccountUsage)."""

    remote_workspaces: int = Field(description="Current running pool-host leases")
    total_workspaces: int = Field(description="Current total remote workspaces, running + stopped")
    buckets: int = Field(description="Current R2 buckets")
    total_bucket_bytes: int = Field(description="Total bytes across the account's buckets")
    llm_spend_usd_this_period: float = Field(description="LiteLLM aggregate spend in the current budget period")
    llm_budget_resets_at: str | None = Field(default=None, description="When the rolling LLM budget period resets")
    active_synced_workspaces: int = Field(description="Current ACTIVE synced workspace records")


class AccountInfo(WireModel):
    """An account's plan, entitlement values, and live usage, from GET /account."""

    user_id: SuperTokensUserId = Field(description="SuperTokens user id")
    email: str = Field(description="The account's verified email")
    plan_name: str = Field(description="Current plan name (e.g. 'explorer' or 'ally')")
    entitlements: AccountEntitlementValues = Field(description="The account's current entitlement values")
    usage: AccountUsageInfo = Field(description="Live usage, computed by the connector at request time")
    available_plans: tuple[str, ...] = Field(
        default=(), description="Every plan name currently seeded (for plan-selector UIs)"
    )


class AdminAccountInfo(AccountInfo):
    """The operator view of an account, from GET /admin/accounts/{email}.

    Extends the user-facing shape with the suspension state, which is
    operator-facing only (the connector never sends it on ``GET /account``).
    """

    suspended_at: str | None = Field(default=None, description="When the account was suspended (None = not suspended)")
    suspended_reason: str | None = Field(
        default=None, description="Operator-recorded suspension reason (internal; never shown to the user)"
    )


class SyncWorkspaceRecord(WireModel):
    """Wire form of one synced workspace record (transport-only; the plugin never decrypts).

    Mirrors the connector's ``WorkspaceRecordModel``: plaintext metadata plus
    the base64 of the client-side-encrypted secrets blob. ``state`` is passed
    through as its lowercase wire string -- the producing (minds) and
    validating (connector) ends own the vocabulary.
    """

    host_id: str = Field(description="Host the workspace is on (PK with the account)")
    agent_id: str = Field(description="Logical workspace id (one ACTIVE record per agent_id)")
    display_name: str = Field(default="", description="Workspace display name")
    color: str | None = Field(default=None, description="Workspace accent color (#rrggbb)")
    provider_kind: str = Field(description="mngr provider backend kind (e.g. 'lima', 'imbue_cloud')")
    hosting_device_id: str | None = Field(
        default=None, description="Install that hosts a local workspace (None for cloud rows)"
    )
    device_label: str = Field(default="", description="Human-readable device name")
    state: str = Field(description="Lifecycle state: 'active' or 'destroyed' (tombstone)")
    restored_from_host_id: str | None = Field(default=None, description="Lineage link for restored workspaces")
    encrypted_secrets: str | None = Field(
        default=None, description="Base64 of the client-encrypted secrets blob (opaque here)"
    )
    revision: int = Field(description="Per-row monotonic revision; pushes are CAS on this")
    record_format: int = Field(
        default=1,
        description=(
            "Semantic format of the record (missing = 1). A client whose SUPPORTED_RECORD_FORMAT is "
            "below this treats the record as read-only; the server rejects pushes carrying a value "
            "below the stored row's (409 record_format_too_new)."
        ),
    )
    created_at: str = Field(default="", description="Server timestamp (response only)")
    updated_at: str = Field(default="", description="Server timestamp (response only)")
    destroyed_at: str | None = Field(
        default=None,
        description=(
            "Server tombstone stamp (response only; set while state is 'destroyed'). Passed through so "
            "clients can age destroyed workspaces' backups against the server's clock."
        ),
    )


class SyncKeyBundle(WireModel):
    """Wire form of the per-account password-wrapped data key (transport-only)."""

    kdf_salt: str = Field(description="Base64 argon2id salt")
    kdf_time_cost: int = Field(description="argon2id iteration count")
    kdf_memory_kib: int = Field(description="argon2id memory (KiB)")
    kdf_parallelism: int = Field(description="argon2id lane count")
    wrapped_dek: str = Field(description="Base64 password-wrapped DEK (opaque here)")
    key_epoch: int = Field(description="Bumped only on compromise recovery")
    updated_at: str = Field(default="", description="Server timestamp (response only)")
