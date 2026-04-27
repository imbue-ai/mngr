from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.services_catalog import MalformedServicesCatalogError
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import _default_permissions_heuristic
from imbue.minds.desktop_client.latchkey.services_catalog import get_service_info
from imbue.minds.desktop_client.latchkey.services_catalog import load_services_catalog


def test_default_permissions_heuristic_picks_read_all_and_write_all() -> None:
    permissions = (
        "slack-read-all",
        "slack-write-all",
        "slack-chat-read",
        "slack-chat-write",
    )
    assert _default_permissions_heuristic(permissions) == (
        "slack-read-all",
        "slack-write-all",
    )


def test_default_permissions_heuristic_falls_back_to_full_list_when_no_all_suffix() -> None:
    permissions = ("any",)
    assert _default_permissions_heuristic(permissions) == ("any",)


def test_default_permissions_heuristic_with_only_one_all_kind() -> None:
    permissions = ("foo-read-all", "foo-bar")
    assert _default_permissions_heuristic(permissions) == ("foo-read-all",)


def test_load_services_catalog_default_file_loads_all_known_services() -> None:
    catalog = load_services_catalog()

    # Spot-check the services explicitly enumerated in the plan.
    assert "slack" in catalog
    assert "github" in catalog
    assert "google-gmail" in catalog
    assert "telegram" in catalog
    assert "aws" in catalog


def test_load_services_catalog_slack_uses_read_all_write_all_defaults() -> None:
    catalog = load_services_catalog()
    slack = catalog["slack"]

    assert slack.scope_schemas == ("slack-api",)
    assert "slack-read-all" in slack.permission_schemas
    assert slack.default_permissions == ("slack-read-all", "slack-write-all")


def test_load_services_catalog_aws_uses_explicit_override() -> None:
    catalog = load_services_catalog()
    aws = catalog["aws"]

    # AWS schemas don't end in -read-all / -write-all so the heuristic
    # would pick them all; the override pins it to read-only S3.
    assert aws.default_permissions == ("aws-s3-read",)


def test_load_services_catalog_linear_uses_any_as_default() -> None:
    catalog = load_services_catalog()
    linear = catalog["linear"]

    assert linear.permission_schemas == ("any",)
    assert linear.default_permissions == ("any",)


def test_load_services_catalog_telegram_uses_explicit_default() -> None:
    catalog = load_services_catalog()
    telegram = catalog["telegram"]

    # Telegram has no -all suffix; explicit defaults must be respected.
    assert telegram.default_permissions == (
        "telegram-send-messages",
        "telegram-updates",
        "telegram-bot-info",
    )


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


def test_load_services_catalog_rejects_default_outside_permission_schemas(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path,
        """
[services.foo]
display_name = "Foo"
description = "A foo service."
scope_schemas = ["foo-api"]
permission_schemas = ["foo-read-all"]
default_permissions = ["foo-write-all"]
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
description = "A foo service."
scope_schemas = []
permission_schemas = ["foo-read-all"]
""",
    )

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)


def test_load_services_catalog_rejects_empty_permission_schemas(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path,
        """
[services.foo]
display_name = "Foo"
description = "A foo service."
scope_schemas = ["foo-api"]
permission_schemas = []
""",
    )

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)


def test_load_services_catalog_applies_heuristic_when_default_omitted(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path,
        """
[services.foo]
display_name = "Foo"
description = "A foo service."
scope_schemas = ["foo-api"]
permission_schemas = ["foo-read-all", "foo-write-all", "foo-other"]
""",
    )

    catalog = load_services_catalog(path)

    assert catalog["foo"].default_permissions == ("foo-read-all", "foo-write-all")


def test_load_services_catalog_falls_back_to_all_permissions_with_no_all_suffix(tmp_path: Path) -> None:
    path = _write_toml(
        tmp_path,
        """
[services.foo]
display_name = "Foo"
description = "A foo service."
scope_schemas = ["foo-api"]
permission_schemas = ["foo-bar", "foo-baz"]
""",
    )

    catalog = load_services_catalog(path)

    assert catalog["foo"].default_permissions == ("foo-bar", "foo-baz")


def test_load_services_catalog_rejects_invalid_toml(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "not = valid = toml")

    with pytest.raises(MalformedServicesCatalogError):
        load_services_catalog(path)


def test_load_services_catalog_default_for_known_services_is_subset_of_permissions() -> None:
    catalog = load_services_catalog()

    for name, info in catalog.items():
        for default in info.default_permissions:
            assert default in info.permission_schemas, (
                f"Service '{name}' has default '{default}' not in permission_schemas"
            )


def test_service_permission_info_is_frozen() -> None:
    info = ServicePermissionInfo(
        name="foo",
        display_name="Foo",
        description="A foo.",
        scope_schemas=("foo-api",),
        permission_schemas=("foo-read",),
        default_permissions=("foo-read",),
    )

    with pytest.raises(Exception):
        info.name = "bar"  # type: ignore[misc]
