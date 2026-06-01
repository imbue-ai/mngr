"""Unit tests for the lazily-fetched services catalog."""

from imbue.minds.desktop_client.latchkey.gateway_client import AvailableServiceEntry
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.services_catalog import ServicesCatalog
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient


def _make_catalog(payload: dict[str, object]) -> ServicesCatalog:
    client = FakeLatchkeyGatewayClient(available_services_payload=payload)
    return ServicesCatalog(gateway_client=client)


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
        {
            "slack": [
                {
                    "scope": "slack-api",
                    "display_name": "Slack",
                    "permissions": [],
                },
            ],
        },
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
    """A gateway that explicitly lists ``any`` must not produce two ``any`` checkboxes."""
    catalog = _make_catalog(
        {
            "demo": [
                {
                    "scope": "demo-api",
                    "display_name": "Demo",
                    "permissions": [{"name": "any"}, {"name": "demo-read"}],
                },
            ],
        },
    )

    infos = catalog.get("demo")

    assert len(infos) == 1
    assert infos[0].permission_schemas == ("any", "demo-read")


def test_catalog_handles_empty_permissions_list() -> None:
    """Services with no granular permissions still expose ``any`` as an available option."""
    catalog = _make_catalog(
        {
            "linear": [
                {
                    "scope": "linear-api",
                    "display_name": "Linear",
                    "permissions": [],
                },
            ],
        },
    )

    infos = catalog.get("linear")

    assert len(infos) == 1
    assert infos[0].permission_schemas == ("any",)


def test_catalog_is_cached_after_first_fetch() -> None:
    """The catalog issues exactly one HTTP fetch, even when accessed many times."""

    class _CountingFakeClient(FakeLatchkeyGatewayClient):
        fetch_count: int = 0

        def get_available_services(self) -> dict[str, tuple[AvailableServiceEntry, ...]]:
            # Bump the counter then defer to the base implementation,
            # which validates the configured payload the same way the
            # real client would.
            self.fetch_count += 1
            return super().get_available_services()

    client = _CountingFakeClient(
        available_services_payload={
            "slack": [{"scope": "slack-api", "display_name": "Slack", "permissions": []}],
        },
    )
    catalog = ServicesCatalog(gateway_client=client)

    for _ in range(5):
        catalog.get("slack")
        catalog.get_by_scope("slack-api")

    assert client.fetch_count == 1


def test_catalog_returns_empty_when_gateway_unreachable() -> None:
    """A fetch failure must not crash callers; the catalog reports empty instead.

    The handler treats an empty catalog the same as "scope not in catalog" and
    falls back to the unknown-scope page, which is the right user-facing
    behaviour when the gateway is down.
    """

    class _FailingClient(LatchkeyGatewayClient):
        def get_available_services(self) -> dict[str, tuple[AvailableServiceEntry, ...]]:
            raise LatchkeyGatewayClientError("connection refused")

    client = _FailingClient()
    catalog = ServicesCatalog(gateway_client=client)

    assert catalog.get("slack") == ()
    assert catalog.get_by_scope("slack-api") is None
    assert dict(catalog.as_mapping()) == {}


def test_catalog_returns_empty_when_payload_is_malformed() -> None:
    """A malformed gateway payload must degrade to an empty catalog with a warning, not raise.

    Per-field validation lives in :class:`LatchkeyGatewayClient` (see
    ``gateway_client_test.py`` for the exhaustive shape cases); this
    test only pins that *any* validation error from the client surfaces
    as the unknown-scope fallback rather than crashing the dialog.
    """
    catalog = _make_catalog({"broken": [{"display_name": "X", "permissions": []}]})

    assert catalog.get("broken") == ()
    assert dict(catalog.as_mapping()) == {}
