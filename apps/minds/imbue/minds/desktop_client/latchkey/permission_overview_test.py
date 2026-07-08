"""Unit tests for the cross-workspace permission overview / revoke helpers."""

from pathlib import Path

import pytest
from pydantic import Field

from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import build_permission_overview
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_for_workspace
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": [
                {"name": "slack-read-all"},
                {"name": "slack-write-all"},
            ],
        },
    ],
    "github": [
        {
            "scope": "github-rest-api",
            "display_name": "GitHub",
            "permissions": [{"name": "github-read-all"}],
        },
    ],
}


class _MultiHostResolver(StaticBackendResolver):
    """Static resolver that maps each agent to a specific host and marks them active workspaces."""

    host_by_agent: dict[str, str] = Field(default_factory=dict)
    name_by_agent: dict[str, str] = Field(default_factory=dict)
    color_by_agent: dict[str, str] = Field(default_factory=dict)
    active_agent_ids: tuple[AgentId, ...] = Field(default=())

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(a) for a in self.host_by_agent)

    def list_active_workspace_ids(self) -> tuple[AgentId, ...]:
        return self.active_agent_ids

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host = self.host_by_agent.get(str(agent_id))
        if host is None:
            return None
        return AgentDisplayInfo(agent_name=self.name_by_agent.get(str(agent_id), str(agent_id)), host_id=host)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.name_by_agent.get(str(agent_id))

    def get_workspace_color(self, agent_id: AgentId) -> str | None:
        return self.color_by_agent.get(str(agent_id))


def _catalog() -> ServicesCatalog:
    return ServicesCatalog.from_catalog_payload(_CATALOG_PAYLOAD)


def _latchkey(tmp_path: Path) -> Latchkey:
    return Latchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")


def _seed_host(latchkey: Latchkey, host_id: HostId, rules: tuple[dict[str, list[str]], ...]) -> None:
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(rules=rules),
    )


def _resolver(agent_host_pairs: dict[str, HostId], names: dict[str, str]) -> _MultiHostResolver:
    return _MultiHostResolver(
        url_by_agent_and_service={},
        host_by_agent={a: str(h) for a, h in agent_host_pairs.items()},
        name_by_agent=names,
        color_by_agent={},
        active_agent_ids=tuple(AgentId(a) for a in agent_host_pairs),
    )


def test_build_overview_groups_grants_per_service_and_workspace(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    _seed_host(latchkey, host_a, ({"slack-api": ["slack-read-all"]}, {"github-rest-api": ["github-read-all"]}))
    _seed_host(latchkey, host_b, ({"slack-api": ["slack-write-all", "slack-read-all"]},))
    resolver = _resolver({agent_a: host_a, agent_b: host_b}, {agent_a: "Alpha", agent_b: "Beta"})

    overview = build_permission_overview(resolver, build_fake_gateway_client(), _catalog(), latchkey)

    by_service = {o.service_name: o for o in overview}
    assert set(by_service) == {"slack", "github"}
    # Sorted by display name: GitHub before Slack.
    assert [o.display_name for o in overview] == ["GitHub", "Slack"]
    slack_ws = {g.workspace_name: g.permission_labels for g in by_service["slack"].workspace_grants}
    assert slack_ws == {"Alpha": ("slack-read-all",), "Beta": ("slack-read-all", "slack-write-all")}
    github_ws = {g.workspace_name: g.permission_labels for g in by_service["github"].workspace_grants}
    assert github_ws == {"Alpha": ("github-read-all",)}


def test_build_overview_relabels_wildcard_as_all(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, ({"slack-api": ["any"]},))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    overview = build_permission_overview(resolver, build_fake_gateway_client(), _catalog(), latchkey)

    assert len(overview) == 1
    assert overview[0].workspace_grants[0].permission_labels == ("all",)


def test_build_overview_omits_services_with_no_grants(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, ({"slack-api": ["slack-read-all"]},))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    overview = build_permission_overview(resolver, build_fake_gateway_client(), _catalog(), latchkey)

    assert [o.service_name for o in overview] == ["slack"]


def test_build_overview_ignores_non_catalog_scopes(tmp_path: Path) -> None:
    """Internal minds scopes present in a host file must not surface as services."""
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, ({"minds-workspaces": ["minds-workspaces-read"]},))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    overview = build_permission_overview(resolver, build_fake_gateway_client(), _catalog(), latchkey)

    assert overview == ()


def test_revoke_service_for_workspace_removes_all_service_scopes(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, ({"slack-api": ["slack-read-all"]}, {"github-rest-api": ["github-read-all"]}))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    revoke_service_for_workspace(resolver, gateway, _catalog(), latchkey, agent, "slack")

    remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))
    assert "slack-api" not in remaining
    assert remaining.get("github-rest-api") == ("github-read-all",)


def test_revoke_service_for_all_workspaces_removes_across_hosts(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    _seed_host(latchkey, host_a, ({"slack-api": ["slack-read-all"]},))
    _seed_host(latchkey, host_b, ({"slack-api": ["slack-write-all"]},))
    resolver = _resolver({agent_a: host_a, agent_b: host_b}, {agent_a: "Alpha", agent_b: "Beta"})

    processed = revoke_service_for_all_workspaces(resolver, gateway, _catalog(), latchkey, "slack")

    assert processed == 2
    for host in (host_a, host_b):
        remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))
        assert "slack-api" not in remaining


def test_revoke_unknown_service_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    with pytest.raises(PermissionOverviewError, match="Unknown service"):
        revoke_service_for_workspace(resolver, build_fake_gateway_client(), _catalog(), latchkey, agent, "nope")


def test_revoke_unresolvable_workspace_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    resolver = _resolver({}, {})

    with pytest.raises(PermissionOverviewError, match="Could not resolve host"):
        revoke_service_for_workspace(
            resolver, build_fake_gateway_client(), _catalog(), latchkey, str(AgentId()), "slack"
        )
