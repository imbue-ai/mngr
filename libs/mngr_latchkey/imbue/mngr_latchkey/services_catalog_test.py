import pytest

from imbue.mngr_latchkey.services_catalog import ServiceCatalogError
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

# -- Sync-path resolution (scope -> canonical service name) --------------------


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
    # Minds' own internal scopes are not in the catalog and contribute nothing.
    config = LatchkeyPermissionsConfig(rules=({"minds-api-proxy-unauthorized": []}, {"slack-api": ["slack-read-all"]}))
    assert ServicesCatalog().services_for_permissions(config) == frozenset({"slack"})


def test_services_for_permissions_empty_for_deny_all() -> None:
    assert ServicesCatalog().services_for_permissions(LatchkeyPermissionsConfig()) == frozenset()


def test_services_for_permissions_wildcard_grants_every_service() -> None:
    config = LatchkeyPermissionsConfig(rules=({"any": ["any"]},))
    assert ServicesCatalog().services_for_permissions(config) == ServicesCatalog().all_service_names()


# -- Dialog-facing catalog (ServicesCatalog / ServicePermissionInfo) ----------


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
