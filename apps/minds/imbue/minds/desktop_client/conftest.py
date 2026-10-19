import base64
import json
import tempfile
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import ServiceLogRecord
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.connections.account_lifecycle import list_signed_in_accounts_from_session_store
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.data_types import SignedInAccount
from imbue.minds.desktop_client.connections.primitives import CONNECTION_DOCUMENT_KIND
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.provider_settings import ProviderSettingsMaterializer
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import AuthRefreshOutcome
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthSession
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudLeaseActiveCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudSyncConflictCliError
from imbue.minds.desktop_client.imbue_cloud_cli import InvitationCliResult
from imbue.minds.desktop_client.imbue_cloud_cli import InvitationOutcomeCliEntry
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.imbue_cloud_cli import MachineSizeCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import MobileAccessLinkCliResult
from imbue.minds.desktop_client.imbue_cloud_cli import NotificationPreferencesCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliRelayEndpoint
from imbue.minds.desktop_client.imbue_cloud_cli import SyncRecordsPullResult
from imbue.minds.desktop_client.latchkey.permission_overview import clear_service_sign_in_options_cache
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.mock_local_prerequisites_test import FakeHostProbe
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.supertokens_routes import _web_login_flows
from imbue.minds.desktop_client.supertokens_routes import _web_login_flows_lock
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.primitives import ServiceName
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName

DEFAULT_SERVICE_NAME: ServiceName = ServiceName("web")

FAKE_CONNECTOR_URL: AnyUrl = AnyUrl("https://test--rsc-api.modal.run")

# The relay set the fake CLIs hand out for a share (one us1 relay, matching
# the workspace domains the fakes build).
TEST_RELAY_ENDPOINTS: tuple[ShareCliRelayEndpoint, ...] = (
    ShareCliRelayEndpoint(relay_id="relay-" + "1" * 16, endpoint="relay-us1.shares.example:7000"),
)


