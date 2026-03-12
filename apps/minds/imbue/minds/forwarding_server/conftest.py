import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.minds.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import parse_agents_from_json
from imbue.minds.forwarding_server.backend_resolver import parse_server_log_records
from imbue.minds.primitives import ServerName
from imbue.mng.primitives import AgentId

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


def make_agents_json(*agent_ids: AgentId) -> str:
    """Build a JSON string matching `mng list --format json` output for the given agent IDs."""
    return json.dumps({"agents": [{"id": str(agent_id)} for agent_id in agent_ids]})


def make_server_log(server: str, url: str) -> str:
    """Build a single JSONL line matching the servers/events.jsonl format."""
    return json.dumps({"server": server, "url": url}) + "\n"


def make_resolver_with_data(
    agents_json: str | None = None,
    server_logs: dict[str, str] | None = None,
) -> MngCliBackendResolver:
    """Create a MngCliBackendResolver pre-populated with test data.

    agents_json is a JSON string matching `mng list --format json` format, used to populate
    agent IDs and SSH info. server_logs is a mapping of agent ID string to raw
    servers/events.jsonl content, parsed to populate the server URL map for each agent.
    """
    resolver = MngCliBackendResolver()

    if agents_json is not None:
        parsed = parse_agents_from_json(agents_json)
        resolver.update_agents(parsed)

    if server_logs:
        for agent_id_str, log_content in server_logs.items():
            records = parse_server_log_records(log_content)
            servers: dict[str, str] = {}
            for record in records:
                servers[str(record.server)] = record.url
            resolver.update_servers(AgentId(agent_id_str), servers)

    return resolver
