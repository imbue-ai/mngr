import pytest

from imbue.mngr_latchkey.core import HIDDEN_BUILTIN_SERVICES
from imbue.mngr_latchkey.services_catalog import ServiceCatalogError
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

# Sync-path resolution (scope -> canonical service name)


def test_all_service_names_includes_known_services() -> None:
    names = ServicesCatalog().all_service_names()
    # A few canonical services that ship in the bundled catalog.
    assert {"slack", "github", "discord"} <= names


def test_services_for_permissions_maps_scopes_to_canonical_names() -> None:
    config = LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},))
    assert ServicesCatalog().services_for_permissions(config) == frozenset({"slack"})


def test_services_for_permissions_collapses_multiple_scopes_of_one_service() -> None:
    # GitHub exposes more than one scope; both must resolve to ``github``.
    config = LatchkeyPermissionsConfig(rules=({"github-rest-api": ["any"]}, {"github-git": ["any"]}))
    assert ServicesCatalog().services_for_permissions(config) == frozenset({"github"})


def test_services_for_permissions_ignores_non_service_scopes() -> None:
    # Imbue Studio's own internal scopes are not in the catalog and contribute nothing.
    config = LatchkeyPermissionsConfig(rules=({"minds-api-proxy-unauthorized": []}, {"slack-api": ["slack-read-all"]}))
    assert ServicesCatalog().services_for_permissions(config) == frozenset({"slack"})


def test_services_for_permissions_empty_for_deny_all() -> None:
    assert ServicesCatalog().services_for_permissions(LatchkeyPermissionsConfig()) == frozenset()


def test_services_for_permissions_wildcard_grants_every_service() -> None:
    config = LatchkeyPermissionsConfig(rules=({"any": ["any"]},))
    assert ServicesCatalog().services_for_permissions(config) == ServicesCatalog().all_service_names()


# Dialog-facing catalog (ServicesCatalog / ServicePermissionInfo)


def _make_catalog(payload: dict[str, object]) -> ServicesCatalog:
    return ServicesCatalog.from_catalog_payload(payload)


def test_catalog_get_returns_entry_for_known_service() -> None:
    catalog = _make_catalog(
        {
            "slack": [
                {
                    "scope": "slack-api",
                    "display_name": "Slack",
                    "permissions": [{"name": "slack-read-all"}, {"name": "slack-write-all"}],
                },
            ],
        },
    )

    infos = catalog.get("slack")

    assert len(infos) == 1
    info = infos[0]
    assert info.name == "slack"
    assert info.scope == "slack-api"
    assert info.display_name == "Slack"
    # ``any`` is always injected at index 0 as an available option; it is
    # not pre-checked by the dialog, but the user can opt into it.
    assert info.permission_schemas[0] == "any"
    assert "slack-read-all" in info.permission_schemas
    assert "slack-write-all" in info.permission_schemas


def test_catalog_exposes_scope_and_permission_descriptions() -> None:
    """Detent's scope and per-permission descriptions are carried onto the dialog-facing record."""
    catalog = _make_catalog(
        {
            "slack": [
                {
                    "scope": "slack-api",
                    "display_name": "Slack",
                    "description": "Any interaction with the Slack API.",
                    "permissions": [
                        {"name": "slack-read-all", "description": "All read operations."},
                        {"name": "slack-write-all"},
                    ],
                },
            ],
        },
    )

    info = catalog.get("slack")[0]

    assert info.description == "Any interaction with the Slack API."
    # Permissions without a description are omitted from the map; the
    # injected ``any`` never has one either.
    assert info.description_by_permission_name == {"slack-read-all": "All read operations."}


def test_catalog_get_returns_all_entries_for_multi_scope_service() -> None:
    """A service that exposes more than one scope yields one entry per scope."""
    catalog = _make_catalog(
        {
            "google": [
                {"scope": "google-gmail-api", "display_name": "Gmail", "permissions": [{"name": "gmail-read"}]},
                {"scope": "google-drive-api", "display_name": "Drive", "permissions": [{"name": "drive-read"}]},
            ],
        },
    )

    infos = catalog.get("google")

    assert tuple(info.scope for info in infos) == ("google-gmail-api", "google-drive-api")
    # Both scopes are independently resolvable by scope lookup.
    assert catalog.get_by_scope("google-gmail-api") is not None
    assert catalog.get_by_scope("google-drive-api") is not None


