import json

import pytest

from imbue.changelings.forwarding_server.backend_resolver import MngCliInterface
from imbue.changelings.primitives import ServerName
from imbue.mng.primitives import AgentId

DEFAULT_SERVER_NAME: ServerName = ServerName("web")


class FakeMngCli(MngCliInterface):
    """Fake mng CLI for testing that returns canned responses."""

    server_logs: dict[str, str]
    agents_json: str | None

    def read_agent_log(self, agent_id: AgentId, log_file: str) -> str | None:
        return self.server_logs.get(str(agent_id))

    def list_agents_json(self) -> str | None:
        return self.agents_json


def make_agents_json(*agent_ids: AgentId) -> str:
    """Build a JSON string matching `mng list --json` output for the given agent IDs."""
    return json.dumps({"agents": [{"id": str(aid)} for aid in agent_ids]})


def make_server_log(server: str, url: str) -> str:
    """Build a single JSONL line matching the servers.jsonl format."""
    return json.dumps({"server": server, "url": url}) + "\n"


@pytest.fixture()
def fake_mng_cli() -> FakeMngCli:
    """Create an empty FakeMngCli instance."""
    return FakeMngCli(server_logs={}, agents_json=None)
