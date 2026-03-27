import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.minds.forwarding_server.backend_resolver import MngrCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import ParsedAgentsResult
from imbue.minds.forwarding_server.backend_resolver import parse_agents_from_json
from imbue.minds.forwarding_server.backend_resolver import parse_server_log_records
from imbue.minds.primitives import ServerName
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName

DEFAULT_SERVER_NAME: ServerName = ServerName("web")


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
    effective_labels = labels if labels is not None else {"mind": "true"}
    return json.dumps({"agents": [{"id": str(agent_id), "labels": effective_labels} for agent_id in agent_ids]})


def make_server_log(server: str, url: str) -> str:
    """Build a single JSONL line matching the servers/events.jsonl format."""
    return json.dumps({"server": server, "url": url}) + "\n"


def make_resolver_with_data(
    agents_json: str | None = None,
    server_logs: dict[str, str] | None = None,
) -> MngrCliBackendResolver:
    """Create a MngrCliBackendResolver pre-populated with test data.

    agents_json is a JSON string matching `mngr list --format json` format, used to populate
    agent IDs and SSH info. server_logs is a mapping of agent ID string to raw
    servers/events.jsonl content, parsed to populate the server URL map for each agent.
    """
    resolver = MngrCliBackendResolver()

    if agents_json is not None:
        parsed = parse_agents_from_json(agents_json)
        # Build DiscoveredAgent objects from the JSON for list_known_mind_ids()
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

    if server_logs:
        for agent_id_str, log_content in server_logs.items():
            records = parse_server_log_records(log_content)
            servers: dict[str, str] = {}
            for record in records:
                servers[str(record.server)] = record.url
            resolver.update_servers(AgentId(agent_id_str), servers)

    return resolver
