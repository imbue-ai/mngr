import json

from imbue.changelings.forwarding_server.backend_resolver import MngCliBackendResolver
from imbue.changelings.forwarding_server.backend_resolver import parse_agents_from_json
from imbue.changelings.forwarding_server.backend_resolver import parse_server_log_records
from imbue.changelings.primitives import ServerName
from imbue.mng.primitives import AgentId

DEFAULT_SERVER_NAME: ServerName = ServerName("web")


def make_agents_json(*agent_ids: AgentId) -> str:
    """Build a JSON string matching `mng list --json` output for the given agent IDs."""
    return json.dumps({"agents": [{"id": str(agent_id)} for agent_id in agent_ids]})


def make_server_log(server: str, url: str) -> str:
    """Build a single JSONL line matching the servers/events.jsonl format."""
    return json.dumps({"server": server, "url": url}) + "\n"


def make_resolver_with_data(
    agents_json: str | None = None,
    server_logs: dict[str, str] | None = None,
) -> MngCliBackendResolver:
    """Create a MngCliBackendResolver pre-populated with test data.

    agents_json is a JSON string matching `mng list --json` format, used to populate
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
