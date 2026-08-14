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
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.mngr.primitives import AgentId


@pytest.mark.witnesses(
    "home-page.consent-gate",
    partial="witnesses the server consent signal (needs_error_reporting_consent) and its "
    "one-time persistence that drive the gate; the SPA rendering the 'Help improve Minds' "
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
    partial="witnesses the server signals (discovery incomplete, zero machines, none restorable) "
    "that put the home page in the discovering state; the mandated 'Discovering machines' copy is "
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
    partial="witnesses the server signals (discovery complete, zero machines, none restorable) "
    "that make the home page the new-machine form; rendering the form is a frontend concern",
)
def test_empty_after_discovery_shows_the_create_form(tmp_path: Path) -> None:
    # Given a consented user and an initial discovery that finished without
    # finding any workspace (update_agents called with an empty agent list).
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    assert resolver.has_completed_initial_discovery() is True
    assert resolver.list_active_workspace_ids() == ()
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    payload = json.loads(client.get("/ui/api/create/landing-extras").get_data(as_text=True))
    assert payload["is_discovery_complete"] is True
    assert payload["has_restorable_workspaces"] is False

    status = json.loads(client.get("/ui/api/app-status").get_data(as_text=True))
    assert status["workspace_count"] == 0


@pytest.mark.witnesses(
    "home-page.lists-machines",
    partial="witnesses the locally-discovered case: the derive that feeds the workspaces channel "
    "surfaces every known machine as a row. The synced-from-another-device (remote tile) variant "
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
