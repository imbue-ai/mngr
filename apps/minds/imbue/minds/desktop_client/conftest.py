import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import ServiceLogRecord
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthAccount
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName

DEFAULT_SERVICE_NAME: ServiceName = ServiceName("web")


class FakeImbueCloudCli(ImbueCloudCli):
    """In-memory test double for :class:`ImbueCloudCli`.

    Tests register accounts via :meth:`set_accounts` /
    :meth:`add_account`; only :meth:`auth_list` is exercised. Other
    subprocess-driven methods on the real CLI keep their default
    implementations and will spawn ``mngr imbue_cloud …`` if a test
    invokes them, so prefer narrower stubs when those paths matter.
    """

    accounts_to_return: list[ImbueCloudAuthAccount] = Field(default_factory=list)

    def auth_list(self) -> list[ImbueCloudAuthAccount]:
        return list(self.accounts_to_return)

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


def make_fake_imbue_cloud_cli() -> FakeImbueCloudCli:
    """Build a :class:`FakeImbueCloudCli` rooted at a fresh ``ConcurrencyGroup``."""
    return FakeImbueCloudCli(parent_concurrency_group=ConcurrencyGroup(name="fake-imbue-cloud-cli"))


def make_session_store_for_test(data_dir: Path, cli: ImbueCloudCli | None = None) -> MultiAccountSessionStore:
    """Build a :class:`MultiAccountSessionStore` with a fake CLI by default."""
    return MultiAccountSessionStore(data_dir=data_dir, cli=cli or make_fake_imbue_cloud_cli())


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


def make_agents_json(*agent_ids: AgentId, labels: dict[str, str] | None = None) -> str:
    """Build a JSON string matching `mngr list --format json` output for the given agent IDs."""
    effective_labels = labels if labels is not None else {"workspace": "true", "is_primary": "true"}
    return json.dumps({"agents": [{"id": str(agent_id), "labels": effective_labels} for agent_id in agent_ids]})


def make_service_log(service: str, url: str) -> str:
    """Build a single JSONL line matching the services/events.jsonl format."""
    return json.dumps({"service": service, "url": url}) + "\n"


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
                host_id=HostId("host-00000000000000000000000000000000"),
                agent_id=AgentId(a["id"]),
                agent_name=AgentName(a.get("name", a["id"])),
                provider_name=ProviderInstanceName("local"),
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
