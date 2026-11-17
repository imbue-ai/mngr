from imbue.minds.desktop_client.conftest import GOOD_APP_ICON
from imbue.minds.desktop_client.conftest import make_apps_seeded_resolver
from imbue.minds.desktop_client.ui_models import UiWorkspaceApp
from imbue.minds.desktop_client.wallpaper_cache import content_version
from imbue.minds.desktop_client.workspace_apps import accepted_workspace_app_icon
from imbue.minds.desktop_client.workspace_apps import build_workspace_apps
from imbue.minds.desktop_client.workspace_apps import icon_version_of
from imbue.minds.desktop_client.workspace_apps import resolve_workspace_apps
from imbue.mngr.primitives import AgentId

_AGENT_ID = AgentId("agent-" + "b" * 32)


def test_icon_version_is_a_digest_of_the_accepted_markup_and_empty_for_unusable_markup() -> None:
    assert icon_version_of(GOOD_APP_ICON) == content_version(GOOD_APP_ICON.encode("utf-8"))
    assert icon_version_of(" " + GOOD_APP_ICON + " ") == icon_version_of(GOOD_APP_ICON)
    assert icon_version_of('<svg onload="x()"></svg>') == ""
    assert icon_version_of("") == ""


def test_build_workspace_apps_falls_back_to_the_service_name_and_an_empty_label() -> None:
    apps = build_workspace_apps(("alpha", "beta"), {"alpha": "Alpha"}, {"alpha": "alpha-r4nd"}, {})

    assert apps == (
        UiWorkspaceApp(name="alpha", display_name="Alpha", label="alpha-r4nd", icon_version=""),
        UiWorkspaceApp(name="beta", display_name="beta", label="", icon_version=""),
    )


def test_resolve_workspace_apps_lists_app_kind_services_only_with_their_versions() -> None:
    apps = resolve_workspace_apps(make_apps_seeded_resolver(str(_AGENT_ID)), _AGENT_ID)

    assert [(app.name, app.display_name, app.label) for app in apps] == [
        ("reading-list", "Reading List", "reading-list-k3j9x2ab"),
        ("zenbox", "zenbox", ""),
    ]
    assert apps[0].icon_version == icon_version_of(GOOD_APP_ICON)
    # zenbox's markup fails the gate, so its tile is a monogram.
    assert apps[1].icon_version == ""


def test_accepted_workspace_app_icon_serves_only_an_apps_acceptable_markup() -> None:
    resolver = make_apps_seeded_resolver(str(_AGENT_ID))

    assert accepted_workspace_app_icon(resolver, _AGENT_ID, "reading-list") == GOOD_APP_ICON
    assert accepted_workspace_app_icon(resolver, _AGENT_ID, "zenbox") == ""
    # A system app is no app of the home page's, and an unknown name has nothing.
    assert accepted_workspace_app_icon(resolver, _AGENT_ID, "terminal") == ""
    assert accepted_workspace_app_icon(resolver, _AGENT_ID, "nope") == ""
