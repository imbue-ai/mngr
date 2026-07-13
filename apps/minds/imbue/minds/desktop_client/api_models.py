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
from pydantic import SecretStr
from pydantic import StrictBool

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.primitives import AIProvider
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

    operation_id: str = Field(description="Poll at /api/v1/workspaces/operations/<kind>/<operation_id>")
    kind: str = Field(description="Operation kind: create, destroy, or restart")


class CreateOperationStatusResponse(FrozenModel):
    """Status of a create operation (polled at /operations/create/<id>)."""

    operation_id: str = Field(description="The creation id being polled")
    kind: str = Field(description="Always 'create'")
    status: str = Field(description="Raw creation status")
    status_text: str = Field(description="Human-readable, mode-aware stage caption for the creating page")
    is_done: bool = Field(description="Whether creation has finished successfully")
    agent_id: str | None = Field(default=None, description="The created workspace agent id, once known")
    redirect_url: str | None = Field(default=None, description="Absolute /goto/<agent>/ URL to navigate to when done")
    error: str | None = Field(default=None, description="Failure message, when the creation failed")


class DestroyOperationStatusResponse(FrozenModel):
    """Status of a destroy operation (polled at /operations/destroy/<id>)."""

    operation_id: str = Field(description="The workspace agent id being destroyed")
    kind: str = Field(description="Always 'destroy'")
    status: str = Field(description="Raw destroy status")
    is_done: bool = Field(description="Whether the host is fully gone")
    agent_id: str = Field(description="The workspace agent id (same as operation_id)")


class RestartOperationStatusResponse(FrozenModel):
    """Status of a restart operation (polled at /operations/restart/<id>)."""

    operation_id: str = Field(description="The workspace agent id being restarted")
    kind: str = Field(description="Always 'restart'")
    status: str = Field(description="Raw restart status")
    is_done: bool = Field(description="Whether the restart has finished")
    error: str | None = Field(default=None, description="Failure message, when the restart failed")


class BackupOperationStatusResponse(FrozenModel):
    """Status of a backup update/configure operation (polled at /operations/backup/<id>)."""

    operation_id: str = Field(description="The workspace agent id the operation acts on")
    kind: str = Field(description="'backup_update' or 'backup_configure'")
    status: str = Field(description="Raw operation status (RUNNING/DONE/FAILED)")
    is_done: bool = Field(description="Whether the operation has finished successfully")
    error: str | None = Field(default=None, description="Failure message, when the operation failed")
    blocked_chats: tuple[str, ...] = Field(
        default=(),
        description="Chat agents whose RUNNING state blocked the update (offer 'Stop all chats and retry')",
    )


class BackupServiceUpdateRequest(ApiRequestModel):
    """Body for the one idempotent 'Update backup service' action."""

    stop_chats: bool = Field(
        default=False,
        description="Stop actively-RUNNING chat agents first (the 'Stop all chats and retry' flow)",
    )


class BackupServiceConfigureRequest(ApiRequestModel):
    """Body for enabling backups or changing a workspace's backup destination."""

    backup_provider: str = Field(description="'IMBUE_CLOUD' or 'API_KEY'")
    master_password: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "The master password, validated against the stored hash. Blank falls back to the saved "
            "plaintext copy when one exists, else means the empty password."
        ),
    )
    save_password: bool = Field(
        default=False,
        description=(
            "Persist the typed (and just-validated) master password locally so later flows don't require retyping. "
            "Never establishes or changes the master password."
        ),
    )
    api_key_env: str = Field(default="", description="For API_KEY: KEY=VALUE block (RESTIC_REPOSITORY + creds)")


class BackupVerificationToggleRequest(ApiRequestModel):
    """Body for enabling/disabling backup verification on a workspace."""

    enabled: bool = Field(description="Whether verification (and the warning badge) is enabled")


