"""Workspace AI-key minting: account resolution + LiteLLM key creation.

The workspace's in-UI Claude sign-in modal links to the desktop client's
mint page (GET /settings/ai-keys, handlers in ``app.py``) when the user
picks "Sign in with Imbue". The page is keyed by the workspace's mngr
**host id** (``?workspace=<host_id>``): the workspace knows its own host
id, and workspace records are keyed by it, so the owning account is
resolved from the workspace-record store (association IS record
existence). With no associated account the page errors and points the
user at the workspace's settings page to associate one.

Mint-only by design: one action mints a LiteLLM virtual key against the
owning account -- with the workspace host id baked into the key's alias
and metadata, so keys are attributable without any editable input -- and
produces an env-var-style credential blob (``ANTHROPIC_BASE_URL`` +
``ANTHROPIC_API_KEY``) for pasting back into the workspace modal. Key
listing/revocation stays a CLI concern
(``mngr imbue_cloud keys litellm list/delete``).
"""

from __future__ import annotations

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import MindError

# Budget defaults for workspace-minted keys, matching the historical
# per-create minting defaults: a rolling daily budget, not a lifetime cap.
_MINT_MAX_BUDGET_DOLLARS = 100.0
_MINT_BUDGET_DURATION = "1d"


class AiKeyMintError(MindError):
    """Raised when a workspace AI key cannot be minted."""


class ResolvedWorkspaceAccount(FrozenModel):
    """The owning account (and display name) resolved for a workspace host id."""

    user_id: str = Field(description="The owning account's user id")
    account_email: str = Field(description="The owning account's email")
    workspace_display_name: str = Field(description="The workspace's display name (falls back to the host id)")


def resolve_workspace_account(
    workspace_host_id: str,
    record_store: WorkspaceRecordStore | None,
    session_store: MultiAccountSessionStore | None,
) -> ResolvedWorkspaceAccount | None:
    """Resolve the account owning ``workspace_host_id`` from the record store.

    Association IS record existence: a workspace belongs to the account whose
    replica holds its ACTIVE record. Returns None when no account is
    associated (or the record store is unavailable).
    """
    if record_store is None or session_store is None:
        return None
    for user_id, records in record_store.list_all_records().items():
        for record in records:
            if record.host_id == workspace_host_id and record.state == RECORD_STATE_ACTIVE:
                account_email = session_store.get_account_email(user_id)
                if not account_email:
                    continue
                return ResolvedWorkspaceAccount(
                    user_id=user_id,
                    account_email=account_email,
                    workspace_display_name=record.display_name or workspace_host_id,
                )
    return None


@pure
def build_credential_blob(api_key: str, base_url: str) -> str:
    """Render the env-var-style blob the workspace modal's textarea expects."""
    return f"ANTHROPIC_BASE_URL={base_url}\nANTHROPIC_API_KEY={api_key}\n"


def mint_workspace_credential_blob(
    workspace_host_id: str,
    account_email: str,
    imbue_cloud_cli: ImbueCloudCli,
) -> str:
    """Mint a LiteLLM key for the workspace and return the paste-ready blob.

    The key's alias and metadata carry the workspace host id, fixed
    server-side -- there is deliberately no user-editable naming input.

    Raises AiKeyMintError when the plugin CLI rejects the mint.
    """
    try:
        key_material = imbue_cloud_cli.create_litellm_key(
            account=account_email,
            alias=f"workspace-{workspace_host_id}",
            max_budget=_MINT_MAX_BUDGET_DOLLARS,
            budget_duration=_MINT_BUDGET_DURATION,
            metadata={"workspace_host_id": workspace_host_id, "source": "ai-keys-page"},
        )
    except ImbueCloudCliError as exc:
        raise AiKeyMintError(f"Failed to create the key: {exc}") from exc
    logger.info("Minted LiteLLM key for workspace {} (account {})", workspace_host_id, account_email)
    return build_credential_blob(api_key=key_material.key.get_secret_value(), base_url=str(key_material.base_url))
