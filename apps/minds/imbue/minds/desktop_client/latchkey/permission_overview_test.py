"""Unit tests for the cross-workspace permission overview / revoke helpers."""

import threading
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field
from pydantic import JsonValue

from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import disconnect_account
from imbue.minds.desktop_client.latchkey.permission_overview import probe_service_sign_in_options
from imbue.minds.desktop_client.latchkey.permission_overview import probe_services_info
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_workspace
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.latchkey.testing import leave_permissions_on_this_computer
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
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
    """Static resolver that maps each agent to a specific host and marks them active machines."""

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


# Generous: it only bounds a hang, and a correct implementation trips it at once.
_BARRIER_TIMEOUT_SECONDS: Final[float] = 10.0


def _catalog() -> ServicesCatalog:
    return ServicesCatalog.from_catalog_payload(_CATALOG_PAYLOAD)


def _latchkey(tmp_path: Path) -> Latchkey:
    return Latchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")


def _seed_account_grants(
    latchkey: Latchkey,
    host_id: HostId,
    *grants: tuple[str, str, tuple[str, ...]],
) -> None:
    """Write a host file granting each ``(scope, account, permissions)`` triple.

    Goes through :func:`build_account_grant` so the seeded file has exactly the
    shape production writes -- rules *and* the generated schemas that say which
    account each rule is pinned to (which is what the overview reads).
    """
    rules: list[dict[str, list[str]]] = []
    schemas: dict[str, JsonValue] = {}
    for scope, account, permissions in grants:
        rule_key, granted, grant_schemas = build_account_grant(scope, account, permissions)
        rules.append({rule_key: list(granted)})
        schemas.update(grant_schemas)
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(rules=tuple(rules), schemas=schemas),
    )


def _resolver(agent_host_pairs: dict[str, HostId], names: dict[str, str]) -> _MultiHostResolver:
    return _MultiHostResolver(
        url_by_agent_and_service={},
        host_by_agent={a: str(h) for a, h in agent_host_pairs.items()},
        name_by_agent=names,
        color_by_agent={},
        active_agent_ids=tuple(AgentId(a) for a in agent_host_pairs),
    )


def _accounts_latchkey(tmp_path: Path, accounts_by_service: dict[str, list[str]]) -> FakeAccountsLatchkey:
    """Return a :class:`Latchkey` double reporting the given stored accounts."""
    return FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service=accounts_by_service,
    )


def test_revoke_service_account_leaves_other_accounts_and_services_alone(tmp_path: Path) -> None:
    latchkey = _accounts_latchkey(tmp_path, {"slack": ["alice@x", "bob@x"], "github": [""]})
    gateway = build_fake_gateway_client()
    agent, host = str(AgentId()), HostId()
    _seed_account_grants(
        latchkey,
        host,
        ("slack-api", "alice@x", ("slack-read-all",)),
        ("slack-api", "bob@x", ("slack-read-all",)),
        ("github-rest-api", "", ("github-read-all",)),
    )
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    revoke_service_account_for_workspace(
        resolver,
        gateway,
        _catalog(),
        latchkey,
        agent,
        "slack",
        "alice@x",
        leave_permissions_on_this_computer,
    )

    remaining = gateway.get_permission_rules(permissions_path_for_host(latchkey.plugin_data_dir, host))
    assert account_scope_key("slack-api", "alice@x") not in remaining
    assert remaining.get(account_scope_key("slack-api", "bob@x")) == ("slack-read-all",)
    assert remaining.get(account_scope_key("github-rest-api", "")) == ("github-read-all",)


def test_revoke_unknown_service_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    agent, host = str(AgentId()), HostId()
    resolver = _resolver({agent: host}, {agent: "Alpha"})

    with pytest.raises(PermissionOverviewError, match="Unknown service"):
        revoke_service_account_for_workspace(
            resolver,
            build_fake_gateway_client(),
            _catalog(),
            latchkey,
            agent,
            "nope",
            "a@x",
            leave_permissions_on_this_computer,
        )