def test_catalog_get_by_scope_indexes_by_schema_name() -> None:
    """The catalog must support reverse lookup so request events (which carry the scope) can be resolved."""
    catalog = _make_catalog(
        {"slack": [{"scope": "slack-api", "display_name": "Slack", "permissions": []}]},
    )

    info = catalog.get_by_scope("slack-api")

    assert info is not None
    assert info.name == "slack"
    assert info.display_name == "Slack"


def test_catalog_returns_none_for_unknown_keys() -> None:
    catalog = _make_catalog({})

    assert catalog.get("nonexistent") == ()
    assert catalog.get_by_scope("nonexistent-api") is None


def test_catalog_dedups_explicit_any_in_permissions() -> None:
    """A catalog that explicitly lists ``any`` must not produce two ``any`` checkboxes."""
    catalog = _make_catalog(
        {
            "demo": [
                {"scope": "demo-api", "display_name": "Demo", "permissions": [{"name": "any"}, {"name": "demo-read"}]}
            ]
        },
    )

    infos = catalog.get("demo")

    assert len(infos) == 1
    assert infos[0].permission_schemas == ("any", "demo-read")


def test_catalog_handles_empty_permissions_list() -> None:
    """Services with no granular permissions still expose ``any`` as an available option."""
    catalog = _make_catalog(
        {"linear": [{"scope": "linear-api", "display_name": "Linear", "permissions": []}]},
    )

    infos = catalog.get("linear")

    assert len(infos) == 1
    assert infos[0].permission_schemas == ("any",)


def test_catalog_raises_on_malformed_payload() -> None:
    """A structurally invalid catalog is a packaging bug and must surface loudly."""
    with pytest.raises(ServiceCatalogError):
        ServicesCatalog.from_catalog_payload({"broken": [{"display_name": "X", "permissions": []}]})


def test_default_catalog_reads_the_bundled_file() -> None:
    """The zero-arg catalog loads real services from the shipped services.json."""
    catalog = ServicesCatalog()
    slack = catalog.get_by_scope("slack-api")
    assert slack is not None
    assert slack.name == "slack"


def test_default_catalog_omits_hidden_builtin_services() -> None:
    """Drift guard: services hidden from agents must not appear in the generated catalog.

    ``scripts/generate_services_json.py`` skips them, so a regeneration that let
    one back in would offer the user grants for a service whose credentials
    latchkey never injects (and, for ``notion``, resurrect the confusion with
    the separate ``notion-mcp`` integration).
    """
    service_names = ServicesCatalog().all_service_names()
    assert len(HIDDEN_BUILTIN_SERVICES) > 0
    for hidden_service_name in HIDDEN_BUILTIN_SERVICES:
        assert hidden_service_name not in service_names


# Additional (custom) services merged into the bundled catalog


def test_default_catalog_includes_additional_claude_ai_service() -> None:
    """The bundled catalog merges the additional (custom) services alongside builtins."""
    catalog = ServicesCatalog()
    assert "claude-ai" in catalog.all_service_names()
    info = catalog.get_by_scope("claude-ai")
    assert info is not None
    assert info.name == "claude-ai"
    assert info.display_name == "Claude"
    # The named ``everything`` permission is offered (alongside the injected ``any``).
    assert "everything" in info.permission_schemas


def test_additional_service_scope_resolves_to_its_service_name() -> None:
    """A granted additional-service scope maps back to its service name (credential-sync path)."""
    config = LatchkeyPermissionsConfig(rules=({"claude-ai": ["everything"]},))
    assert ServicesCatalog().services_for_permissions(config) == frozenset({"claude-ai"})


# Service-level display names


