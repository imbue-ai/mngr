import json
import tempfile
from collections.abc import Iterator
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import ServiceLogRecord
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthSession
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudSyncConflictCliError
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.primitives import ServiceName
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


class FakeImbueCloudCli(ImbueCloudCli):
    """In-memory test double for :class:`ImbueCloudCli`.

    Tests register accounts via :meth:`set_accounts` /
    :meth:`add_account`; only :meth:`auth_list` is exercised. Other
    subprocess-driven methods on the real CLI keep their default
    implementations and will spawn ``mngr imbue_cloud …`` if a test
    invokes them, so prefer narrower stubs when those paths matter.

    The ``mngr_caller`` defaults to a :class:`RecordingMngrCaller` (rather than
    the process-wide warm-process caller) so any code that reaches through
    ``cli.mngr_caller`` -- e.g. the tunnel-token injection in the sharing flow --
    is a fast in-memory no-op instead of spawning a real ``mngr`` process.
    """

    mngr_caller: MngrCaller = Field(default_factory=RecordingMngrCaller)
    accounts_to_return: list[ImbueCloudAuthAccount] = Field(default_factory=list)
    oauth_session_to_return: ImbueCloudAuthSession | None = Field(
        default=None, description="Session auth_oauth returns; raises ImbueCloudCliError when unset"
    )
    is_auth_list_failing: bool = Field(
        default=False,
        description="When True, auth_list raises ImbueCloudCliError (simulates a transient subprocess failure)",
    )

    def auth_list(self) -> list[ImbueCloudAuthAccount]:
        if self.is_auth_list_failing:
            raise ImbueCloudCliError("fake transient auth list failure")
        return list(self.accounts_to_return)

    def auth_oauth(
        self,
        account: str,
        provider_id: str,
        callback_port: int | None = None,
        no_browser: bool = False,
        success_redirect_url: str | None = None,
    ) -> ImbueCloudAuthSession:
        if self.oauth_session_to_return is None:
            raise ImbueCloudCliError("auth oauth: no fake OAuth session configured on FakeImbueCloudCli")
        return self.oauth_session_to_return

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
        default_factory=dict, description="email -> host_id -> wire record (the fake server state)"
    )
    sync_bundle_by_email: dict[str, dict[str, object]] = Field(
        default_factory=dict, description="email -> key bundle (the fake server state)"
    )
    is_sync_offline: bool = Field(default=False, description="When True, every sync call raises (connector down)")

    def _check_sync_online(self, command_repr: str) -> None:
        if self.is_sync_offline:
            raise ImbueCloudCliError(f"{command_repr}: connector unreachable (fake offline)")

    def sync_records_pull(self, account: str) -> list[dict[str, object]]:
        self._check_sync_online("sync records pull")
        return [dict(record) for record in self.sync_records_by_email.get(account, {}).values()]

    def sync_record_push(self, account: str, record: Mapping[str, object]) -> dict[str, object]:
        self._check_sync_online("sync records push")
        by_host = self.sync_records_by_email.setdefault(account, {})
        host_id = str(record["host_id"])
        existing = by_host.get(host_id)
        pushed_revision = int(str(record["revision"]))
        if existing is not None and pushed_revision != int(str(existing["revision"])) + 1:
            conflict = ImbueCloudSyncConflictCliError("sync records push: revision conflict")
            conflict.stored_record = dict(existing)
            raise conflict
        if str(record.get("state")) == "active":
            for other_host_id, other in by_host.items():
                is_other = other_host_id != host_id
                if is_other and other.get("agent_id") == record.get("agent_id") and other.get("state") == "active":
                    agent_conflict = ImbueCloudSyncConflictCliError("sync records push: active agent conflict")
                    agent_conflict.stored_record = None
                    raise agent_conflict
        stored = dict(record)
        by_host[host_id] = stored
        return dict(stored)

    def sync_record_delete(self, account: str, host_id: str) -> None:
        self._check_sync_online("sync records delete")
        self.sync_records_by_email.get(account, {}).pop(host_id, None)

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
    ) -> LiteLLMKeyMaterial:
        self.create_calls.append(
            {
                "account": account,
                "alias": alias,
                "max_budget": max_budget,
                "budget_duration": budget_duration,
                "metadata": dict(metadata) if metadata is not None else None,
            }
        )
        return LiteLLMKeyMaterial(
            key=SecretStr("sk-fake-litellm-key"),
            base_url=AnyUrl("https://litellm.example.com"),
        )


def make_fake_imbue_cloud_cli() -> FakeImbueCloudCli:
    """Build a :class:`FakeImbueCloudCli` rooted at a fresh ``ConcurrencyGroup``."""
    return FakeImbueCloudCli(
        connector_url=FAKE_CONNECTOR_URL,
    )


def make_session_store_for_test(data_dir: Path, cli: ImbueCloudCli | None = None) -> MultiAccountSessionStore:
    """Build a :class:`MultiAccountSessionStore` (with its record store) over a fake CLI by default."""
    effective_cli = cli or make_fake_imbue_cloud_cli()
    record_store = WorkspaceRecordStore(
        paths=WorkspacePaths(data_dir=data_dir),
        cli=effective_cli,
        device_id="device-test",
        device_label="test-device",
    )
    return MultiAccountSessionStore(data_dir=data_dir, cli=effective_cli, record_store=record_store)


@pytest.fixture
def root_concurrency_group() -> Iterator[ConcurrencyGroup]:
    """Root ``ConcurrencyGroup`` for tests that construct an ``AgentCreator``.

    ``AgentCreator.root_concurrency_group`` is required (in production it is
    owned by ``start_desktop_client`` and brackets the FastAPI lifespan); this
    fixture enters an equivalent group for the test's duration and exits it
    cleanly afterwards so any strand tracking / shutdown semantics match.
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


def make_service_log(service: str, url: str) -> str:
    """Build a single JSONL line matching the services/events.jsonl format."""
    return json.dumps({"service": service, "url": url}) + "\n"


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
            for record in records:
                if isinstance(record, ServiceLogRecord):
                    services[str(record.service)] = record.url
            resolver.update_services(AgentId(agent_id_str), services)

    return resolver
