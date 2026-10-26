"""Witnessing tests for the home-page ("/") routing behaviors.

The home page is an SPA surface: what an authenticated user sees at "/" is
decided client-side (Shell + LandingPage) from a handful of server signals.
These tests witness those server signals -- the observable contract the SPA
routes off of. Each test's ``partial=`` note names the client-rendering
residue it does not observe.

Behaviors witnessed: ``apps/minds/behaviors/home-page/home-page.feature``.
"""

import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName


@pytest.mark.witnesses(
    "home-page.consent-gate",
    partial="witnesses the server consent signal (needs_error_reporting_consent) and its "
    "one-time persistence that drive the gate; the SPA rendering the 'Help improve Imbue Studio' "
    "screen is a frontend concern outside the Python witnessing surface",
)
def test_consent_gate_is_asked_once_then_never_again(tmp_path: Path) -> None:
    # Given an authenticated user who has never answered the consent question,
    # the server tells the SPA to show the consent screen ahead of home content.
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert minds_config.get_error_reporting_consent_given() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    before = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
    assert before["needs_error_reporting_consent"] is True

    answered = client.post("/ui/api/onboarding/consent")
    assert answered.status_code == 200

    # Then no later visit ever shows the consent screen again: the signal flips
    # off for this session...
    after = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
    assert after["needs_error_reporting_consent"] is False

    # ...and it is persisted per installation, so a fresh app process over the
    # same data dir (a later launch) still sees the question as answered.
    reloaded_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert reloaded_config.get_error_reporting_consent_given() is True
    fresh_client, _fresh_app, _fresh_auth = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=reloaded_config
    )
    relaunch = json.loads(fresh_client.get("/ui/api/app-status").get_data(as_text=True))
    assert relaunch["needs_error_reporting_consent"] is False


@pytest.mark.witnesses(
    "home-page.discovering",
    partial="witnesses the server signals (discovery incomplete, zero workspaces, none restorable) "
    "that put the home page in the discovering state; the mandated 'Discovering workspaces' copy is "
    "rendered by the Mithril LandingPage (frontend/src/views/pages/LandingPage.ts), and the page "
    "refreshes via the /ui/ws channel rather than a timer -- both outside the Python witnessing surface",
)
def test_discovering_state_before_initial_discovery_finishes(tmp_path: Path) -> None:
    # Given a consented user, no workspaces known, and initial discovery still
    # running (update_agents never called -> has_completed_initial_discovery False).
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    minds_config.set_error_reporting_consent_given(True)
    resolver = make_resolver_with_data()
    assert resolver.has_completed_initial_discovery() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver, minds_config=minds_config
    )

    # When they visit "/", the signals the home page routes off of put it in
    # the discovering state: discovery unfinished, no workspace known anywhere.
    payload = json.loads(client.get("/ui/api/create/landing-extras").get_data(as_text=True))
    assert payload["is_discovery_complete"] is False
    assert payload["has_restorable_workspaces"] is False

    status = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
    assert status["workspace_count"] == 0


@pytest.mark.witnesses(
    "home-page.empty-shows-create-form",
    partial="witnesses the server signals (discovery complete, zero workspaces, none restorable, "
    "onboarding complete) that make the home page the new-workspace form; rendering the form is a "
    "frontend concern",
)
def test_empty_after_discovery_shows_the_create_form(tmp_path: Path) -> None:
    # Given a consented user whose installation is past onboarding, and an
    # initial discovery that finished without finding any workspace
    # (update_agents called with an empty agent list).
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    minds_config.set_is_onboarding_complete(True)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    assert resolver.has_completed_initial_discovery() is True
    assert resolver.list_active_workspace_ids() == ()
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver, minds_config=minds_config
    )

    payload = json.loads(client.get("/ui/api/create/landing-extras").get_data(as_text=True))
    assert payload["is_discovery_complete"] is True
    assert payload["has_restorable_workspaces"] is False

    status = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
    assert status["workspace_count"] == 0
    assert status["is_onboarding_complete"] is True


