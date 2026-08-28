import json

import pytest
from pydantic import ValidationError

from imbue.mngr_latchkey.additional_services import _ADDITIONAL_SERVICES_ADAPTER
from imbue.mngr_latchkey.additional_services import additional_service_registration_entries
from imbue.mngr_latchkey.additional_services import additional_service_schemas
from imbue.mngr_latchkey.additional_services import additional_services_catalog_payload
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


def test_every_bundled_registration_is_one_latchkey_can_act_on() -> None:
    """Guard the two latchkey rules that fail *silently* on a malformed registration.

    Registrations are copied into latchkey's config verbatim, so latchkey does
    the validating -- but two of its rules degrade quietly rather than erroring,
    and both would ship as a config that looks fine: a service with no
    ``baseApiUrl`` matches no request, and a ``loginFlow`` with no ``loginUrl``
    is dropped, leaving a service registered but impossible to sign in to.
    """
    for name, registration in additional_service_registration_entries().items():
        assert isinstance(registration, dict), name
        assert registration.get("baseApiUrl"), name
        if "loginFlow" in registration:
            assert registration.get("loginUrl"), name


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
                    "registration": {"baseApiUrl": "https://claude.ai/"},
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


def test_schemas_include_scope_and_permission_schemas() -> None:
    """The merged schemas carry each service's scope schema and permission schema(s)."""
    schemas = additional_service_schemas()
    # The claude-ai scope schema pins the domain; the ``everything`` permission matches all.
    assert schemas["claude-ai"] == {"properties": {"domain": {"const": "claude.ai"}}, "required": ["domain"]}
    assert schemas["everything"] == {}


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
