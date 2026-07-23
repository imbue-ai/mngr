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
from imbue.minds.desktop_client.latchkey.permission_overview import disconnect_account
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_file_sharing_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_file_sharing_for_workspace
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_for_all_workspaces
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_for_workspace
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_workspace_verb_for_workspace
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
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
    # The catch-all carries a non-empty tooltip description.
    assert wildcard[0].description


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


def test_build_workspace_overview_groups_by_granting_workspace(tmp_path: Path) -> None:
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
                    f"minds-workspaces-backups-export-{target}",
                )
            ),
        ),
    )
    # The resolver also knows the target's display name (it is not a granting
    # workspace, so it is not in the active set -- only named for resolution).
    resolver = _resolver({agent: host}, {agent: "Ops Bot", target: "KarelTreti"})

    overview = build_workspace_overview(resolver, build_fake_gateway_client(), latchkey)

    assert len(overview) == 1
    grant = overview[0]
    assert grant.workspace_name == "Ops Bot"
    verbs = {verb.label: verb for verb in grant.verbs}
    assert set(verbs) == {"read", "backups-export"}
    # ``read`` is non-targeted -> all workspaces, no specific targets.
    assert verbs["read"].is_all_workspaces is True
    assert verbs["read"].target_names == ()
    # ``backups-export`` (hyphenated) is pinned to the specific target.
    assert verbs["backups-export"].is_all_workspaces is False
    assert verbs["backups-export"].target_names == ("KarelTreti",)
    # Verbs are in catalog order (read before backups-export).
    assert [verb.label for verb in grant.verbs] == ["read", "backups-export"]