class FakeImbueCloudCli(ImbueCloudCli):
    """In-memory test double for :class:`ImbueCloudCli`.

    Tests register accounts via :meth:`set_accounts` /
    :meth:`add_account`; :meth:`auth_list` reads them and
    :meth:`auth_signout` drops one. Other
    subprocess-driven methods on the real CLI keep their default
    implementations and will spawn ``mngr imbue_cloud …`` if a test
    invokes them, so prefer narrower stubs when those paths matter.

    The ``mngr_caller`` defaults to a :class:`RecordingMngrCaller` (rather than
    the process-wide warm-process caller) so any code that reaches through
    ``cli.mngr_caller`` is a fast in-memory no-op instead of spawning a real
    ``mngr`` process.
    """

    # ``set_plan_error_to_raise`` holds an exception instance, which pydantic
    # cannot schema-generate; allow arbitrary types for this test double.
    model_config = ConfigDict(frozen=False, extra="forbid", arbitrary_types_allowed=True)

    mngr_caller: MngrCaller = Field(default_factory=RecordingMngrCaller)
    accounts_to_return: list[ImbueCloudAuthAccount] = Field(default_factory=list)
    login_session_to_return: ImbueCloudAuthSession | None = Field(
        default=None, description="Session auth_login returns; raises ImbueCloudCliError when unset"
    )
    login_url_to_write: str = Field(
        default="https://accounts.example.com/login?next=%2Faccounts%2Fauthorize",
        description="Sign-in URL auth_login writes to its url_file (the copy-the-link fallback)",
    )
    login_error_to_raise: Exception | None = Field(
        default=None, description="Error auth_login raises instead of returning a session, when set"
    )
    is_auth_list_failing: bool = Field(
        default=False,
        description="When True, auth_list raises ImbueCloudCliError (simulates a transient subprocess failure)",
    )
    is_auth_signout_failing: bool = Field(
        default=False, description="When True, auth_signout raises ImbueCloudCliError and the account stays signed in"
    )
    shares_by_account: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="account email -> {host_id: share state}"
    )
    deleted_share_host_ids: list[str] = Field(
        default_factory=list, description="Every host id delete_share was called with, in order"
    )
    is_share_lookup_failing: bool = Field(
        default=False,
        description="When True, get_share_status raises ImbueCloudCliError (simulates a connector hiccup)",
    )
    create_share_error_to_raise: ImbueCloudCliError | None = Field(
        default=None,
        description=(
            "Error create_share raises instead of creating a share, when set (an ImbueCloudQuotaExceededCliError "
            "plays the connector's structured quota refusal); honored by every create_share override"
        ),
    )
    share_details_by_host_id: dict[str, ShareCliInfo] = Field(
        default_factory=dict,
        description=(
            "Per host id, the full share row list_shares answers with (workspace id, stamps); a share "
            "without an entry here is answered in the canned get_share_status shape"
        ),
    )
    relays_to_return: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description=(
            "Relay map list_share_relays returns. Empty (the default) makes the "
            "latency-based region picker a no-op, so tests never probe the network."
        ),
    )

    resent_verification_emails: list[str] = Field(
        default_factory=list, description="Every email auth_resend_verification was called with, in order"
    )
    is_resend_suppressed: bool = Field(
        default=False, description="When True, auth_resend_verification reports the server cooldown (sent=False)"
    )
    verified_emails: set[str] = Field(
        default_factory=set, description="Accounts auth_is_email_verified reports as verified"
    )
    is_verified_checks: list[str] = Field(
        default_factory=list, description="Every email auth_is_email_verified was called with, in order"
    )
    is_verified_error_to_raise: ImbueCloudCliError | None = Field(
        default=None, description="When set, auth_is_email_verified raises it instead of answering"
    )
    set_plan_calls: list[tuple[str, str]] = Field(
        default_factory=list, description="(account email, plan) for every set_account_plan call, in order"
    )
    set_plan_error_to_raise: ImbueCloudCliError | None = Field(
        default=None, description="When set, set_account_plan raises this instead of switching"
    )

    def auth_list(self) -> list[ImbueCloudAuthAccount]:
        if self.is_auth_list_failing:
            raise ImbueCloudCliError("fake transient auth list failure")
        return list(self.accounts_to_return)

    auth_refresh_outcome_to_return: AuthRefreshOutcome = Field(
        default=AuthRefreshOutcome.REFRESHED, description="What auth_refresh_outcome reports for every account"
    )
    refreshed_accounts: list[str] = Field(
        default_factory=list, description="Every account auth_refresh_outcome was called with, in order"
    )
    signed_out_accounts: list[str] = Field(
        default_factory=list, description="Every account auth_signout was called with, in order"
    )

    def auth_refresh_outcome(self, account: str) -> AuthRefreshOutcome:
        self.refreshed_accounts.append(account)
        return self.auth_refresh_outcome_to_return

    def auth_signout(self, account: str) -> None:
        self.signed_out_accounts.append(account)
        if self.is_auth_signout_failing:
            raise ImbueCloudCliError("fake auth signout failure")
        self.remove_account_by_email(account)

    def remove_account_by_email(self, email: str) -> None:
        self.accounts_to_return = [a for a in self.accounts_to_return if a.email != email]

    def auth_resend_verification(self, account: str) -> bool:
        self.resent_verification_emails.append(account)
        return not self.is_resend_suppressed

    def auth_is_email_verified(self, account: str) -> bool:
        self.is_verified_checks.append(account)
        if self.is_verified_error_to_raise is not None:
            raise self.is_verified_error_to_raise
        return account in self.verified_emails

    def set_account_plan(self, account: str, plan: str) -> dict[str, Any]:
        self.set_plan_calls.append((account, plan))
        if self.set_plan_error_to_raise is not None:
            raise self.set_plan_error_to_raise
        return {"plan_name": plan}

    def auth_login(
        self,
        success_redirect_url: str | None = None,
        url_file: Path | None = None,
    ) -> ImbueCloudAuthSession:
        if url_file is not None:
            url_file.write_text(self.login_url_to_write + "\n")
        if self.login_error_to_raise is not None:
            raise self.login_error_to_raise
        session = self.login_session_to_return
        if session is None:
            raise ImbueCloudCliError("auth login: no fake login session configured on FakeImbueCloudCli")
        # The real plugin writes the session to its state dir (replacing any earlier one for the
        # same user), so the account is listed once from then on.
        self.remove_account(session.user_id)
        self.add_account(user_id=session.user_id, email=session.email, display_name=session.display_name)
        return session

    def set_accounts(self, accounts: list[ImbueCloudAuthAccount]) -> None:
        self.accounts_to_return = list(accounts)

    def add_account(
        self,
        user_id: str,
        email: str,
        display_name: str | None = None,
        is_active: bool = False,
        profile_picture_url: str | None = None,
    ) -> None:
        self.accounts_to_return.append(
            ImbueCloudAuthAccount(
                user_id=user_id,
                email=email,
                display_name=display_name,
                profile_picture_url=profile_picture_url,
                is_active=is_active,
            )
        )

    def remove_account(self, user_id: str) -> None:
        self.accounts_to_return = [a for a in self.accounts_to_return if a.user_id != user_id]

    # In-memory machine shares (drives the teardown tests)

    def add_share(self, account: str, host_id: str) -> None:
        self.shares_by_account.setdefault(account, {})[host_id] = "active"

    def _raise_create_share_error_if_configured(self) -> None:
        if self.create_share_error_to_raise is not None:
            raise self.create_share_error_to_raise

    def create_share(
        self,
        *,
        account: str,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareCliInfo:
        self._raise_create_share_error_if_configured()
        return super().create_share(
            account=account,
            host_id=host_id,
            entry_label=entry_label,
            preferred_region=preferred_region,
            workspace_id=workspace_id,
        )

    def get_share_status(self, *, account: str, host_id: str) -> ShareCliInfo | None:
        if self.is_share_lookup_failing:
            raise ImbueCloudCliError("fake transient share lookup failure")
        state = self.shares_by_account.get(account, {}).get(host_id)
        if state is None:
            return None
        return ShareCliInfo(
            host_id=host_id,
            workspace_domain=f"{host_id}.owner1234.us1.shares.example",
            region="us1",
            state=state,
            relay_endpoints=TEST_RELAY_ENDPOINTS,
        )

    def delete_share(self, *, account: str, host_id: str) -> None:
        self.deleted_share_host_ids.append(host_id)
        self.shares_by_account.get(account, {}).pop(host_id, None)

    def list_shares(self, *, account: str) -> list[ShareCliInfo]:
        if self.is_share_lookup_failing:
            raise ImbueCloudCliError("fake transient share listing failure")
        rows: list[ShareCliInfo] = []
        for host_id, state in self.shares_by_account.get(account, {}).items():
            detailed = self.share_details_by_host_id.get(host_id)
            if detailed is not None:
                rows.append(detailed.model_copy_update(to_update(detailed.field_ref().state, state)))
            else:
                status = self.get_share_status(account=account, host_id=host_id)
                assert status is not None
                rows.append(status)
        return rows

    def list_share_relays(self, *, account: str) -> dict[str, tuple[str, ...]]:
        return {region: tuple(endpoints) for region, endpoints in self.relays_to_return.items()}

    # The invitation surface (specs/inviting-granted-visitors/spec.md)

    pushed_grants_documents: list[tuple[str, str, dict[str, Any]]] = Field(
        default_factory=list, description="(account email, host id, document) for every push_share_grants call"
    )
    is_grants_push_failing: bool = Field(
        default=False, description="When True, push_share_grants raises ImbueCloudCliError (connector down)"
    )
    invite_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="Every invite_grantee call's arguments"
    )
    invite_results: list[InvitationCliResult | ImbueCloudCliError] = Field(
        default_factory=list,
        description="What invite_grantee answers, consumed in order (the last repeats); an error is raised",
    )
    invitation_outcomes_to_return: list[InvitationOutcomeCliEntry] = Field(
        default_factory=list, description="What list_invitation_outcomes answers"
    )
    invitation_outcomes_error_to_raise: ImbueCloudCliError | None = Field(
        default=None, description="When set, list_invitation_outcomes raises it"
    )
    notification_preferences_by_email: dict[str, NotificationPreferencesCliInfo] = Field(
        default_factory=dict, description="account email -> preferences (the fake server state; absent means defaults)"
    )
    is_notification_preferences_failing: bool = Field(
        default=False, description="When True, the notification preference calls raise ImbueCloudCliError"
    )

    def push_share_grants(self, *, account: str, host_id: str, document: Mapping[str, Any]) -> int:
        if self.is_grants_push_failing:
            raise ImbueCloudCliError("fake grants push failure")
        self.pushed_grants_documents.append((account, host_id, dict(document)))
        return 1

    def invite_grantee(
        self,
        *,
        account: str,
        host_id: str,
        user_id: str | None,
        email: str | None,
        app: str | None,
        link: str | None,
        workspace_name: str | None,
        app_display_name: str | None,
    ) -> InvitationCliResult:
        self.invite_calls.append(
            {
                "account": account,
                "host_id": host_id,
                "user_id": user_id,
                "email": email,
                "app": app,
                "link": link,
                "workspace_name": workspace_name,
                "app_display_name": app_display_name,
            }
        )
        if not self.invite_results:
            return InvitationCliResult(outcome="invited", invited_at="2026-09-30T12:00:00+00:00")
        result = self.invite_results.pop(0) if len(self.invite_results) > 1 else self.invite_results[0]
        if isinstance(result, ImbueCloudCliError):
            raise result
        return result

    mobile_access_link_calls: list[dict[str, Any]] = Field(
        default_factory=list, description="Every send_mobile_access_link call's arguments"
    )
    mobile_access_link_results: list[MobileAccessLinkCliResult | ImbueCloudCliError] = Field(
        default_factory=list,
        description="What send_mobile_access_link answers, consumed in order (the last repeats); an error is raised",
    )

    def send_mobile_access_link(
        self, *, account: str, host_id: str, link: str | None, workspace_name: str | None
    ) -> MobileAccessLinkCliResult:
        self.mobile_access_link_calls.append(
            {"account": account, "host_id": host_id, "link": link, "workspace_name": workspace_name}
        )
        if not self.mobile_access_link_results:
            return MobileAccessLinkCliResult(
                outcome="sent", recipient_email=account, sent_at="2026-09-30T12:00:00+00:00"
            )
        result = (
            self.mobile_access_link_results.pop(0)
            if len(self.mobile_access_link_results) > 1
            else self.mobile_access_link_results[0]
        )
        if isinstance(result, ImbueCloudCliError):
            raise result
        return result

    def list_invitation_outcomes(self, *, account: str, host_id: str) -> list[InvitationOutcomeCliEntry]:
        if self.invitation_outcomes_error_to_raise is not None:
            raise self.invitation_outcomes_error_to_raise
        return list(self.invitation_outcomes_to_return)

    def get_notification_preferences(self, *, account: str) -> NotificationPreferencesCliInfo:
        if self.is_notification_preferences_failing:
            raise ImbueCloudCliError("fake notification preferences failure")
        return self.notification_preferences_by_email.get(account, NotificationPreferencesCliInfo())

    def set_notification_preferences(
        self, *, account: str, email_enabled: bool, in_app_enabled: bool
    ) -> NotificationPreferencesCliInfo:
        if self.is_notification_preferences_failing:
            raise ImbueCloudCliError("fake notification preferences failure")
        info = NotificationPreferencesCliInfo(email_enabled=email_enabled, in_app_enabled=in_app_enabled)
        self.notification_preferences_by_email[account] = info
        return info

    # In-memory storage-cleanup backend (drives the backup-trim tests)

    storage_recheck_results: list[dict[str, object]] = Field(
        default_factory=list,
        description="Queue of recheck_storage results, consumed in order (the last entry repeats)",
    )
    cleanup_grant_result: dict[str, object] = Field(
        default_factory=dict, description="Result returned by create_storage_cleanup_grant"
    )
    cleanup_grant_call_count: int = Field(default=0, description="How many grants were requested")

    def recheck_storage(self, account: str) -> dict[str, object]:
        if not self.storage_recheck_results:
            raise ImbueCloudCliError("recheck storage: no fake results configured on FakeImbueCloudCli")
        if len(self.storage_recheck_results) > 1:
            return dict(self.storage_recheck_results.pop(0))
        return dict(self.storage_recheck_results[0])

    def create_storage_cleanup_grant(self, account: str) -> dict[str, object]:
        self.cleanup_grant_call_count += 1
        return dict(self.cleanup_grant_result)

    # In-memory machine listing (drives the stop-kind tracker)

    machines: list[MachineSizeCliInfo] = Field(
        default_factory=list, description="The machines list_machines and show_machine answer from, every account"
    )
    is_machine_listing_failing: bool = Field(
        default=False, description="When True, list_machines raises ImbueCloudCliError (connector unreachable)"
    )
    machine_list_call_count: int = Field(default=0, description="How many times list_machines was called")
    machine_show_call_count: int = Field(default=0, description="How many times show_machine was called")

    def list_machines(self, account: str) -> list[MachineSizeCliInfo]:
        self.machine_list_call_count += 1
        if self.is_machine_listing_failing:
            raise ImbueCloudCliError("machines show: connector unreachable (fake)")
        return list(self.machines)

    def show_machine(self, account: str, machine_ref: str) -> MachineSizeCliInfo | None:
        self.machine_show_call_count += 1
        return next((machine for machine in self.machines if machine.host_id == machine_ref), None)

    resize_calls: list[tuple[str, str, int | None, int | None]] = Field(
        default_factory=list, description="Every resize_machine call: (account, machine ref, units, disk GB)"
    )
    resize_error_to_raise: Exception | None = Field(
        default=None, description="Error resize_machine raises instead of recording the targets, when set"
    )

    def resize_machine(
        self, account: str, machine_ref: str, target_memory_units: int | None, target_disk_gb: int | None
    ) -> MachineSizeCliInfo:
        self.resize_calls.append((account, machine_ref, target_memory_units, target_disk_gb))
        if self.resize_error_to_raise is not None:
            raise self.resize_error_to_raise
        machine = next((entry for entry in self.machines if entry.host_id == machine_ref), None)
        if machine is None:
            raise ImbueCloudCliError(f"machines resize: no machine matches {machine_ref!r} (fake)")
        # The connector's semantics: a target equal to the current size clears that dimension.
        recorded = machine.model_copy_update(
            to_update(
                machine.field_ref().target_memory_units,
                None
                if target_memory_units is None or target_memory_units == machine.memory_units
                else target_memory_units,
            ),
            to_update(
                machine.field_ref().target_disk_gb,
                None if target_disk_gb is None or target_disk_gb == machine.disk_gb else target_disk_gb,
            ),
        )
        stamped = recorded.model_copy_update(
            to_update(
                recorded.field_ref().is_restart_needed_to_apply,
                recorded.target_memory_units is not None or recorded.target_disk_gb is not None,
            )
        )
        self.machines = [stamped if entry.host_id == machine_ref else entry for entry in self.machines]
        return stamped

    account_info_by_email: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="The raw `account show` document get_account_info answers per account email"
    )

    def get_account_info(self, account: str) -> dict[str, Any]:
        info = self.account_info_by_email.get(account)
        if info is None:
            raise ImbueCloudCliError(f"account show: connector unreachable for {account} (fake)")
        return info

    # In-memory workspace-sync backend (mirrors the connector's semantics)

    sync_records_by_email: dict[str, dict[str, dict[str, object]]] = Field(
        default_factory=dict, description="email -> workspace id -> wire record (the fake server state)"
    )
    sync_bundle_by_email: dict[str, dict[str, object]] = Field(
        default_factory=dict, description="email -> key bundle (the fake server state)"
    )
    is_sync_offline: bool = Field(default=False, description="When True, every sync call raises (connector down)")
    lease_holding_workspace_ids: set[str] = Field(
        default_factory=set,
        description="Workspace ids whose record delete the fake connector refuses with lease_active (a live lease)",
    )

    def _check_sync_online(self, command_repr: str) -> None:
        if self.is_sync_offline:
            raise ImbueCloudCliError(f"{command_repr}: connector unreachable (fake offline)")

    shared_agent_ids_by_email: dict[str, set[str]] = Field(
        default_factory=dict, description="email -> agent ids the fake connector's shares index lists as shared"
    )

    def sync_records_pull(self, account: str) -> SyncRecordsPullResult:
        self._check_sync_online("sync records pull")
        return SyncRecordsPullResult(
            records=tuple(dict(record) for record in self.sync_records_by_email.get(account, {}).values()),
            shared_agent_ids=tuple(sorted(self.shared_agent_ids_by_email.get(account, set()))),
        )

    def sync_record_push(self, account: str, record: Mapping[str, object]) -> dict[str, object]:
        self._check_sync_online("sync records push")
        # Mirrors the workspace-keyed connector: one row per workspace id,
        # host_id is a mutable attribute, CAS on the row's revision.
        by_workspace = self.sync_records_by_email.setdefault(account, {})
        workspace_id = str(record["agent_id"])
        existing = by_workspace.get(workspace_id)
        pushed_revision = int(str(record["revision"]))
        if existing is not None and pushed_revision != int(str(existing["revision"])) + 1:
            conflict = ImbueCloudSyncConflictCliError("sync records push: revision conflict")
            conflict.stored_record = dict(existing)
            raise conflict
        stored = dict(record)
        by_workspace[workspace_id] = stored
        return dict(stored)

    def sync_record_delete(self, account: str, record_id: str) -> None:
        self._check_sync_online("sync records delete")
        if record_id in self.lease_holding_workspace_ids:
            raise ImbueCloudLeaseActiveCliError("sync records delete: the workspace still holds its cloud lease")
        by_workspace = self.sync_records_by_email.get(account, {})
        if record_id in by_workspace:
            del by_workspace[record_id]
            return
        # Legacy host-id addressing resolves through the row's host column.
        for workspace_id, record in list(by_workspace.items()):
            if record.get("host_id") == record_id:
                del by_workspace[workspace_id]
                return

    def sync_scrub_secrets(self, account: str) -> int:
        self._check_sync_online("sync scrub-secrets")
        scrubbed = 0
        for record in self.sync_records_by_email.get(account, {}).values():
            if record.get("encrypted_secrets") is not None:
                record["encrypted_secrets"] = None
                scrubbed += 1
        for by_doc_id in self.sync_documents_by_email.get(account, {}).values():
            for document in by_doc_id.values():
                document["encrypted_payload"] = None
        return scrubbed

    # In-memory account documents (mirrors the connector's /sync/documents semantics)

    sync_documents_by_email: dict[str, dict[str, dict[str, dict[str, Any]]]] = Field(
        default_factory=dict, description="email -> kind -> doc_id -> wire document (the fake server state)"
    )
    document_push_count: int = Field(default=0, description="How many document pushes reached the fake server")
    on_sync_documents_pull: Callable[[str], None] | None = Field(
        default=None,
        description=(
            "Invoked with the account email on every document pull, before it answers: a test's hook for "
            "something happening (a signout) while a sync pass is mid-pull"
        ),
    )

    def seed_connection_document(
        self,
        account: str,
        doc_id: str,
        metadata: Mapping[str, Any],
        encrypted_payload: str | None = None,
        revision: int = 1,
    ) -> None:
        """Place an ACTIVE connection document on the fake server, as another device would have pushed it."""
        by_doc_id = self.sync_documents_by_email.setdefault(account, {}).setdefault(CONNECTION_DOCUMENT_KIND, {})
        by_doc_id[doc_id] = {
            "kind": CONNECTION_DOCUMENT_KIND,
            "doc_id": doc_id,
            "metadata": dict(metadata),
            "encrypted_payload": encrypted_payload,
            "state": "active",
            "revision": revision,
            "document_format": 1,
        }

    def sync_documents_pull(self, account: str, kind: str) -> list[dict[str, Any]]:
        self._check_sync_online("sync documents pull")
        if self.on_sync_documents_pull is not None:
            self.on_sync_documents_pull(account)
        by_doc_id = self.sync_documents_by_email.get(account, {}).get(kind, {})
        return [dict(document) for document in by_doc_id.values()]

    def sync_document_push(self, account: str, document: Mapping[str, Any]) -> dict[str, Any]:
        self._check_sync_online("sync documents push")
        self.document_push_count += 1
        kind = str(document["kind"])
        doc_id = str(document["doc_id"])
        by_doc_id = self.sync_documents_by_email.setdefault(account, {}).setdefault(kind, {})
        existing = by_doc_id.get(doc_id)
        pushed_revision = int(str(document["revision"]))
        expected_revision = 1 if existing is None else int(str(existing["revision"])) + 1
        if pushed_revision != expected_revision:
            conflict = ImbueCloudSyncConflictCliError("sync documents push: revision conflict")
            conflict.stored_record = dict(existing) if existing is not None else None
            raise conflict
        if existing is not None and int(str(document.get("document_format", 1))) < int(
            str(existing.get("document_format", 1))
        ):
            too_new = ImbueCloudCliError("sync documents push failed: document_format_too_new")
            too_new.stderr = '{"error": "document format too new", "error_class": "ImbueCloudRecordFormatTooNewError"}'
            raise too_new
        # The payload is preserved when absent from the push and cleared by an explicit null.
        stored: dict[str, Any] = dict(existing) if existing is not None else {}
        stored.update(dict(document))
        previous_state = existing.get("state") if existing is not None else None
        if stored.get("state") == "deleted":
            if previous_state != "deleted":
                stored["deleted_at"] = "2026-01-01T00:00:00+00:00"
        else:
            stored["deleted_at"] = None
        by_doc_id[doc_id] = stored
        return dict(stored)

    def tombstone_connection_document(self, account: str, doc_id: str) -> None:
        """Tombstone a connection document on the fake server, as another device's removal would have."""
        stored = self.sync_documents_by_email[account][CONNECTION_DOCUMENT_KIND][doc_id]
        stored["state"] = "deleted"
        stored["revision"] = int(str(stored["revision"])) + 1
        stored["encrypted_payload"] = None
        stored["deleted_at"] = "2026-01-01T00:00:00+00:00"

    def sync_bundle_pull(self, account: str) -> dict[str, object] | None:
        self._check_sync_online("sync bundle pull")
        bundle = self.sync_bundle_by_email.get(account)
        return dict(bundle) if bundle is not None else None

    def sync_bundle_push(self, account: str, bundle: Mapping[str, object]) -> None:
        self._check_sync_online("sync bundle push")
        self.sync_bundle_by_email[account] = dict(bundle)

    def sync_bundle_delete(self, account: str) -> None:
        self._check_sync_online("sync bundle delete")
        self.sync_bundle_by_email.pop(account, None)