class EmptyResponse(FrozenModel):
    """An empty ``{}`` success body (e.g. an idempotent dismissal)."""


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
    instance_type: str | None = Field(
        default=None,
        description="Machine size for the cloud-VM modes (AWS/GCP/Azure); must be one of the form's offered types",
    )
    cloud_account: str | None = Field(
        default=None,
        description=(
            "Bring-your-own cloud account to create on: the ``byo-<backend>-<slug>`` provider "
            "block name from /desktop/cloud-accounts. When set, launch_mode must match the "
            "account's backend and the create targets that provider instance."
        ),
    )
    backup_provider: BackupProvider | None = Field(
        default=None, description="Restic backup provider (default CONFIGURE_LATER)"
    )
    backup_master_password: SecretStr | None = Field(
        default=None,
        description=(
            "Master/recovery passphrase, validated against the stored hash. Blank/absent falls back to the "
            "saved copy when one exists, else means the empty password. Agents should always create with "
            "backups unconfigured and never ask the user for this."
        ),
    )
    backup_save_password: bool | None = Field(
        default=None, description="Persist the typed (just-validated) master password locally for later flows"
    )
    backup_api_key_env: str | None = Field(default=None, description="KEY=VALUE block for an API_KEY backup provider")


class PatchWorkspaceRequest(ApiRequestModel):
    """Body for partially updating a workspace's metadata."""

    color: str | None = Field(default=None, description="New hex color")
    account_id: str | None = Field(
        default=None,
        description=(
            "Signed-in account to associate -- either the account id or its email; "
            "null/empty to disassociate. An id/email that matches no signed-in account is rejected (404)."
        ),
    )


class RestartWorkspaceRequest(ApiRequestModel):
    """Body for restarting a workspace's services or whole host."""

    # ``scope`` is structurally a required string; the route validates that its
    # value is one of services/host (a value-semantic check kept in the handler,
    # since the lowercase wire values can't be a standard UpperCaseStrEnum).
    scope: str = Field(description="'services' (restart system-services in place) or 'host' (bounce the host)")
    host_already_stopped: bool | None = Field(
        default=None, description="Skip the redundant stop step (host scope only) when the host is known stopped"
    )
    auto_dispatched: bool | None = Field(
        default=None,
        description=(
            "Set by the recovery page's automatic tier dispatch (not a manual restart). When the workspace has "
            "already self-recovered to HEALTHY before the slow host-health probe finished, an auto-dispatched "
            "restart is skipped rather than bouncing a healthy backend; a manual restart always proceeds."
        ),
    )


class EnableSharingRequest(ApiRequestModel):
    """Body for enabling/updating Cloudflare sharing of a workspace service."""

    emails: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Emails allowed by the Cloudflare Access policy; at least one is required. "
            "An empty list is rejected because it would expose the service publicly."
        ),
    )


class BugReportRequest(ApiRequestModel):
    """Body for submitting a bug report on behalf of an in-workspace agent.

    Only ``description`` is part of the validated contract; the handler forwards
    the full (extra-field-bearing) body to the shared report collector, so extra
    keys are ignored here rather than rejected.
    """

    description: str = Field(min_length=1, description="What went wrong (required, non-empty)")


class CloudAccountCreateRequest(ApiRequestModel):
    """Body for registering a bring-your-own cloud account (pasted credentials)."""

    alias: str = Field(min_length=1, description="Display name for the account (also seeds the block-name slug)")
    backend: str = Field(description="Cloud backend: 'aws', 'gcp', or 'azure'")
    region: str = Field(
        min_length=1,
        description="Default placement for machines created on this account (an AWS/Azure region, or a GCE zone)",
    )
    aws_access_key_id: str | None = Field(default=None, description="AWS access key id (required for aws)")
    aws_secret_access_key: str | None = Field(default=None, description="AWS secret access key (required for aws)")
    gcp_service_account_key_json: str | None = Field(
        default=None, description="Full JSON contents of a GCP service-account key (required for gcp)"
    )
    azure_subscription_id: str | None = Field(default=None, description="Azure subscription id (required for azure)")
    azure_tenant_id: str | None = Field(default=None, description="Entra tenant id (required for azure)")
    azure_client_id: str | None = Field(default=None, description="Service-principal client id (required for azure)")
    azure_client_secret: str | None = Field(
        default=None, description="Service-principal client secret (required for azure)"
    )