def test_service_display_name_names_the_service_not_one_of_its_scopes() -> None:
    catalog = ServicesCatalog()
    # ``github`` exposes REST / GraphQL / git, each label disambiguated by a
    # parenthetical; the connection spanning them carries the service's name.
    assert [info.display_name for info in catalog.get("github")][0] == "GitHub (REST API)"
    assert {info.service_display_name for info in catalog.get("github")} == {"GitHub"}
    # A service the catalog gives no name of its own takes its scope's label,
    # parenthetical and all -- there it is part of the name, not a scope marker.
    assert catalog.get("notion-mcp")[0].service_display_name == "Notion (MCP)"
    assert catalog.get("slack")[0].service_display_name == "Slack"


# Implicit permissions (the handshake every grant carries)


_NOTION_MCP_PAYLOAD: dict[str, object] = {
    "notion-mcp": [
        {
            "scope": "notion-mcp-api",
            "display_name": "Notion (MCP)",
            "permissions": [
                {"name": "notion-mcp-session", "description": "Open an MCP session.", "is_implicit": True},
                {"name": "notion-mcp-read-all", "description": "Read everything."},
                {"name": "notion-mcp-search"},
            ],
        },
    ],
}


def test_catalog_sets_implicit_permissions_aside_from_the_grantable_ones() -> None:
    """An implicit permission is never offered as a choice, but its description still travels."""
    info = _make_catalog(_NOTION_MCP_PAYLOAD).get("notion-mcp")[0]

    assert info.permission_schemas == ("any", "notion-mcp-read-all", "notion-mcp-search")
    assert info.implicit_permission_schemas == ("notion-mcp-session",)
    assert info.description_by_permission_name == {
        "notion-mcp-session": "Open an MCP session.",
        "notion-mcp-read-all": "Read everything.",
    }


def test_with_implicit_permissions_follows_a_choice_with_the_handshake_and_leaves_nothing_empty() -> None:
    info = _make_catalog(_NOTION_MCP_PAYLOAD).get("notion-mcp")[0]

    assert info.with_implicit_permissions(("notion-mcp-search",)) == ("notion-mcp-search", "notion-mcp-session")
    assert info.with_implicit_permissions(("any",)) == ("any", "notion-mcp-session")
    # A set that already names the handshake is not given it twice.
    assert info.with_implicit_permissions(("notion-mcp-session", "notion-mcp-search")) == (
        "notion-mcp-session",
        "notion-mcp-search",
    )
    assert info.with_implicit_permissions(()) == ()


def test_with_implicit_permissions_is_the_choice_itself_for_a_scope_without_any() -> None:
    catalog = _make_catalog(
        {"slack": [{"scope": "slack-api", "display_name": "Slack", "permissions": [{"name": "slack-read-all"}]}]},
    )
    info = catalog.get("slack")[0]

    assert info.implicit_permission_schemas == ()
    assert info.with_implicit_permissions(("slack-read-all",)) == ("slack-read-all",)


def test_default_catalog_marks_every_handshake_permission_implicit() -> None:
    """Drift guard: every hosted MCP wrapper's session permission, and Fastmail's two handshakes, are implicit.

    ``scripts/generate_services_json.py`` curates the split; a regeneration that
    lost it would offer a "Session" toggle again and write grants an MCP client
    cannot open a session with.
    """
    catalog = ServicesCatalog()
    mcp_service_names = sorted(name for name in catalog.all_service_names() if name.endswith("-mcp"))
    assert len(mcp_service_names) > 0
    for service_name in mcp_service_names:
        info = catalog.get(service_name)[0]
        assert info.implicit_permission_schemas == (f"{service_name}-session",), service_name
        assert f"{service_name}-session" not in info.permission_schemas, service_name
    jmap = catalog.get_by_scope("fastmail-api")
    dav = catalog.get_by_scope("fastmail-dav-api")
    assert jmap is not None and jmap.implicit_permission_schemas == ("fastmail-session",)
    assert dav is not None and dav.implicit_permission_schemas == ("fastmail-dav-discover",)
    slack = catalog.get_by_scope("slack-api")
    assert slack is not None and slack.implicit_permission_schemas == ()