class SucceedingCreateShareCli(FakeImbueCloudCli):
    """A ``create_share`` returning real relay coordinates, so the full client-side bring-up runs.

    The returned share carries a relay endpoint + token, letting the share
    flow continue through share-env rendering and materials injection (the
    default ``RecordingMngrCaller`` records the exec writes). Every call is
    recorded for seam assertions.
    """

    create_share_calls: list[tuple[str, str, str | None, str | None, str | None]] = Field(
        default_factory=list,
        description=(
            "(account email, host id, entry label, preferred region, workspace id) for every create_share call, in order"
        ),
    )
    chrome_origin_to_return: str | None = Field(
        default=None,
        description=(
            "chrome_origin the returned share carries; None (the default) simulates a connector "
            "that predates the field or a tier with no hosted chrome configured"
        ),
    )

    created_workspace_domain_to_return: str | None = Field(
        default=None,
        description="workspace_domain the created share carries; None uses the canned <host>.owner1234 shape",
    )

    def create_share(
        self,
        *,
        account: str,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareCliInfo:
        self.create_share_calls.append((account, host_id, entry_label, preferred_region, workspace_id))
        self._raise_create_share_error_if_configured()
        self.add_share(account, host_id)
        return ShareCliInfo(
            host_id=host_id,
            workspace_domain=self.created_workspace_domain_to_return or f"{host_id}.owner1234.us1.shares.example",
            region="us1",
            state="active",
            relay_endpoints=TEST_RELAY_ENDPOINTS,
            relay_token=SecretStr("relay-token-xyz"),
            chrome_origin=self.chrome_origin_to_return,
        )


class RecordingImbueCloudCli(FakeImbueCloudCli):
    """``FakeImbueCloudCli`` that records ``create_litellm_key`` calls.

    Returns a stub :class:`LiteLLMKeyMaterial` instead of spawning the real
    ``mngr imbue_cloud keys litellm create`` subprocess so tests can run
    fully offline.
    """

    create_calls: list[dict[str, object]] = Field(default_factory=list)

    def create_litellm_key(
        self,
        *,
        account: str,
        alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        metadata: Mapping[str, str] | None = None,
        is_rotate_on_exists: bool = False,
    ) -> LiteLLMKeyMaterial:
        self.create_calls.append(
            {
                "account": account,
                "alias": alias,
                "max_budget": max_budget,
                "budget_duration": budget_duration,
                "metadata": dict(metadata) if metadata is not None else None,
                "is_rotate_on_exists": is_rotate_on_exists,
            }
        )
        return LiteLLMKeyMaterial(
            key=SecretStr("sk-fake-litellm-key"),
            base_url=AnyUrl("https://litellm.example.com"),
        )


def make_share_probe_result(
    is_gateway_present: bool = True,
    is_share_env_present: bool = False,
    grants_toml_text: str | None = None,
) -> MngrCallResult:
    """A canned ``mngr exec --format json`` result answering the share state probe.

    Tests hand this to a :class:`RecordingMngrCaller` so the enable flow's
    one-exec probe (``probe_share_state_in_agent``) parses a realistic
    envelope. The same result is returned for every later call too, which is
    harmless: the write exec only inspects the returncode.
    """
    grants_line = (
        "MNGR_SHARE_GRANTS_B64=" + base64.b64encode(grants_toml_text.encode()).decode("ascii")
        if grants_toml_text is not None
        else "MNGR_SHARE_GRANTS_B64=ABSENT"
    )
    stdout = "\n".join(
        [
            f"MNGR_SHARE_GATEWAY={1 if is_gateway_present else 0}",
            f"MNGR_SHARE_ENV={1 if is_share_env_present else 0}",
            grants_line,
        ]
    )
    envelope = {"results": [{"agent": "probe", "stdout": stdout, "stderr": "", "success": True}]}
    return MngrCallResult(returncode=0, stdout=json.dumps(envelope))


def make_fake_imbue_cloud_cli() -> FakeImbueCloudCli:
    """Build a :class:`FakeImbueCloudCli` rooted at a fresh ``ConcurrencyGroup``."""
    return FakeImbueCloudCli(
        connector_url=FAKE_CONNECTOR_URL,
    )


def make_session_store_for_test(
    data_dir: Path,
    cli: ImbueCloudCli | None = None,
    mngr_host_dir: Path | None = None,
    connection_store: ConnectionStore | None = None,
) -> MultiAccountSessionStore:
    """Build a :class:`MultiAccountSessionStore` (with its record store) over a fake CLI by default.

    ``mngr_host_dir`` (default None: disabled) enables the identity cache's
    on-disk sessions-dir coherence check, exactly as the app wires it;
    ``connection_store`` (default None) lets the record store classify
    connection rows, as the app wires it.
    """
    effective_cli = cli or make_fake_imbue_cloud_cli()
    record_store = WorkspaceRecordStore(
        paths=InstallationPaths(data_dir=data_dir),
        cli=effective_cli,
        device_id=device_id_for_test("session-store"),
        device_label="test-device",
        connection_store=connection_store,
    )
    return MultiAccountSessionStore(
        data_dir=data_dir, cli=effective_cli, record_store=record_store, mngr_host_dir=mngr_host_dir
    )


def associate_hosted_workspace(
    store: MultiAccountSessionStore,
    agent_id: AgentId,
    resolver: BackendResolverInterface,
    user_id: str,
    display_name: str = "alpha",
) -> str:
    """Associate the workspace ``resolver`` discovers as ``agent_id`` with ``user_id``'s account; returns its host id.

    The setup of a workspace this device hosts under a signed-in account,
    which the sharing paths resolve back to the account through the host id.
    """
    display_info = resolver.get_agent_display_info(agent_id)
    assert display_info is not None
    host_id = str(display_info.host_id)
    store.associate_created_workspace(
        user_id=user_id,
        agent_id=str(agent_id),
        host_id=host_id,
        display_name=display_name,
        color=None,
        is_cloud_row=False,
    )
    return host_id


def make_profiled_device_with_connections_for_test(
    base: Path, name: str, cli: FakeImbueCloudCli
) -> tuple[InstallationPaths, WorkspaceRecordStore, MultiAccountSessionStore, ConnectionStore, Path]:
    """A device (paths, record store, session store, connection store) whose mngr profile dir exists, plus that
    profile dir.

    Host access material collection and materialization need the profile dir;
    the per-host key of a cloud machine lives under it. The connection store is
    wired into the record store, so rows on the device's connections resolve.
    """
    paths = InstallationPaths(data_dir=base / name)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    mngr_host_dir = base / name / "mngr"
    profile_id = uuid4().hex
    profile_dir = mngr_host_dir / "profiles" / profile_id
    profile_dir.mkdir(parents=True)
    (mngr_host_dir / "config.toml").write_text(f'profile = "{profile_id}"\n')
    connection_store = ConnectionStore(paths=paths, cli=cli, device_label=name)
    record_store = WorkspaceRecordStore(
        paths=paths,
        mngr_host_dir=mngr_host_dir,
        cli=cli,
        device_id=device_id_for_test(name),
        device_label=name,
        connection_store=connection_store,
    )
    session_store = MultiAccountSessionStore(data_dir=paths.data_dir, cli=cli, record_store=record_store)
    return paths, record_store, session_store, connection_store, profile_dir


def make_profiled_device_for_test(
    base: Path, name: str, cli: FakeImbueCloudCli
) -> tuple[InstallationPaths, WorkspaceRecordStore, MultiAccountSessionStore, Path]:
    """:func:`make_profiled_device_with_connections_for_test` for tests that never touch the connection store."""
    paths, record_store, session_store, _, profile_dir = make_profiled_device_with_connections_for_test(
        base, name, cli
    )
    return paths, record_store, session_store, profile_dir


def make_connection_store_for_test(data_dir: Path, cli: ImbueCloudCli | None = None) -> ConnectionStore:
    """Build a :class:`ConnectionStore` over a fake CLI by default."""
    return ConnectionStore(
        paths=InstallationPaths(data_dir=data_dir),
        cli=cli or make_fake_imbue_cloud_cli(),
        device_label="test-device",
    )


def unlock_account_with_master_password_for_test(paths: InstallationPaths, user_id: str) -> None:
    """Make ``user_id`` an unlocked account with a master password set: the state a synced connection needs."""
    ensure_dek(paths, user_id)
    set_master_password_for_account(paths, user_id, SecretStr("pw"))


def add_modal_connection_for_test(
    store: ConnectionStore,
    owner_user_id: str,
    alias: str,
    *,
    connection_id: ConnectionId | None = None,
    token_id: str = "ak",
    token_secret: str = "as",
) -> ConnectionId:
    """Add a Modal connection with fake credentials under ``owner_user_id``; returns its id.

    A test that snapshots the materialized block pins ``connection_id`` (it names
    the instance) and the token it expects to see written.
    """
    connection_id = connection_id or ConnectionId.generate()
    store.add_connection(
        owner_user_id,
        connection_id,
        ConnectionBackend.MODAL,
        alias,
        f"{token_id}…",
        {},
        ConnectionCredentials(token_id=token_id, token_secret=SecretStr(token_secret)),
    )
    return connection_id


def make_provider_materializer_for_test(
    root_name: str,
    connection_store: ConnectionStore,
    list_accounts: Callable[[], Sequence[SignedInAccount] | None],
    connector_url: str = str(FAKE_CONNECTOR_URL),
) -> ProviderSettingsMaterializer:
    """Build a :class:`ProviderSettingsMaterializer` with no discovery bounce (tests assert on the file).

    ``list_accounts`` is the account source: a fixed list for a pure
    materializer test, or :func:`session_store_account_source` for a device
    whose signed-in accounts the fake plugin tracks.
    """
    return ProviderSettingsMaterializer(
        root_name=root_name,
        connection_store=connection_store,
        connector_url=connector_url.rstrip("/"),
        list_accounts=list_accounts,
    )


def session_store_account_source(
    session_store: MultiAccountSessionStore,
) -> Callable[[], Sequence[SignedInAccount] | None]:
    """The materializer's production account source over a session store (None when the listing failed)."""
    return lambda: list_signed_in_accounts_from_session_store(session_store)


def build_desktop_client_for_test(
    tmp_path: Path,
    is_authenticated: bool,
    backend_resolver: BackendResolverInterface | None = None,
    **create_kwargs: Any,
) -> tuple[FlaskClient, Flask, FileAuthStore]:
    """Build a desktop-client Flask app over a fresh ``FileAuthStore`` and return its test client.

    ``backend_resolver`` defaults to a bare ``MngrCliBackendResolver``; any extra
    keyword arguments are forwarded to ``create_desktop_client``. When
    ``is_authenticated`` is True the returned client already carries a valid
    session cookie signed with the auth store's key.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    effective_resolver = backend_resolver if backend_resolver is not None else MngrCliBackendResolver()
    # A described machine rather than the real one: the create form's defaults
    # probe the local backends, and a real probe would run docker in a test.
    create_kwargs.setdefault("host_probe", FakeHostProbe())
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=effective_resolver,
        http_client=None,
        **create_kwargs,
    )
    client = app.test_client()
    if is_authenticated:
        client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client, app, auth_store


def build_desktop_client_with_accounts(
    tmp_path: Path,
    signed_in_user_ids: tuple[str, ...],
    stored_default_account_id: str,
    cli: FakeImbueCloudCli | None = None,
) -> tuple[FlaskClient, MindsConfig]:
    """An authenticated desktop client whose config stores ``stored_default_account_id`` as the default account.

    Each of ``signed_in_user_ids`` is signed in (as ``<user_id>@example.com``)
    on ``cli``, a fresh fake by default. Returns the client and its config.
    """
    effective_cli = cli or make_fake_imbue_cloud_cli()
    for user_id in signed_in_user_ids:
        effective_cli.add_account(user_id=user_id, email=f"{user_id}@example.com")
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    minds_config.set_default_account_id(stored_default_account_id)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        imbue_cloud_cli=effective_cli,
        session_store=make_session_store_for_test(tmp_path / "session-store", effective_cli),
        minds_config=minds_config,
    )
    return client, minds_config


@pytest.fixture
def agent_id() -> AgentId:
    """A fresh workspace agent id, for tests that need exactly one."""
    return AgentId.generate()


@pytest.fixture
def root_concurrency_group() -> Iterator[ConcurrencyGroup]:
    """Root ``ConcurrencyGroup`` for tests that construct something requiring one.

    Several components take it as a required field -- ``AgentCreator``,
    ``ConnectivityDetector``, ``WorkspaceViewRefresher`` -- and in production all
    of them are handed the one group ``start_desktop_client`` owns, which
    brackets the app's lifespan. This fixture enters an equivalent group for the
    test's duration and exits it cleanly afterwards so any strand tracking /
    shutdown semantics match.
    """
    cg = ConcurrencyGroup(name="test-root")
    with cg:
        yield cg


@pytest.fixture
def notification_dispatcher() -> NotificationDispatcher:
    """A ``NotificationDispatcher`` outside Electron, so no ``emit_event`` JSONL lines leak into the test's stdout."""
    return NotificationDispatcher(is_electron=False)


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """Temporary directory with a short path, for use with AF_UNIX sockets.

    pytest's tmp_path embeds the test function name, which can push Unix socket
    paths over the 104-char limit on macOS. This fixture uses a short prefix
    directly in the system tmpdir instead.
    """
    with tempfile.TemporaryDirectory(prefix="ssh") as d:
        yield Path(d)


_FIXED_TEST_HOST_ID: str = "host-00000000000000000000000000000000"


def make_agents_json(*agent_ids: AgentId, labels: dict[str, str] | None = None, host_name: str | None = None) -> str:
    """Build a JSON string matching `mngr list --format json` output for the given agent IDs.

    When ``host_name`` is given, each agent carries a ``host`` object with that
    name so the resolver's ``host_name_by_host_id`` (the canonical host-name
    source) is populated, mirroring real discovery output.
    """
    effective_labels = labels if labels is not None else {"is_primary": "true"}

    def _agent(agent_id: AgentId) -> dict[str, object]:
        entry: dict[str, object] = {"id": str(agent_id), "labels": effective_labels}
        if host_name is not None:
            entry["host"] = {"id": _FIXED_TEST_HOST_ID, "name": host_name}
        return entry

    return json.dumps({"agents": [_agent(agent_id) for agent_id in agent_ids]})


def make_service_log(service: str, url: str, label: str = "", display_name: str = "") -> str:
    """Build a single JSONL line matching the services/events.jsonl format.

    ``label`` is the service's origin label (``<name>-<rand>``); omit it for the
    legacy (label-less) shape. ``display_name`` is what users read for the
    service; omit it for a service registered without a manifest.
    """
    entry: dict[str, str] = {"service": service, "url": url}
    if label:
        entry["label"] = label
    if display_name:
        entry["display_name"] = display_name
    return json.dumps(entry) + "\n"


def seed_provider_snapshots(
    resolver: MngrCliBackendResolver,
    providers: tuple[DiscoveredProvider, ...] = (),
    error_by_provider_name: Mapping[ProviderInstanceName, DiscoveryError] | None = None,
    last_snapshot_at: datetime | None = None,
) -> None:
    """Feed per-provider discovery snapshots into ``resolver`` via its per-provider merge API.

    Convenience for tests that previously seeded provider state through the old
    global ``update_providers`` in a single call: it fans the healthy providers
    and the errored-provider entries into one ``update_providers`` call each,
    every entry stamped with ``last_snapshot_at`` (defaulting to now). A real
    provider snapshot carries either a constructed provider or an error, so the
    two groups are kept distinct here.
    """
    snapshot_at = last_snapshot_at if last_snapshot_at is not None else datetime.now(timezone.utc)
    for provider in providers:
        resolver.update_providers(
            provider_name=provider.provider_name, provider=provider, error=None, last_snapshot_at=snapshot_at
        )
    for provider_name, error in (error_by_provider_name or {}).items():
        resolver.update_providers(
            provider_name=provider_name, provider=None, error=error, last_snapshot_at=snapshot_at
        )


def make_resolver_with_data(
    agents_json: str | None = None,
    service_logs: dict[str, str] | None = None,
) -> MngrCliBackendResolver:
    """Create a MngrCliBackendResolver pre-populated with test data.

    agents_json is a JSON string matching `mngr list --format json` format, used to populate
    agent IDs and SSH info. service_logs is a mapping of agent ID string to raw
    services/events.jsonl content, parsed to populate the service URL map for each agent.
    """
    resolver = MngrCliBackendResolver()

    if agents_json is not None:
        parsed = parse_agents_from_json(agents_json)
        # Build DiscoveredAgent objects from the JSON for list_known_workspace_ids()
        raw = json.loads(agents_json)
        discovered = tuple(
            DiscoveredAgent(
                # Honor a per-agent host id when the test data provides one so
                # multi-workspace tests get distinct hosts; else the fixed id.
                host_id=HostId(a.get("host", {}).get("id", _FIXED_TEST_HOST_ID)),
                agent_id=AgentId(a["id"]),
                agent_name=AgentName(a.get("name", a["id"])),
                # Honor a per-agent provider instance name (e.g. an imbue_cloud
                # account instance for cloud-row tests); else the local default.
                provider_name=ProviderInstanceName(a.get("provider", "local")),
                certified_data={"labels": a.get("labels", {})},
            )
            for a in raw.get("agents", [])
            if "id" in a
        )
        resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=parsed.agent_ids,
                discovered_agents=discovered,
                ssh_info_by_agent_id=parsed.ssh_info_by_agent_id,
                host_name_by_host_id=parsed.host_name_by_host_id,
            )
        )

    if service_logs:
        for agent_id_str, log_content in service_logs.items():
            records = parse_service_log_records(log_content)
            services: dict[str, str] = {}
            labels: dict[str, str] = {}
            display_names: dict[str, str] = {}
            for record in records:
                if isinstance(record, ServiceLogRecord):
                    services[str(record.service)] = record.url
                    if record.label:
                        labels[str(record.service)] = record.label
                    if record.display_name:
                        display_names[str(record.service)] = record.display_name
            resolver.update_services(AgentId(agent_id_str), services, labels, display_names=display_names)

    return resolver


@pytest.fixture(autouse=True)
def _forget_web_login_flows() -> Iterator[None]:
    """Start each test with no browser sign-in flows tracked.

    The flow table is process-wide, and a new sign-in joins any flow still
    running, so a flow one test leaves running would be joined by the next.
    """
    with _web_login_flows_lock:
        _web_login_flows.clear()
    yield
    with _web_login_flows_lock:
        _web_login_flows.clear()


@pytest.fixture(autouse=True)
def _forget_service_sign_in_options() -> Iterator[None]:
    """Keep each test's latchkey double out of the next test's sign-in cache.

    How a service connects is remembered for the life of the process, since it
    is a property of the latchkey binary. Tests swap that binary for a double
    per test, so the memo has to go with it.
    """
    clear_service_sign_in_options_cache()
    yield
    clear_service_sign_in_options_cache()
