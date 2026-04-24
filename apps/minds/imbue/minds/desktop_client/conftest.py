import json
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from typing import Final

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import ServiceLogRecord
from imbue.minds.desktop_client.backend_resolver import parse_agents_from_json
from imbue.minds.desktop_client.backend_resolver import parse_service_log_records
from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName

DEFAULT_SERVICE_NAME: ServiceName = ServiceName("web")


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


_FAKE_LEASE_RESPONSE: Final[dict[str, object]] = {
    "host_db_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
    "vps_ip": "203.0.113.10",
    "ssh_port": 22,
    "ssh_user": "root",
    "container_ssh_port": 2222,
    "agent_id": "agent-abc12300000000000000000000000000",
    "host_id": "host-def45600000000000000000000000000",
    "version": "v0.1.0",
}


class _FakePoolHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that returns canned responses for pool endpoints."""

    def do_POST(self) -> None:
        if self.path == "/hosts/lease":
            self._respond(200, _FAKE_LEASE_RESPONSE)
        elif self.path.endswith("/release"):
            self._respond(200, {"status": "released"})
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self) -> None:
        if self.path == "/hosts":
            self._respond(200, [dict(_FAKE_LEASE_RESPONSE, leased_at="2026-01-01T00:00:00Z")])
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: object) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture()
def fake_pool_server() -> Iterator[HostPoolClient]:
    """Start a local HTTP server and return a HostPoolClient pointing to it."""
    server = HTTPServer(("127.0.0.1", 0), _FakePoolHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = HostPoolClient(
        connector_url=RemoteServiceConnectorUrl("http://127.0.0.1:{}".format(port)),
    )
    yield client
    server.shutdown()


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
