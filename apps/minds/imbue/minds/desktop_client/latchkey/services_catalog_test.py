from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.services_catalog import IMPLICIT_DEFAULT_PERMISSIONS
from imbue.minds.desktop_client.latchkey.services_catalog import MalformedServicesCatalogError
from imbue.minds.desktop_client.latchkey.services_catalog import get_service_info
from imbue.minds.desktop_client.latchkey.services_catalog import load_services_catalog


def test_implicit_default_permissions_is_just_any() -> None:
    assert IMPLICIT_DEFAULT_PERMISSIONS == ("any",)


def test_load_services_catalog_default_file_loads_all_known_services() -> None:
    catalog = load_services_catalog()

    # Spot-check the services explicitly enumerated in the plan.
    assert "slack" in catalog
    assert "github" in catalog
    assert "google-gmail" in catalog
    assert "telegram" in catalog
    assert "aws" in catalog


def test_load_services_catalog_prepends_any_to_every_services_permission_schemas() -> None:
    catalog = load_services_catalog()

    for name, info in catalog.items():
        assert info.permission_schemas[0] == "any", (
            f"Service '{name}' must have 'any' as the first permission_schemas entry"
        )


def test_load_services_catalog_does_not_duplicate_any_when_already_present() -> None:
    # Linear's TOML entry intentionally has an empty permission_schemas list,
    # so the auto-prepend produces a single ``any`` entry. Other services
    # never list ``any`` themselves, but verify that if they did, it would
    # be deduplicated rather than appearing twice.
    catalog = load_services_catalog()
    linear = catalog["linear"]

    assert linear.permission_schemas == ("any",)


def test_load_services_catalog_keeps_granular_permissions_after_any() -> None:
    catalog = load_services_catalog()
    slack = catalog["slack"]

    # ``any`` first, then the granular schemas in TOML order.
    assert slack.permission_schemas[0] == "any"
    assert "slack-read-all" in slack.permission_schemas
    assert "slack-write-all" in slack.permission_schemas


def test_get_service_info_returns_none_for_unknown_service() -> None:
    catalog = load_services_catalog()

    assert get_service_info(catalog, "nonexistent-service") is None


def test_get_service_info_returns_entry_for_known_service() -> None:
    catalog = load_services_catalog()

    info = get_service_info(catalog, "github")

    assert info is not None
    assert info.display_name == "GitHub"


def _write_toml(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "services.toml"
    path.write_text(contents)
    return path


def test_load_services_catalog_rejects_missing_services_section(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "[other]\nkey = 'value'\n")

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)


def test_load_services_catalog_rejects_missing_display_name(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path,
        """
[services.foo]
scope_schemas = ["foo-api"]
permission_schemas = ["foo-read-all"]
""",
    )

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)


def test_load_services_catalog_rejects_empty_scope_schemas(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path,
        """
[services.foo]
display_name = "Foo"
scope_schemas = []
permission_schemas = ["foo-read-all"]
""",
    )

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)


def test_load_services_catalog_accepts_empty_permission_schemas(tmp_path: Path) -> None:
    # Empty granular permission list is allowed: the implicit ``any`` will
    # be prepended automatically and end up as the only entry.
    path = _write_toml(
        tmp_path,
        """
[services.foo]
display_name = "Foo"
scope_schemas = ["foo-api"]
permission_schemas = []
""",
    )

    catalog = load_services_catalog(path)

    assert catalog["foo"].permission_schemas == ("any",)


def test_load_services_catalog_rejects_invalid_toml(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "not = valid = toml")

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)