@pytest.mark.witnesses(
    "home-page.empty-shows-create-form",
    partial="witnesses the per-account server signal (an account with no workspace of its own is not "
    "held in the discovering state by another account's restorable workspace); rendering the form is a "
    "frontend concern",
)
def test_another_accounts_restorable_workspace_does_not_hold_a_home_page_in_discovering(tmp_path: Path) -> None:
    # Given Alice's workspace, which an earlier discovery enumerated but this
    # session's (finished) discovery has not re-listed yet: it is known only
    # from the last-good topology and its record's association.
    cli = make_fake_imbue_cloud_cli()
    alice_user_id, bob_user_id = "user-alice", "user-bob"
    cli.add_account(user_id=alice_user_id, email="alice@example.com")
    cli.add_account(user_id=bob_user_id, email="bob@example.com")
    session_store = make_session_store_for_test(tmp_path / "minds", cli=cli)
    host_id, agent_id = HostId.generate(), AgentId.generate()
    session_store.associate_created_workspace(
        user_id=alice_user_id,
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="alices-workspace",
        color=None,
        is_cloud_row=False,
    )
    resolver = MngrCliBackendResolver(last_good_agents_path=tmp_path / "last_good_agent_topology.json")
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=(agent_id,),
            discovered_agents=(
                DiscoveredAgent(
                    host_id=host_id,
                    agent_id=agent_id,
                    agent_name=AgentName("system-services"),
                    provider_name=ProviderInstanceName("docker"),
                    certified_data={"labels": {"workspace": "true", "is_primary": "true"}},
                ),
            ),
        )
    )
    resolver.update_agents(ParsedAgentsResult())
    assert resolver.list_active_workspace_ids() == ()
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver, session_store=session_store
    )

    payload_by_account_id = {
        account_id: json.loads(
            client.get(f"/ui/api/create/landing-extras?account={account_id}").get_data(as_text=True)
        )
        for account_id in (alice_user_id, bob_user_id, LOCAL_PROFILE_USER_ID)
    }

    # Alice's home page keeps discovering until her workspace is re-listed;
    # Bob's and the device's have none of their own and settle.
    assert all(payload["is_discovery_complete"] for payload in payload_by_account_id.values())
    assert payload_by_account_id[alice_user_id]["has_restorable_workspaces"] is True
    assert payload_by_account_id[bob_user_id]["has_restorable_workspaces"] is False
    assert payload_by_account_id[LOCAL_PROFILE_USER_ID]["has_restorable_workspaces"] is False


@pytest.mark.witnesses(
    "home-page.empty-starts-onboarding",
    partial="witnesses the server signals (discovery complete, zero workspaces, none restorable, "
    "onboarding incomplete) that hand the home page over to the start flow, and the sign-in path's "
    "write that ends the hand-over for good; the redirect itself is the Mithril LandingPage "
    "(frontend/src/views/pages/LandingPage.ts), and the create path's write of the same flag is "
    "witnessed in api_v1_test.py",
)
def test_empty_after_discovery_hands_over_to_the_start_flow(tmp_path: Path) -> None:
    # Given an installation that has never been taken past the start flow (the
    # flag was never written) and an initial discovery that finished without
    # finding any workspace.
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    assert resolver.has_completed_initial_discovery() is True
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver, minds_config=minds_config
    )

    # When they visit "/", the signals say: nothing to list, and the install is
    # not past onboarding -- the home page hands over to the start flow.
    payload = json.loads(client.get("/ui/api/create/landing-extras").get_data(as_text=True))
    assert payload["is_discovery_complete"] is True
    status = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
    assert status["workspace_count"] == 0
    assert status["restorable_workspace_ids"] == []
    assert status["is_onboarding_complete"] is False

    # And once they sign in from the start flow's "I have an account (log in)" answer
    # (the SPA posts the completion), no later visit reads as incomplete again.
    assert client.post("/ui/api/onboarding/complete").status_code == 200
    for _ in range(2):
        status = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
        assert status["is_onboarding_complete"] is True


@pytest.mark.witnesses(
    "home-page.lists-workspaces",
    partial="witnesses the locally-discovered case: the derive that feeds the workspaces channel "
    "surfaces every known workspace as a row. The synced-from-another-device (remote tile) variant "
    "and the rendered DOM rows are not exercised here",
)
def test_every_discovered_workspace_is_listed(tmp_path: Path) -> None:
    first = AgentId.generate()
    second = AgentId.generate()
    third = AgentId.generate()
    resolver = make_resolver_with_data(agents_json=make_agents_json(first, second, third))

    rows = _build_workspace_list(resolver)

    listed_ids = {row["id"] for row in rows}
    assert listed_ids == {str(first), str(second), str(third)}
