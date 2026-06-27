"""Pydantic models for the ``/api/v1`` surface -- the single source of truth.

These models describe the request bodies, query params, and responses of the
Minds API. They live in this low module (no imports from ``api_v1`` or
``api_schema``) so that both the route handlers (which validate against them) and
the schema endpoint (which publishes them) can import them without an import
cycle.

This is the foundation of the spectree conversion (see
``blueprint/minds-api-spectree/plan-minds-api-spectree.md``): today the schema
endpoint reads these for documentation; the in-progress conversion wires the
same models into the handlers as spectree request/response validators so the
documented contract and the enforced contract can never drift.
"""

from pydantic import ConfigDict
from pydantic import Field
from pydantic import StrictBool

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import LaunchMode


class ApiRequestModel(FrozenModel):
    """Base for request-body models.

    Unlike :class:`FrozenModel` (which forbids extra keys), request bodies must
    *ignore* unknown keys: several handlers accept a superset of the validated
    fields and pass the raw body straight through (e.g. the bug-report route
    forwards arbitrary extra fields to the collector). Forbidding extras would
    turn those previously-accepted requests into a spurious 422, a regression.
    Only the structural shape of the *known* fields is validated here; semantic
    checks stay in the handlers.
    """

    model_config = ConfigDict(extra="ignore")


class ApiErrorResponse(FrozenModel):
    """A JSON error body returned by Minds API routes on failure."""

    error: str = Field(description="Human-readable error message")
    field: str | None = Field(default=None, description="Offending request field, when the error is field-specific")
    redirect_url: str | None = Field(default=None, description="Where to send the user, for flows that need sign-in")


class ApiValidationErrorItem(FrozenModel):
    """A single field-level validation failure."""

    field: str = Field(description="Dotted path to the offending field (pydantic ``loc`` joined by '.')")
    message: str = Field(description="Why the field failed validation")


class ApiValidationErrorResponse(FrozenModel):
    """The 422 body returned when a request fails structural/type validation."""

    errors: tuple[ApiValidationErrorItem, ...] = Field(description="One entry per failed field")


class OperationHandleResponse(FrozenModel):
    """A handle for a long-running create/destroy/restart operation, to poll."""

    operation_id: str = Field(description="Poll at /api/v1/workspaces/operations/<operation_id>")
    kind: str = Field(description="Operation kind: create, destroy, or restart")


class AgentNotificationRequest(ApiRequestModel):
    """Body for sending a desktop notification on behalf of an agent."""

    message: str = Field(description="Notification body text")
    title: str | None = Field(default=None, description="Optional notification title")
    urgency: str | None = Field(default=None, description="One of: low, normal (default), critical")


class EstablishSshRequest(ApiRequestModel):
    """Body for requesting temporary SSH access into a target workspace."""

    public_key: str = Field(
        description="The caller's OpenSSH public key (single line); the private key never leaves the caller"
    )
    requester_workspace_id: str = Field(description="The calling workspace's own agent id (self-reported)")


class SshConnectionResponse(FrozenModel):
    """Connection info for SSHing into a target workspace."""

    agent_id: str = Field(description="The target workspace agent id")
    user: str = Field(description="SSH username on the target")
    host: str = Field(description="Reachable host (a remote address, or 127.0.0.1 for a hub-brokered local target)")
    port: int = Field(description="SSH port (the brokered loopback port for a local target)")
    expires_at: str = Field(description="UTC ISO 8601 expiry of the key grant")


class CreateWorkspaceRequest(ApiRequestModel):
    """Body for creating a new peer workspace."""

    git_url: str = Field(description="Template repository URL or local path (required)")
    host_name: str | None = Field(default=None, description="Workspace/host name; auto-assigned (mind-N) when omitted")
    branch: str | None = Field(default=None, description="Branch/tag to create from")
    color: str | None = Field(default=None, description="Hex color for the workspace tile")
    launch_mode: LaunchMode | None = Field(default=None, description="Compute provider (default DOCKER)")
    ai_provider: AIProvider | None = Field(
        default=None, description="How to obtain Anthropic credentials (default SUBSCRIPTION)"
    )
    account_id: str | None = Field(default=None, description="imbue_cloud account id (required for imbue_cloud modes)")
    anthropic_api_key: str | None = Field(
        default=None, description="Anthropic API key (required when ai_provider is API_KEY)"
    )
    region: str | None = Field(default=None, description="Provider region")
    backup_provider: BackupProvider | None = Field(
        default=None, description="Restic backup provider (default CONFIGURE_LATER)"
    )
    backup_encryption_method: BackupEncryptionMethod | None = Field(default=None, description="Backup repo key method")
    backup_master_password: str | None = Field(default=None, description="Master/recovery passphrase, when used")
    backup_save_password: bool | None = Field(default=None, description="Whether to persist the master password")
    backup_api_key_env: str | None = Field(default=None, description="KEY=VALUE block for an API_KEY backup provider")


class PatchWorkspaceRequest(ApiRequestModel):
    """Body for partially updating a workspace's metadata."""

    color: str | None = Field(default=None, description="New hex color")
    account_id: str | None = Field(default=None, description="Account id to associate, or null/empty to disassociate")


class RestartWorkspaceRequest(ApiRequestModel):
    """Body for restarting a workspace's services or whole host."""

    # ``scope`` is structurally a required string; the route validates that its
    # value is one of services/host (a value-semantic check kept in the handler,
    # since the lowercase wire values can't be a standard UpperCaseStrEnum).
    scope: str = Field(description="'services' (restart system-services in place) or 'host' (bounce the host)")
    host_already_stopped: bool | None = Field(
        default=None, description="Skip the redundant stop step (host scope only) when the host is known stopped"
    )