class CloudAccountPatchRequest(ApiRequestModel):
    """Body for renaming a cloud account's display alias."""

    alias: str = Field(min_length=1, description="New display alias")


class CloudAccountSummary(FrozenModel):
    """One registered bring-your-own cloud account."""

    name: str = Field(description="Provider block name (byo-<backend>-<slug>) -- the stable id")
    alias: str = Field(description="Display alias")
    backend: str = Field(description="Cloud backend (e.g. 'aws')")
    region: str = Field(description="The account's current default region")
    identifier: str = Field(description="Masked credential hint (e.g. 'AKIA…F5X2'); never the secret")


class CloudAccountsResponse(FrozenModel):
    """All registered bring-your-own cloud accounts."""

    accounts: tuple[CloudAccountSummary, ...] = Field(description="Registered accounts")


class CloudAccountPrepareResponse(FrozenModel):
    """Result of registering + preparing a cloud account."""

    account: CloudAccountSummary = Field(description="The registered account")


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
    git_url: str | None = Field(
        default=None,
        description="Template repo URL or local path the workspace was created from (the agent's 'remote' label), when known",
    )
    branch: str | None = Field(
        default=None,
        description="Branch/tag the workspace was created from (the create-time value); null when none was specified",
    )
    account_id: str | None = Field(
        default=None, description="Signed-in account id the workspace is associated with, when any"
    )
    account_email: str | None = Field(default=None, description="Email of the associated signed-in account, when any")
    provider_name: str | None = Field(default=None, description="Provider backend name")
    create_time: str | None = Field(default=None, description="Creation time (UTC ISO 8601)")
    original_minds_version: str | None = Field(default=None, description="Immutable create-time minds version label")
    color: str | None = Field(default=None, description="Workspace tile color")


class WorkspaceListResponse(FrozenModel):
    """The list of all known workspaces (including destroyed-but-backed-up ones)."""

    workspaces: tuple[WorkspaceSummary, ...] = Field(description="All known workspaces")


class AccountSummary(FrozenModel):
    """One signed-in account on this device."""

    account_id: str = Field(description="Account id (the value to pass to the workspace-association API)")
    email: str = Field(description="Account email")
    display_name: str | None = Field(default=None, description="Account display name, when known")


class AccountsResponse(FrozenModel):
    """The accounts signed in on this device (for associating a workspace with one)."""

    accounts: tuple[AccountSummary, ...] = Field(description="All signed-in accounts")


class OkResponse(FrozenModel):
    """A minimal ``{"ok": true}`` acknowledgement."""

    ok: bool = Field(description="Whether the operation succeeded")


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
    """A workspace's full backup picture: snapshots plus the backup-service verification result.

    The snapshot half (restic, run from the minds machine) works even when
    the workspace is offline or destroyed; the verification half execs into
    the workspace and reports OFFLINE/DISABLED instead when it cannot or
    must not run. Cross-workspace parallelism is the caller's job -- this is
    deliberately the only backup-health surface, one workspace per request.
    """

    agent_id: str = Field(description="The workspace agent id")
    is_configured: bool = Field(description="Whether minds holds a canonical restic.env for this workspace")
    is_backing_up: bool = Field(description="Whether a (non-stale) restic backup is currently running")
    snapshots: tuple[BackupSnapshotSummary, ...] = Field(default=(), description="All snapshots, newest-first")
    snapshots_error: str | None = Field(
        default=None, description="Why the snapshot listing failed (e.g. restic error), when it did"
    )
    check_state: str = Field(description="Verification verdict: OK/PROBLEMS/OFFLINE/DISABLED/UNKNOWN")
    problems: tuple[str, ...] = Field(default=(), description="Detected backup-service problems (badge causes)")
    installed_version: str | None = Field(default=None, description="Installed backup-code version, when known")
    minimum_version: str | None = Field(
        default=None, description="The minimum required minds-v* tag the check compared against"
    )
    update_target_version: str | None = Field(
        default=None, description="The minds-v* tag the 'Update backup service' action would install"
    )
    check_detail: str = Field(default="", description="Extra human-readable check detail (e.g. why unverifiable)")
    is_verification_enabled: bool = Field(description="Whether backup verification is enabled for this workspace")


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
