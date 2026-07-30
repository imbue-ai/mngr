from imbue.minds.desktop_client.onboarding_services import _cleaned_display_name
from imbue.minds.desktop_client.onboarding_services import list_onboarding_services
from imbue.mngr_latchkey.services_catalog import ServicesCatalog


def test_cleaned_display_name_strips_scope_parenthetical() -> None:
    assert _cleaned_display_name("GitHub (REST API)") == "GitHub"
    assert _cleaned_display_name("Slack") == "Slack"
    # Only a trailing parenthetical is stripped, and only with a separating space.
    assert _cleaned_display_name("(weird)") == "(weird)"


def test_list_onboarding_services_dedupes_and_sorts() -> None:
    catalog = ServicesCatalog.from_catalog_payload(
        {
            "notion-mcp": [{"scope": "notion-mcp", "display_name": "Notion (MCP)"}],
            "notion": [{"scope": "notion-api", "display_name": "Notion"}],
            "slack": [{"scope": "slack-api", "display_name": "Slack"}],
            "github": [
                {"scope": "github-rest-api", "display_name": "GitHub (REST API)"},
                {"scope": "github-git", "display_name": "GitHub (git)"},
            ],
        }
    )
    services = list_onboarding_services(catalog)
    # One entry per cleaned display name (notion + notion-mcp collapse to
    # "Notion", github's two scopes to "GitHub"), sorted by name.
    assert [s.display_name for s in services] == ["GitHub", "Notion", "Slack"]
    # The dedupe keeps the first service id in sorted-id order.
    notion = next(s for s in services if s.display_name == "Notion")
    assert notion.service_id == "notion"


def test_list_onboarding_services_bundled_catalog_has_icons() -> None:
    """The real catalog resolves, and the bundled brand icons cover most of it.

    Every icon_url must point at a file that actually ships in
    static/service_icons (list_onboarding_services only emits URLs for
    files it found on disk, so this asserts the mapping stays non-empty
    and well-formed rather than re-checking the filesystem).
    """
    services = list_onboarding_services(ServicesCatalog())
    assert len(services) >= 20
    with_icons = [s for s in services if s.icon_url is not None]
    assert len(with_icons) >= 20
    for service in with_icons:
        assert service.icon_url == f"/_static/service_icons/{service.service_id}.svg"
