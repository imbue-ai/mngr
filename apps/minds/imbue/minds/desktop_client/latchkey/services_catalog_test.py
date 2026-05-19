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
            "slack": {
                "scope": "slack-api",
                "display_name": "Slack",
                "permissions": ["slack-read-all", "slack-write-all"],
            },
        },
    )

    info = catalog.get("slack")

    assert info is not None
    assert info.name == "slack"
    assert info.scope == "slack-api"
    assert info.display_name == "Slack"
    # ``any`` is always injected at index 0 as an available option; it is
    # not pre-checked by the dialog, but the user can opt into it.
    assert info.permission_schemas[0] == "any"
    assert "slack-read-all" in info.permission_schemas
    assert "slack-write-all" in info.permission_schemas


def test_catalog_get_by_scope_indexes_by_schema_name() -> None:
    """The catalog must support reverse lookup so request events (which carry the scope) can be resolved."""
    catalog = _make_catalog(
        {
            "slack": {
                "scope": "slack-api",
                "display_name": "Slack",
                "permissions": [],
            },
        },
    )

    info = catalog.get_by_scope("slack-api")

    assert info is not None
    assert info.name == "slack"
    assert info.display_name == "Slack"


def test_catalog_returns_none_for_unknown_keys() -> None:
    catalog = _make_catalog({})

    assert catalog.get("nonexistent") is None
    assert catalog.get_by_scope("nonexistent-api") is None


def test_catalog_dedups_explicit_any_in_permissions() -> None:
    """A gateway that explicitly lists ``any`` must not produce two ``any`` checkboxes."""
    catalog = _make_catalog(
        {
            "demo": {
                "scope": "demo-api",
                "display_name": "Demo",
                "permissions": ["any", "demo-read"],
            },
        },
    )

    info = catalog.get("demo")

    assert info is not None
    assert info.permission_schemas == ("any", "demo-read")


def test_catalog_handles_empty_permissions_list() -> None:
    """Services with no granular permissions still expose ``any`` as an available option."""
    catalog = _make_catalog(
        {
            "linear": {
                "scope": "linear-api",
                "display_name": "Linear",
                "permissions": [],
            },
        },
    )

    info = catalog.get("linear")

    assert info is not None
    assert info.permission_schemas == ("any",)


def test_catalog_is_cached_after_first_fetch() -> None:
    """The catalog issues exactly one HTTP fetch, even when accessed many times."""

    class _CountingFakeClient(FakeLatchkeyGatewayClient):
        fetch_count: int = 0

        def get_available_services(self) -> dict[str, AvailableServiceEntry]:
            # Bump the counter then defer to the base implementation,
            # which validates the configured payload the same way the
            # real client would.
            self.fetch_count += 1
            return super().get_available_services()

    client = _CountingFakeClient(
        available_services_payload={
            "slack": {"scope": "slack-api", "display_name": "Slack", "permissions": []},
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
        def get_available_services(self) -> dict[str, AvailableServiceEntry]:
            raise LatchkeyGatewayClientError("connection refused")

    client = _FailingClient()
    catalog = ServicesCatalog(gateway_client=client)

    assert catalog.get("slack") is None
    assert catalog.get_by_scope("slack-api") is None
    assert dict(catalog.as_mapping()) == {}


def test_catalog_returns_empty_when_payload_is_malformed() -> None:
    """A malformed gateway payload must degrade to an empty catalog with a warning, not raise.

    Per-field validation lives in :class:`LatchkeyGatewayClient` (see
    ``gateway_client_test.py`` for the exhaustive shape cases); this
    test only pins that *any* validation error from the client surfaces
    as the unknown-scope fallback rather than crashing the dialog.
    """
    catalog = _make_catalog({"broken": {"display_name": "X", "permissions": []}})

    assert catalog.get("broken") is None
    assert dict(catalog.as_mapping()) == {}