def test_revoke_unresolvable_workspace_raises(tmp_path: Path) -> None:
    latchkey = _latchkey(tmp_path)
    resolver = _resolver({}, {})

    with pytest.raises(PermissionOverviewError, match="Could not resolve host"):
        revoke_service_account_for_workspace(
            resolver,
            build_fake_gateway_client(),
            _catalog(),
            latchkey,
            str(AgentId()),
            "slack",
            "a@x",
            leave_permissions_on_this_computer,
        )


# -- Connector accounts (services info --offline) --


def test_disconnect_account_clears_the_named_account(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["a@x", "b@x"]},
    )

    disconnect_account(latchkey, "slack", "a@x")

    assert latchkey.cleared_calls == [("slack", "a@x")]
    assert latchkey.accounts_by_service["slack"] == ["b@x"]


def test_disconnect_account_raises_when_clear_fails(tmp_path: Path) -> None:
    class _FailingClearLatchkey(FakeAccountsLatchkey):
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


def test_browser_sign_in_probes_run_concurrently(tmp_path: Path) -> None:
    """Each probe is its own subprocess, so they must not add up in series.

    The double's ``services_info`` waits on a barrier that only trips once every
    probe has entered it, which no sequential implementation can satisfy.
    """
    service_names = ("slack", "github", "aws")
    barrier = threading.Barrier(len(service_names))

    class _BarrierLatchkey(FakeAccountsLatchkey):
        def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo | None:
            barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
            return super().services_info(service_name, is_offline=is_offline)

    latchkey = _BarrierLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service=dict.fromkeys(service_names, ["a@x"]),
        credential_example_by_service={"aws": None},
    )

    service_info_by_name = probe_services_info(latchkey, service_names)

    assert {name: info.is_browser_auth_supported for name, info in service_info_by_name.items()} == {
        "slack": True,
        "github": True,
        "aws": False,
    }
    assert not barrier.broken, "the probes did not all run at the same time"


class _FlakyProbeLatchkey(FakeAccountsLatchkey):
    """``Latchkey`` double whose first probe of a service fails the way a real one does.

    A failed ``latchkey services info`` does not raise: it returns ``None``.
    Treating that as an answer (and remembering it) would offer a browser flow
    forever to a service that has none.
    """

    failing_service_names: set[str] = Field(default_factory=set)
    probed_service_names: list[str] = Field(default_factory=list)

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo | None:
        self.probed_service_names.append(service_name)
        if service_name in self.failing_service_names:
            self.failing_service_names.discard(service_name)
            return None
        return super().services_info(service_name, is_offline=is_offline)


def test_a_failed_probe_is_left_out_rather_than_reported_as_a_browser_sign_in(tmp_path: Path) -> None:
    latchkey = _FlakyProbeLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"aws": ["a@x"]},
        credential_example_by_service={"aws": None},
        failing_service_names={"aws"},
    )

    assert probe_services_info(latchkey, ("aws",)) == {}


def test_a_failed_probe_is_asked_again_rather_than_remembered(tmp_path: Path) -> None:
    """The memo is for latchkey's answers, not for the fallback that stands in when it has none."""
    latchkey = _FlakyProbeLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"aws": ["a@x"]},
        credential_example_by_service={"aws": None},
        failing_service_names={"aws"},
    )

    assert probe_service_sign_in_options(latchkey, ("aws",)) == {}
    # The second build asks again and gets AWS's real answer: credentials, not a browser.
    assert probe_service_sign_in_options(latchkey, ("aws",))["aws"].is_browser_auth_supported is False
    # ...and that one is remembered, so a third build costs no subprocess.
    assert probe_service_sign_in_options(latchkey, ("aws",))["aws"].is_browser_auth_supported is False
    assert latchkey.probed_service_names == ["aws", "aws"]
