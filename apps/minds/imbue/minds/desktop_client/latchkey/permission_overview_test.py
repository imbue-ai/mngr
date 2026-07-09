"""Unit tests for the cross-workspace permission overview / revoke helpers."""

from pathlib import Path

import pytest
from pydantic import Field

from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import build_file_sharing_overview
from imbue.minds.desktop_client.latchkey.permission_overview import build_permission_overview
from imbue.minds.desktop_client.latchkey.permission_overview import build_workspace_overview
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_file_sharing_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_file_sharing_for_workspace
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_for_workspace
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_workspace_ops_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_workspace_ops_for_workspace
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
    slack_ws = {g.workspace_name: tuple(p.label for p in g.permissions) for g in by_service["slack"].workspace_grants}
    assert slack_ws == {"Alpha": ("slack-read-all",), "Beta": ("slack-read-all", "slack-write-all")}
    github_ws = {
        g.workspace_name: tuple(p.label for p in g.permissions) for g in by_service["github"].workspace_grants
    }
    assert github_ws == {"Alpha": ("github-read-all",)}


def test_build_overview_relabels_wildcard_as_all(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, ({"slack-api": ["any"]},))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    overview = build_permission_overview(resolver, build_fake_gateway_client(), _catalog(), latchkey)

    assert len(overview) == 1
    wildcard = overview[0].workspace_grants[0].permissions
    assert tuple(p.label for p in wildcard) == ("all",)
    assert wildcard[0].description  # the catch-all carries a non-empty tooltip description


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


# -- File sharing --------------------------------------------------------------

# The shared internal scope also carries a baseline permission that must survive
# any file-sharing revocation.
_BASELINE_SELF_PERM = "latchkey-self-create-permission-request"


def _file_sharing_rule(read_paths: tuple[str, ...] = (), write_paths: tuple[str, ...] = ()) -> dict[str, list[str]]:
    perms = [_BASELINE_SELF_PERM]
    perms += [f"minds-file-server-read-{p}" for p in read_paths]
    perms += [f"minds-file-server-write-{p}" for p in write_paths]
    return {"latchkey-self": perms}


def test_build_file_sharing_overview_groups_by_access(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, (_file_sharing_rule(read_paths=("/home/docs", "/tmp/x"), write_paths=("/home/out",)),))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    overview = build_file_sharing_overview(resolver, build_fake_gateway_client(), latchkey)

    assert len(overview) == 1
    # Each path is listed individually with its effective access level, sorted by path.
    access_by_path = {p.path: p.access_label for p in overview[0].paths}
    assert access_by_path == {"/home/docs": "read", "/home/out": "read and write", "/tmp/x": "read"}
    assert [p.path for p in overview[0].paths] == ["/home/docs", "/home/out", "/tmp/x"]


def test_build_file_sharing_overview_omits_workspaces_without_grants(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    # Only a baseline self permission, no file-sharing schemas.
    _seed_host(latchkey, host, ({"latchkey-self": [_BASELINE_SELF_PERM]},))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    assert build_file_sharing_overview(resolver, build_fake_gateway_client(), latchkey) == ()


def test_revoke_file_sharing_for_workspace_keeps_other_permissions(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, (_file_sharing_rule(read_paths=("/home/docs",), write_paths=("/home/out",)),))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    revoke_file_sharing_for_workspace(resolver, gateway, latchkey, agent)

    remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))
    # The baseline self permission survives; every file-sharing schema is gone.
    assert remaining["latchkey-self"] == (_BASELINE_SELF_PERM,)


def test_revoke_file_sharing_for_all_workspaces(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    _seed_host(latchkey, host_a, (_file_sharing_rule(read_paths=("/a",)),))
    _seed_host(latchkey, host_b, (_file_sharing_rule(write_paths=("/b",)),))
    resolver = _resolver({agent_a: host_a, agent_b: host_b}, {agent_a: "A", agent_b: "B"})

    processed = revoke_file_sharing_for_all_workspaces(resolver, gateway, latchkey)

    assert processed == 2
    for host in (host_a, host_b):
        remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))
        assert remaining["latchkey-self"] == (_BASELINE_SELF_PERM,)


def test_revoke_file_sharing_unresolvable_workspace_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    resolver = _resolver({}, {})

    with pytest.raises(PermissionOverviewError, match="Could not resolve host"):
        revoke_file_sharing_for_workspace(resolver, build_fake_gateway_client(), latchkey, str(AgentId()))


# -- Cross-workspace management ------------------------------------------------


def _workspace_rule(names: tuple[str, ...]) -> dict[str, list[str]]:
    return {"latchkey-self": [_BASELINE_SELF_PERM, *names]}


def test_build_workspace_overview_groups_shared_and_per_target(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    _seed_host(
        latchkey,
        host,
        (
            _workspace_rule(
                (
                    "minds-workspaces-read",
                    "minds-workspaces-create",
                    f"minds-workspaces-recover-{target}",
                    f"minds-workspaces-backups-export-{target}",
                )
            ),
        ),
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    overview = build_workspace_overview(resolver, build_fake_gateway_client(), latchkey)

    # Shared group first, then the per-target group.
    assert [g.is_shared for g in overview] == [True, False]
    shared, per_target = overview
    assert shared.target_workspace_id == ""
    assert {p.label for p in shared.cards[0].permissions} == {"read", "create"}
    assert per_target.target_workspace_id == target
    # ``backups-export`` keeps its internal hyphen in the short label.
    assert {p.label for p in per_target.cards[0].permissions} == {"recover", "backups-export"}


def test_build_workspace_overview_ignores_non_workspace_permissions(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    # Only baseline + file-sharing + accounts, no minds-workspaces verbs.
    _seed_host(
        latchkey, host, ({"latchkey-self": [_BASELINE_SELF_PERM, "minds-file-server-read-/x", "minds-accounts-read"]},)
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    assert build_workspace_overview(resolver, build_fake_gateway_client(), latchkey) == ()


def test_revoke_workspace_ops_shared_keeps_per_target_and_baseline(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    _seed_host(
        latchkey,
        host,
        (_workspace_rule(("minds-workspaces-read", f"minds-workspaces-ssh-{target}")),),
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    # Revoke the shared scope only (target_workspace_id=None).
    revoke_workspace_ops_for_workspace(resolver, gateway, latchkey, agent, None)

    remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))[
        "latchkey-self"
    ]
    assert "minds-workspaces-read" not in remaining
    assert f"minds-workspaces-ssh-{target}" in remaining
    assert _BASELINE_SELF_PERM in remaining


def test_revoke_workspace_ops_per_target_keeps_shared(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    _seed_host(
        latchkey,
        host,
        (_workspace_rule(("minds-workspaces-read", f"minds-workspaces-ssh-{target}")),),
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    revoke_workspace_ops_for_workspace(resolver, gateway, latchkey, agent, target)

    remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))[
        "latchkey-self"
    ]
    assert f"minds-workspaces-ssh-{target}" not in remaining
    assert "minds-workspaces-read" in remaining


def test_revoke_workspace_ops_for_all_workspaces(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    _seed_host(latchkey, host_a, (_workspace_rule(("minds-workspaces-create",)),))
    _seed_host(latchkey, host_b, (_workspace_rule(("minds-workspaces-read",)),))
    resolver = _resolver({agent_a: host_a, agent_b: host_b}, {agent_a: "A", agent_b: "B"})

    processed = revoke_workspace_ops_for_all_workspaces(resolver, gateway, latchkey, None)

    assert processed == 2
    for host in (host_a, host_b):
        remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))[
            "latchkey-self"
        ]
        assert remaining == (_BASELINE_SELF_PERM,)
