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

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import LaunchMode


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


class AgentNotificationRequest(FrozenModel):
    """Body for sending a desktop notification on behalf of an agent."""

    message: str = Field(description="Notification body text")
    title: str | None = Field(default=None, description="Optional notification title")
    urgency: str | None = Field(default=None, description="One of: low, normal (default), critical")


class EstablishSshRequest(FrozenModel):
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


class CreateWorkspaceRequest(FrozenModel):
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


class PatchWorkspaceRequest(FrozenModel):
    """Body for partially updating a workspace's metadata."""

    color: str | None = Field(default=None, description="New hex color")
    account_id: str | None = Field(default=None, description="Account id to associate, or null/empty to disassociate")


class RestartWorkspaceRequest(FrozenModel):
    """Body for restarting a workspace's services or whole host."""

    scope: str = Field(description="'services' (restart system-services in place) or 'host' (bounce the host)")
    host_already_stopped: bool | None = Field(
        default=None, description="Skip the redundant stop step (host scope only) when the host is known stopped"
    )


class EnableSharingRequest(FrozenModel):
    """Body for enabling/updating Cloudflare sharing of a workspace service."""

    emails: tuple[str, ...] = Field(description="Emails allowed by the Cloudflare Access policy")


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
