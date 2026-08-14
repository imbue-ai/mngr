import json

import pytest
from pydantic import ValidationError

from imbue.mngr_latchkey.additional_services import _ADDITIONAL_SERVICES_ADAPTER
from imbue.mngr_latchkey.additional_services import _AdditionalServiceEntry
from imbue.mngr_latchkey.additional_services import _registration_entry
from imbue.mngr_latchkey.additional_services import additional_service_registration_entries
from imbue.mngr_latchkey.additional_services import additional_service_shared_schemas
from imbue.mngr_latchkey.additional_services import additional_services_catalog_payload
from imbue.mngr_latchkey.additional_services import shared_schemas_file_content
from imbue.mngr_latchkey.services_catalog import ServicesCatalog


def test_registration_entries_include_claude_ai() -> None:
    """The bundled file yields claude.ai in latchkey's ``registeredServices`` shape."""
    entries = additional_service_registration_entries()
    assert entries["claude-ai"] == {
        "baseApiUrl": "https://claude.ai/",
        "loginUrl": "https://claude.ai/login",
        "loginFlow": {
            "name": "cookie-capture",
            # claude.ai authenticates with a single session cookie, set on the
            # API's own domain (sign-in may start elsewhere, hence ``cookieUrl``).
            "params": {"cookieKeys": ["sessionKey"], "cookieUrl": "https://claude.ai/"},
        },
    }


def test_registration_entry_omits_the_login_keys_without_a_browser_sign_in() -> None:
    """A service with no browser sign-in gets no ``loginUrl`` / ``loginFlow`` keys at all.

    Latchkey's config schema types both as absent-or-a-value, so a ``null``
    would make the whole entry unreadable to it -- costing the service its
    registration. Every bundled service currently has a sign-in, so the
    projection is exercised here against a service that does not.
    """
    entry = _AdditionalServiceEntry.model_validate(
        {
            "display_name": "Example",
            "base_api_url": "https://example.com/api/",
            "scope": {"name": "example", "schema": {}},
        }
    )
    assert _registration_entry(entry) == {"baseApiUrl": "https://example.com/api/"}


def test_a_service_name_latchkey_would_reject_is_refused_at_load() -> None:
    """A key latchkey cannot canonicalize is caught here, not written into its config.

    Registrations are written straight into latchkey's ``config.json`` instead of
    going through ``latchkey services register``, so the CLI no longer vets the
    name; the bundled file's keys are validated on load instead. The trap is
    keying a service by its dotted domain (``claude.ai``) rather than its
    canonical name (``claude-ai``), which would otherwise land in the config as a
    registration latchkey can never match.
    """
    with pytest.raises(ValidationError):
        _ADDITIONAL_SERVICES_ADAPTER.validate_python(
            {
                "claude.ai": {
                    "display_name": "Claude",
                    "base_api_url": "https://claude.ai/",
                    "scope": {"name": "claude-ai", "schema": {}},
                }
            }
        )


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
