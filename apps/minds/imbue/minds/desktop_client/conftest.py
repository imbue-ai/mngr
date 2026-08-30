import base64
import json
import tempfile
from collections.abc import Iterator
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import AnyUrl
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import ServiceLogRecord
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthSession
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudLeaseActiveCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudSyncConflictCliError
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliRelayEndpoint
from imbue.minds.desktop_client.latchkey.permission_overview import clear_service_sign_in_options_cache
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
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
    :meth:`add_account`; only :meth:`auth_list` is exercised. Other
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
    is_auth_list_failing: bool = Field(
        default=False,
        description="When True, auth_list raises ImbueCloudCliError (simulates a transient subprocess failure)",
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

    def auth_resend_verification(self, account: str) -> bool:
        self.resent_verification_emails.append(account)
        return not self.is_resend_suppressed

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
        if self.login_session_to_return is None:
            raise ImbueCloudCliError("auth login: no fake login session configured on FakeImbueCloudCli")
        return self.login_session_to_return

    def set_accounts(self, accounts: list[ImbueCloudAuthAccount]) -> None:
        self.accounts_to_return = list(accounts)

    def add_account(
        self,
        user_id: str,
        email: str,
        display_name: str | None = None,
        is_active: bool = False,
    ) -> None:
        self.accounts_to_return.append(
            ImbueCloudAuthAccount(
                user_id=user_id,
                email=email,
                display_name=display_name,
                is_active=is_active,
            )
        )

    def remove_account(self, user_id: str) -> None:
        self.accounts_to_return = [a for a in self.accounts_to_return if a.user_id != user_id]

    # -- In-memory machine shares (drives the teardown tests) --

    def add_share(self, account: str, host_id: str) -> None:
        self.shares_by_account.setdefault(account, {})[host_id] = "active"

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

    def list_share_relays(self, *, account: str) -> dict[str, tuple[str, ...]]:
        return {region: tuple(endpoints) for region, endpoints in self.relays_to_return.items()}

    # -- In-memory storage-cleanup backend (drives the backup-trim tests) --

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

    # -- In-memory workspace-sync backend (mirrors the connector's semantics) --

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

    def sync_records_pull(self, account: str) -> list[dict[str, object]]:
        self._check_sync_online("sync records pull")
        return [dict(record) for record in self.sync_records_by_email.get(account, {}).values()]

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
        return scrubbed

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
        self.add_share(account, host_id)
        return ShareCliInfo(
            host_id=host_id,
            workspace_domain=f"{host_id}.owner1234.us1.shares.example",
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
    data_dir: Path, cli: ImbueCloudCli | None = None, mngr_host_dir: Path | None = None
) -> MultiAccountSessionStore:
    """Build a :class:`MultiAccountSessionStore` (with its record store) over a fake CLI by default.

    ``mngr_host_dir`` (default None: disabled) enables the identity cache's
    on-disk sessions-dir coherence check, exactly as the app wires it.
    """
    effective_cli = cli or make_fake_imbue_cloud_cli()
    record_store = WorkspaceRecordStore(
        paths=InstallationPaths(data_dir=data_dir),
        cli=effective_cli,
        device_id=device_id_for_test("session-store"),
        device_label="test-device",
    )
    return MultiAccountSessionStore(
        data_dir=data_dir, cli=effective_cli, record_store=record_store, mngr_host_dir=mngr_host_dir
    )


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
    """``NotificationDispatcher`` wired to the tkinter channel in tests.

    Tests generally do not exercise the dispatch path; this fixture just
    satisfies the required ``AgentCreator.notification_dispatcher`` field.
    Pass ``is_electron=False`` so no ``emit_event`` JSONL lines leak into the
    test's stdout. ``NotificationDispatcher.create`` skips tkinter setup when
    ``tkinter_module`` is ``None``, which is what we want for unit tests.
    """
    return NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False)


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


def make_service_log(service: str, url: str, label: str = "") -> str:
    """Build a single JSONL line matching the services/events.jsonl format.

    ``label`` is the service's origin label (``<name>-<rand>``); omit it for the
    legacy (label-less) shape.
    """
    entry: dict[str, str] = {"service": service, "url": url}
    if label:
        entry["label"] = label
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
            for record in records:
                if isinstance(record, ServiceLogRecord):
                    services[str(record.service)] = record.url
                    if record.label:
                        labels[str(record.service)] = record.label
            resolver.update_services(AgentId(agent_id_str), services, labels)

    return resolver


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