def test_build_workspace_overview_broad_grant_subsumes_specific_targets(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    # ``destroy`` granted both broadly and on a specific target: the broad grant
    # wins the display (all workspaces), no per-target names shown.
    _seed_host(
        latchkey,
        host,
        (_workspace_rule(("minds-workspaces-destroy", f"minds-workspaces-destroy-{target}")),),
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    grant = build_workspace_overview(resolver, build_fake_gateway_client(), latchkey)[0]
    (verb,) = grant.verbs
    assert verb.label == "destroy"
    assert verb.is_all_workspaces is True
    assert verb.target_names == ()


def test_build_workspace_overview_ignores_non_workspace_permissions(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    # Only baseline + file-sharing + accounts, no minds-workspaces verbs.
    _seed_host(
        latchkey, host, ({"latchkey-self": [_BASELINE_SELF_PERM, "minds-file-server-read-/x", "minds-accounts-read"]},)
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    assert build_workspace_overview(resolver, build_fake_gateway_client(), latchkey) == ()


def test_revoke_workspace_verb_removes_verb_across_targets_keeps_others(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    gateway = build_fake_gateway_client()
    agent, host = str(AgentId()), HostId()
    target_a, target_b = str(AgentId()), str(AgentId())
    _seed_host(
        latchkey,
        host,
        (
            _workspace_rule(
                (
                    "minds-workspaces-read",
                    f"minds-workspaces-ssh-{target_a}",
                    f"minds-workspaces-ssh-{target_b}",
                )
            ),
        ),
    )
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    # Revoking ``ssh`` removes it for every target, but keeps ``read`` + baseline.
    revoke_workspace_verb_for_workspace(resolver, gateway, latchkey, agent, "minds-workspaces-ssh")

    remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))[
        "latchkey-self"
    ]
    assert f"minds-workspaces-ssh-{target_a}" not in remaining
    assert f"minds-workspaces-ssh-{target_b}" not in remaining
    assert "minds-workspaces-read" in remaining
    assert _BASELINE_SELF_PERM in remaining


def test_revoke_workspace_verb_unknown_verb_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    resolver = _resolver({agent: host}, {agent: "Ops Bot"})

    with pytest.raises(PermissionOverviewError, match="Unknown workspace verb"):
        revoke_workspace_verb_for_workspace(
            resolver, build_fake_gateway_client(), latchkey, agent, "minds-workspaces-nope"
        )


def test_revoke_workspace_verb_unresolvable_workspace_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    resolver = _resolver({}, {})

    with pytest.raises(PermissionOverviewError, match="Could not resolve host"):
        revoke_workspace_verb_for_workspace(
            resolver, build_fake_gateway_client(), latchkey, str(AgentId()), "minds-workspaces-read"
        )


# -- Round-trip guardrails -----------------------------------------------------
#
# These lock the parsers to the exact permission-name format the gateway emits
# (``minds-file-server-<access>-<path>`` and ``minds-workspaces-<verb>`` /
# ``minds-workspaces-<verb>-<target>``). The *gateway* side of this contract --
# that a real grant produces exactly these names -- is verified against a live
# Node gateway in ``mngr_latchkey``'s ``permission_requests_test.py``; these
# tests are the other half: given those canonical names, the parser must recover
# the original fields. The names below are written as literals on purpose (not
# via the parser's own constants), so a rename of the parser's convention breaks
# these instead of silently mis-parsing. They deliberately include the awkward
# cases: a hyphenated verb (``backups-export``), a verb whose name is a prefix
# of another (``read`` vs ``recover``), paths containing hyphens, and a path
# that itself starts with an access-like token.


def test_file_sharing_parser_round_trips_canonical_gateway_names(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    # Each case is (path, access-token-in-name, expected-access-label). The
    # awkward ones: a hyphen inside the path, a path that begins with an
    # access-like token, and a path with a space (kept verbatim in the name).
    cases = (
        ("/home/user/docs", "read", "read"),
        ("/home/user/my-notes.txt", "write", "read and write"),
        ("/read-only/data", "read", "read"),
        ("/home/with space/file.txt", "read", "read"),
    )
    names = [f"minds-file-server-{access}-{path}" for path, access, _ in cases]
    _seed_host(latchkey, host, ({"latchkey-self": [_BASELINE_SELF_PERM, *names]},))
    resolver = _resolver({agent: host}, {agent: "WS"})

    overview = build_file_sharing_overview(resolver, build_fake_gateway_client(), latchkey)

    assert len(overview) == 1
    recovered = {shared.path: shared.access_label for shared in overview[0].paths}
    assert recovered == {path: label for path, _, label in cases}


def test_workspace_parser_round_trips_canonical_gateway_names(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    # Shared: a non-targeted verb (read) and a targeted verb granted for all
    # workspaces (destroy). Per-target: recover (whose name must not be read as
    # ``read``) and backups-export (a hyphenated verb name).
    names = (
        "minds-workspaces-read",
        "minds-workspaces-destroy",
        f"minds-workspaces-recover-{target}",
        f"minds-workspaces-backups-export-{target}",
    )
    _seed_host(latchkey, host, ({"latchkey-self": [_BASELINE_SELF_PERM, *names]},))
    resolver = _resolver({agent: host}, {agent: "Ops"})

    overview = build_workspace_overview(resolver, build_fake_gateway_client(), latchkey)

    assert len(overview) == 1
    verbs = {verb.label: verb for verb in overview[0].verbs}
    # Non-targeted / broadly-granted verbs read as all-workspaces; the per-target
    # ones (including the hyphenated ``backups-export``) carry the target name.
    assert verbs["read"].is_all_workspaces and verbs["destroy"].is_all_workspaces
    assert verbs["recover"].target_names == (target,)
    assert verbs["backups-export"].target_names == (target,)


# -- Connector accounts (services info --offline) --


class _AccountsLatchkey(Latchkey):
    """``Latchkey`` double whose ``services_info`` / ``auth_clear`` operate on an in-memory account map.

    ``services_info(--offline)`` reports the configured accounts for a service;
    ``auth_clear`` records each call and removes the named account so
    :func:`disconnect_account` sees the updated state on its follow-up read.
    """

    accounts_by_service: dict[str, list[str]] = Field(default_factory=dict)
    cleared_calls: list[tuple[str, str | None]] = Field(default_factory=list)

    def _accounts_for(self, service_name: str) -> tuple[ServiceAccountCredential, ...]:
        return tuple(
            ServiceAccountCredential(account=account, credential_status=CredentialStatus.VALID)
            for account in self.accounts_by_service.get(service_name, [])
        )

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo:
        del is_offline
        accounts = self._accounts_for(service_name)
        return LatchkeyServiceInfo(
            credential_status=CredentialStatus.VALID if accounts else CredentialStatus.MISSING,
            accounts=accounts,
            auth_options=frozenset({"browser", "set"}),
            set_credentials_example=None,
        )

    def auth_list(self, *, is_offline: bool = False) -> dict[str, tuple[ServiceAccountCredential, ...]]:
        del is_offline
        return {service: self._accounts_for(service) for service in self.accounts_by_service}

    def auth_clear(
        self,
        service_name: str,
        *,
        account: str | None = None,
        is_all: bool = False,
    ) -> tuple[bool, str]:
        del is_all
        self.cleared_calls.append((service_name, account))
        if account is not None and service_name in self.accounts_by_service:
            self.accounts_by_service[service_name] = [
                stored for stored in self.accounts_by_service[service_name] if stored != account
            ]
        return (True, "")


def test_build_overview_lists_service_accounts(tmp_path: Path) -> None:
    latchkey = _AccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["hynek@imbue-ai", ""]},
    )
    agent, host = str(AgentId()), HostId()
    _seed_host(latchkey, host, ({"slack-api": ["slack-read-all"]},))
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    overview = build_permission_overview(resolver, build_fake_gateway_client(), _catalog(), latchkey)

    slack = {o.service_name: o for o in overview}["slack"]
    # Named account first, the unnamed default account last and relabelled.
    assert [(a.account, a.label) for a in slack.accounts] == [
        ("hynek@imbue-ai", "hynek@imbue-ai"),
        ("", "Default account"),
    ]


def test_disconnect_account_reports_not_last_when_accounts_remain(tmp_path: Path) -> None:
    latchkey = _AccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["a@x", "b@x"]},
    )

    is_last = disconnect_account(latchkey, "slack", "a@x")

    assert is_last is False
    assert latchkey.cleared_calls == [("slack", "a@x")]


def test_disconnect_account_reports_last_when_none_remain(tmp_path: Path) -> None:
    latchkey = _AccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["only@x"]},
    )

    is_last = disconnect_account(latchkey, "slack", "only@x")

    assert is_last is True
    assert latchkey.cleared_calls == [("slack", "only@x")]


def test_disconnect_account_raises_when_clear_fails(tmp_path: Path) -> None:
    class _FailingClearLatchkey(_AccountsLatchkey):
        def auth_clear(
            self,
            service_name: str,
            *,
            account: str | None = None,
            is_all: bool = False,
        ) -> tuple[bool, str]:
            del service_name, account, is_all
            return (False, "keychain locked")

    latchkey = _FailingClearLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["a@x"]},
    )

    with pytest.raises(PermissionOverviewError, match="keychain locked"):
        disconnect_account(latchkey, "slack", "a@x")
