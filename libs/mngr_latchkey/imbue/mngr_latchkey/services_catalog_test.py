from imbue.mngr_latchkey.services_catalog import all_service_names
from imbue.mngr_latchkey.services_catalog import services_for_permissions
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig


def test_all_service_names_includes_known_services() -> None:
    names = all_service_names()
    # A few canonical services that ship in the bundled catalog.
    assert {"slack", "github", "discord"} <= names


def test_services_for_permissions_maps_scopes_to_canonical_names() -> None:
    config = LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},))
    assert services_for_permissions(config) == frozenset({"slack"})


def test_services_for_permissions_collapses_multiple_scopes_of_one_service() -> None:
    # GitHub exposes more than one scope; both must resolve to ``github``.
    config = LatchkeyPermissionsConfig(rules=({"github-rest-api": ["any"]}, {"github-git": ["any"]}))
    assert services_for_permissions(config) == frozenset({"github"})


def test_services_for_permissions_ignores_non_service_scopes() -> None:
    # Minds' own internal scopes are not in the catalog and contribute nothing.
    config = LatchkeyPermissionsConfig(rules=({"minds-api-proxy-unauthorized": []}, {"slack-api": ["slack-read-all"]}))
    assert services_for_permissions(config) == frozenset({"slack"})


def test_services_for_permissions_empty_for_deny_all() -> None:
    assert services_for_permissions(LatchkeyPermissionsConfig()) == frozenset()


def test_services_for_permissions_wildcard_grants_every_service() -> None:
    config = LatchkeyPermissionsConfig(rules=({"any": ["any"]},))
    assert services_for_permissions(config) == all_service_names()
