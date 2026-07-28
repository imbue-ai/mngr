import json

from imbue.mngr_latchkey.additional_services import additional_service_shared_schemas
from imbue.mngr_latchkey.additional_services import additional_services_catalog_payload
from imbue.mngr_latchkey.additional_services import load_additional_service_registrations
from imbue.mngr_latchkey.additional_services import shared_schemas_file_content
from imbue.mngr_latchkey.services_catalog import ServicesCatalog


def test_load_additional_service_registrations_includes_claude_ai() -> None:
    """The bundled file yields claude.ai with the base API URL used for registration."""
    registration_by_name = {
        registration.name: registration for registration in load_additional_service_registrations()
    }
    assert "claude-ai" in registration_by_name
    assert registration_by_name["claude-ai"].base_api_url == "https://claude.ai/"


def test_catalog_payload_projects_claude_ai_into_services_json_shape() -> None:
    """The catalog projection matches the ``services.json`` scope-entry shape."""
    payload = additional_services_catalog_payload()
    assert "claude-ai" in payload
    entries = payload["claude-ai"]
    # An additional service exposes exactly one scope.
    assert len(entries) == 1
    entry = entries[0]
    assert entry["scope"] == "claude-ai"
    assert entry["display_name"] == "Claude"
    # The single ``everything`` permission is projected through into the entry.
    assert "everything" in json.dumps(entry["permissions"])


def test_shared_schemas_include_scope_and_permission_schemas() -> None:
    """The merged shared schemas carry each service's scope schema and permission schema(s)."""
    schemas = additional_service_shared_schemas()
    # The claude-ai scope schema pins the domain; the ``everything`` permission matches all.
    assert schemas["claude-ai"] == {"properties": {"domain": {"const": "claude.ai"}}, "required": ["domain"]}
    assert schemas["everything"] == {}


def test_shared_schemas_file_content_is_a_schemas_only_detent_config() -> None:
    """The serialized shared file is a detent config with only a ``schemas`` block (no rules)."""
    parsed = json.loads(shared_schemas_file_content())
    assert set(parsed.keys()) == {"schemas"}
    assert "claude-ai" in parsed["schemas"]
    assert "everything" in parsed["schemas"]


def test_every_additional_service_is_folded_into_the_bundled_services_json() -> None:
    """Drift guard: the generated catalog must carry every additional service.

    ``scripts/generate_services_json.py`` folds these in so the readers only ever
    see ``services.json``. A regeneration that skipped the fold would silently
    drop them from the dialog and from request validation, so pin it here.
    """
    catalog = ServicesCatalog()
    for name, entries in additional_services_catalog_payload().items():
        infos = catalog.get(name)
        assert infos, f"{name} is missing from the bundled services.json"
        assert [info.scope for info in infos] == [entry["scope"] for entry in entries]