class EnableSharingRequest(ApiRequestModel):
    """Body for enabling/updating Cloudflare sharing of a workspace service."""

    emails: tuple[str, ...] = Field(
        default=(), description="Emails allowed by the Cloudflare Access policy (empty = open to all)"
    )


class BugReportRequest(ApiRequestModel):
    """Body for submitting a bug report on behalf of an in-workspace agent.

    Only ``description`` is part of the validated contract; the handler forwards
    the full (extra-field-bearing) body to the shared report collector, so extra
    keys are ignored here rather than rejected.
    """

    description: str = Field(min_length=1, description="What went wrong (required, non-empty)")


class SetProviderEnabledRequest(ApiRequestModel):
    """Body for toggling a provider's enabled flag."""

    # StrictBool (not bool) so a non-boolean like ``1`` or ``"yes"`` is rejected
    # rather than truthiness-coerced, matching the route's prior isinstance check.
    enabled: StrictBool = Field(description="Desired enabled state for the provider")


class WorkspaceSummary(FrozenModel):
    """Summary of one workspace from discovery + its labels."""

    agent_id: str = Field(description="Workspace (primary) agent id")
    name: str | None = Field(default=None, description="Workspace display name")
    host_id: str | None = Field(default=None, description="Host id, when known")
    host_state: str | None = Field(default=None, description="Host lifecycle state, when known")
    provider_name: str | None = Field(default=None, description="Provider backend name")
    create_time: str | None = Field(default=None, description="Creation time (UTC ISO 8601)")
    original_minds_version: str | None = Field(default=None, description="Immutable create-time minds version label")
    color: str | None = Field(default=None, description="Workspace tile color")


class WorkspaceListResponse(FrozenModel):
    """The list of all known workspaces (including destroyed-but-backed-up ones)."""

    workspaces: tuple[WorkspaceSummary, ...] = Field(description="All known workspaces")


class OkResponse(FrozenModel):
    """A minimal ``{"ok": true}`` acknowledgement."""

    ok: bool = Field(description="Whether the operation succeeded")


class BugReportResponse(FrozenModel):
    """Acknowledgement for a submitted bug report."""

    ok: bool = Field(description="Whether the report was accepted")
    event_id: str | None = Field(default=None, description="Id of the recorded report event, when one was written")


class WorkspaceLifecycleResponse(FrozenModel):
    """Result of a workspace host start/stop action."""

    agent_id: str = Field(description="The workspace agent id")
    action: str = Field(description="The action performed: start or stop")
    host_state: str | None = Field(default=None, description="The resulting host lifecycle state, when known")


class UpgradeMergeSummary(FrozenModel):
    """One minds-version upgrade merge in a workspace's git history."""

    commit_sha: str = Field(description="Full commit hash of the merge")
    committed_at: str | None = Field(default=None, description="Commit time (UTC ISO 8601), when parseable")
    summary: str = Field(description="First line of the merge commit message")


class WorkspaceVersionResponse(FrozenModel):
    """A workspace's minds version: the immutable create-time version + git-derived current/history."""

    agent_id: str = Field(description="The workspace agent id")
    original_minds_version: str | None = Field(default=None, description="Immutable create-time minds version label")
    current_minds_version: str | None = Field(default=None, description="Current minds version from the workspace git")
    upgrade_merges: tuple[UpgradeMergeSummary, ...] = Field(
        default=(), description="Upgrade merges applied since creation (best-effort)"
    )


class BackupSnapshotSummary(FrozenModel):
    """One restic backup snapshot of a workspace."""

    snapshot_id: str = Field(description="Full snapshot id (hex)")
    short_id: str = Field(description="Abbreviated snapshot id restic also accepts")
    time: str = Field(description="When the snapshot was created (UTC ISO 8601)")
    paths: tuple[str, ...] = Field(default=(), description="Absolute paths captured in the snapshot")
    hostname: str = Field(default="", description="Hostname recorded in the snapshot")
    tags: tuple[str, ...] = Field(default=(), description="Tags recorded on the snapshot")
    total_size_bytes: int | None = Field(default=None, description="Total snapshot size in bytes, when known")


class WorkspaceBackupsResponse(FrozenModel):
    """A workspace's restic backup snapshots plus whether a backup is running now."""

    agent_id: str = Field(description="The workspace agent id")
    is_backing_up: bool = Field(description="Whether a (non-stale) restic backup is currently running")
    snapshots: tuple[BackupSnapshotSummary, ...] = Field(default=(), description="All snapshots, newest-first")


class SharingReadinessResponse(FrozenModel):
    """Whether a shared service's hostname is live yet at the Cloudflare edge."""

    ready: bool = Field(description="Whether the shared URL is reachable yet")


class SharingToggleResponse(FrozenModel):
    """Result of enabling/disabling sharing for a workspace service."""

    agent_id: str = Field(description="The workspace agent id")
    service_name: str = Field(description="The service whose sharing was changed")
    enabled: bool = Field(description="Whether sharing is now enabled")


class ProviderToggleResponse(FrozenModel):
    """Result of toggling a provider's enabled flag."""

    provider_name: str = Field(description="The provider that was toggled")
    enabled: bool = Field(description="The provider's new enabled state")
    changed: bool = Field(description="Whether the state actually changed")


class StopStateContainerResponse(FrozenModel):
    """Result of stopping the local mngr Docker state container."""

    stopped: bool = Field(description="Whether the state container was stopped")
