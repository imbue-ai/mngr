import json
import re
from pathlib import Path

import pytest
from pydantic import JsonValue

from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import SERVICES_CATALOG_FILENAME
from imbue.mngr_latchkey.core import bundled_gateway_extension_content
from imbue.mngr_latchkey.core import custom_service_registration_entries
from imbue.mngr_latchkey.core import merge_minds_latchkey_config
from imbue.mngr_latchkey.core import overlaid_services_catalog_content
from imbue.mngr_latchkey.core import read_registered_services
from imbue.mngr_latchkey.custom_services import CUSTOM_SERVICE_NAME_PREFIX
from imbue.mngr_latchkey.custom_services import CustomServiceError
from imbue.mngr_latchkey.custom_services import DomainWarning
from imbue.mngr_latchkey.custom_services import LoginFlow
from imbue.mngr_latchkey.custom_services import base_api_url_for_domain
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.custom_services import custom_service_catalog_payload
from imbue.mngr_latchkey.custom_services import custom_service_name
from imbue.mngr_latchkey.custom_services import domain_from_base_api_url
from imbue.mngr_latchkey.custom_services import domain_warning
from imbue.mngr_latchkey.custom_services import is_custom_service_name
from imbue.mngr_latchkey.custom_services import scheme_from_base_api_url
from imbue.mngr_latchkey.custom_services import validate_domain
from imbue.mngr_latchkey.custom_services import validate_login_flow
from imbue.mngr_latchkey.custom_services import validate_scheme
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

# Latchkey's own service-name rule (``serviceRegistry.ts``). Every name we
# derive has to satisfy it, or the registration loads and is unreachable.
_LATCHKEY_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


def _write_config(directory: Path, registered_services: dict[str, object]) -> None:
    (directory / "config.json").write_text(
        json.dumps({"settings": {}, "registeredServices": registered_services}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "raw,expected",
    # Lower-casing is the only repair. Single labels, private suffixes and
    # addresses are all hostnames a private network may use, so they pass as
    # they are; a non-ASCII name has to arrive as its punycode.
    [
        ("example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("a-b.com", "a-b.com"),
        ("xn--r8jz45g.com", "xn--r8jz45g.com"),
        ("intranet", "intranet"),
        ("svc.internal", "svc.internal"),
        ("foo.localhost", "foo.localhost"),
        ("10.0.0.5", "10.0.0.5"),
        ("-x.com", "-x.com"),
    ],
)
def test_domain_accepts_and_normalizes(raw: str, expected: str) -> None:
    assert validate_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    # In order: empty, stray whitespace (refused, not trimmed), longer than DNS
    # allows, the shapes a URL carries that a bare hostname must not (scheme, path, port, userinfo, query), then what
    # the derived service name cannot represent (a wildcard, an underscore, an
    # IPv6 literal), a name that is not punycode, empty labels, and the
    # gateway's own address in two spellings.
    [
        "",
        "  api.example.com  ",
        "a" * 254,
        "https://example.com",
        "example.com/v1",
        "example.com:8443",
        "user@example.com",
        "example.com?q=1",
        "*.example.com",
        "a_b.com",
        "[::1]",
        "例え.com",
        "x..com",
        ".example.com",
        "latchkey-self.invalid",
        "LATCHKEY-SELF.INVALID",
    ],
)
def test_domain_rejects(raw: str) -> None:
    with pytest.raises(CustomServiceError):
        validate_domain(raw)


@pytest.mark.parametrize(
    "domain,expected",
    [
        # Ordinary public names, including a private network's own subdomain.
        ("api.example.io", None),
        ("intranet.acme-widgets.com", None),
        ("a.b.c.co.uk", None),
        # Reserved names nothing answers to.
        ("foo.invalid", DomainWarning.UNREACHABLE),
        ("api.test", DomainWarning.UNREACHABLE),
        ("widgets.example", DomainWarning.UNREACHABLE),
        ("example.com", DomainWarning.UNREACHABLE),
        ("www.example.org", DomainWarning.UNREACHABLE),
        ("abcdefghijklmnop.onion", DomainWarning.UNREACHABLE),
        # A reserved name outranks everything else it looks like.
        ("xn--80ak6aa92e.example", DomainWarning.UNREACHABLE),
        # Punycode, whatever suffix it sits on.
        ("xn--80ak6aa92e.com", DomainWarning.LOOKALIKE),
        ("api.xn--r8jz45g.jp", DomainWarning.LOOKALIKE),
        # Addresses, public or not: a numeric last label is never a name.
        ("127.0.0.1", DomainWarning.IP_ADDRESS),
        ("10.0.0.5", DomainWarning.IP_ADDRESS),
        ("203.0.113.9", DomainWarning.IP_ADDRESS),
        # Names the workspace machine's own network resolves.
        ("localhost", DomainWarning.LOCAL_NAME),
        ("app.localhost", DomainWarning.LOCAL_NAME),
        ("gitlab", DomainWarning.LOCAL_NAME),
        ("printer.local", DomainWarning.LOCAL_NAME),
        ("vault.internal", DomainWarning.LOCAL_NAME),
        ("nas.lan", DomainWarning.LOCAL_NAME),
        ("mail.corp", DomainWarning.LOCAL_NAME),
        ("router.home", DomainWarning.LOCAL_NAME),
        ("hub.home.arpa", DomainWarning.LOCAL_NAME),
        # A label that merely *contains* a reserved word is nothing special.
        ("localhost.acme-widgets.com", None),
        ("example.acme-widgets.com", None),
    ],
)
def test_domain_warning(domain: str, expected: DomainWarning | None) -> None:
    assert domain_warning(domain) == expected


def test_domain_rejects_underscore_because_the_naming_rule_depends_on_it() -> None:
    # Not incidental strictness: ``custom_service_name`` substitutes ``_``
    # for ``.``, which is injective only while the input can contain no
    # ``_`` of its own. If this ever starts passing, the naming rule below
    # stops being injective and two domains can collide on one service.
    with pytest.raises(CustomServiceError):
        validate_domain("a_b.com")


def test_name_derives_the_documented_shape() -> None:
    assert custom_service_name("example.com", "https") == "custom_https_example_com"
    assert custom_service_name("api.example.com", "http") == "custom_http_api_example_com"


def test_name_is_injective_over_near_miss_pairs() -> None:
    # ``a-b.com`` and ``a.b.com`` are the pair the obvious ``.`` -> ``-``
    # substitution collides; ``https`` + ``x`` and ``http`` + ``s.x`` are the
    # pair a scheme not followed by its own separator would. Every legal
    # (scheme, domain) pair must land on its own name -- including the two
    # schemes of one domain, which are two services.
    pairs = [
        ("https", "a-b.com"),
        ("https", "a.b.com"),
        ("https", "a-b.c.com"),
        ("https", "a.b-c.com"),
        ("https", "ab.com"),
        ("https", "a.bc.om"),
        ("https", "example.com"),
        ("http", "example.com"),
        ("https", "example.co.m"),
        ("https", "xn--r8jz45g.com"),
        ("https", "x"),
        ("http", "s.x"),
        ("https", "10.0.0.5"),
    ]
    names = [custom_service_name(validate_domain(domain), scheme) for scheme, domain in pairs]
    assert len(set(names)) == len(names), dict(zip(pairs, names, strict=False))


@pytest.mark.parametrize(
    "domain",
    ["example.com", "a-b.com", "0example.com", "xn--r8jz45g.com", "a.b.c.example.com", "intranet", "10.0.0.5"],
)
@pytest.mark.parametrize("scheme", ["https", "http"])
def test_name_name_is_legal_for_latchkey(domain: str, scheme: str) -> None:
    assert re.fullmatch(_LATCHKEY_NAME_PATTERN, custom_service_name(domain, scheme)) is not None


def test_name_recognizes_its_own_names_only() -> None:
    assert is_custom_service_name("custom_example_com")
    assert not is_custom_service_name("slack")
    assert not is_custom_service_name("claude-ai")
    # A self-hosted instance a user registered themselves is not ours.
    assert not is_custom_service_name("my-gitlab")


def test_registration_base_api_url_is_the_bare_origin() -> None:
    # An origin is the only base URL for which latchkey's prefix match and
    # detent's hostname match describe the same set of requests.
    assert base_api_url_for_domain("api.example.com", "https") == "https://api.example.com/"
    assert base_api_url_for_domain("api.example.com", "http") == "http://api.example.com/"


def test_registration_scheme_admits_only_the_two() -> None:
    assert validate_scheme("HTTPS") == "https"
    assert validate_scheme(" http ") == "http"
    with pytest.raises(CustomServiceError):
        validate_scheme("ftp")
    assert build_custom_service_registration("example.com", scheme="http") == {"baseApiUrl": "http://example.com/"}


@pytest.mark.parametrize(
    "base_api_url,expected",
    [("https://example.com/", "https"), ("HTTP://example.com/", "http"), ("ftp://example.com/", None), ("::", None)],
)
def test_registration_scheme_comes_from_the_base_url(base_api_url: str, expected: str | None) -> None:
    assert scheme_from_base_api_url(base_api_url) == expected


def test_registration_without_login_registers_only_the_base_url() -> None:
    assert build_custom_service_registration("example.com", "https") == {"baseApiUrl": "https://example.com/"}


def test_registration_with_login_registers_the_flow_as_given() -> None:
    # The parameters land verbatim: what latchkey reads back is exactly what
    # the agent sent, once validate_login_flow has let it through.
    registration = build_custom_service_registration(
        "example.com",
        "https",
        login_url="https://accounts.example.com/login",
        login_flow=LoginFlow.COOKIE_CAPTURE,
        login_flow_params={"cookieKeys": ["session", "csrf"], "cookieUrl": "https://example.com/"},
    )
    assert registration == {
        "baseApiUrl": "https://example.com/",
        "loginUrl": "https://accounts.example.com/login",
        "loginFlow": {
            "name": "cookie-capture",
            "params": {"cookieKeys": ["session", "csrf"], "cookieUrl": "https://example.com/"},
        },
    }


@pytest.mark.parametrize(
    "login_url,login_flow,params",
    [
        ("https://example.com/login", LoginFlow.COOKIE_CAPTURE, {"cookieKeys": ["session"]}),
        (
            "http://example.com/login",
            LoginFlow.COOKIE_CAPTURE,
            {"cookieKeys": ["a", "b"], "cookieUrl": "https://api.example.com/"},
        ),
        (
            "https://app.example.com/auth/login",
            LoginFlow.TOKEN_CAPTURE,
            {"tokenUrl": "https://app.example.com/api/auth/session", "tokenField": "data.accessToken"},
        ),
        (
            "https://example.com/login",
            LoginFlow.TOKEN_CAPTURE,
            {"tokenUrl": "https://example.com/s", "tokenField": "t", "header": "X-Token: {token}"},
        ),
    ],
)
def test_login_flow_accepts_what_the_cli_accepts(
    login_url: str, login_flow: LoginFlow, params: dict[str, JsonValue]
) -> None:
    validate_login_flow("example.com", login_url, login_flow, params)


def test_login_flow_accepts_no_sign_in_at_all() -> None:
    validate_login_flow("example.com", None, None, None)


@pytest.mark.parametrize(
    "login_url,login_flow,params,fragment",
    [
        # The three fields are one thing: a flow needs a URL and parameters, and vice versa.
        ("https://example.com/login", None, None, "login_flow is required"),
        (None, LoginFlow.COOKIE_CAPTURE, {"cookieKeys": ["s"]}, "login_url is required"),
        ("https://example.com/login", LoginFlow.COOKIE_CAPTURE, None, "login_flow_params is required"),
        (None, None, {"cookieKeys": ["s"]}, "login_flow is required"),
        # Every URL stays on the domain, whichever scheme it uses.
        (
            "https://evil.test/login",
            LoginFlow.COOKIE_CAPTURE,
            {"cookieKeys": ["s"]},
            "login_url must be on example.com",
        ),
        ("ftp://example.com/login", LoginFlow.COOKIE_CAPTURE, {"cookieKeys": ["s"]}, "http or https"),
        (
            "https://example.com/login",
            LoginFlow.COOKIE_CAPTURE,
            {"cookieKeys": ["s"], "cookieUrl": "https://evil.test/"},
            "cookieUrl must be on example.com",
        ),
        (
            "https://example.com/login",
            LoginFlow.TOKEN_CAPTURE,
            {"tokenUrl": "https://evil.test/session", "tokenField": "t"},
            "tokenUrl must be on example.com",
        ),
        # The parameters are latchkey's schema, no more and no less.
        ("https://example.com/login", LoginFlow.COOKIE_CAPTURE, {"cookieKeys": []}, "at least one cookie"),
        ("https://example.com/login", LoginFlow.COOKIE_CAPTURE, {"cookieKeys": ["s", ""]}, "none empty"),
        ("https://example.com/login", LoginFlow.COOKIE_CAPTURE, {"cookieUrl": "https://example.com/"}, "cookieKeys"),
        (
            "https://example.com/login",
            LoginFlow.COOKIE_CAPTURE,
            {"cookieKeys": ["s"], "cookie_keys": ["s"]},
            "cookie_keys",
        ),
        ("https://example.com/login", LoginFlow.COOKIE_CAPTURE, {"cookieKeys": "s"}, "cookieKeys"),
        ("https://example.com/login", LoginFlow.TOKEN_CAPTURE, {"tokenUrl": "https://example.com/s"}, "tokenField"),
        (
            "https://example.com/login",
            LoginFlow.TOKEN_CAPTURE,
            {"tokenUrl": "https://example.com/s", "tokenField": ""},
            "non-empty",
        ),
        (
            "https://example.com/login",
            LoginFlow.TOKEN_CAPTURE,
            {"tokenUrl": "https://example.com/s", "tokenField": "t", "header": "X-Token: nope"},
            "{token}",
        ),
        (
            "https://example.com/login",
            LoginFlow.TOKEN_CAPTURE,
            {"tokenUrl": "https://example.com/s", "tokenField": "t", "header": "Bearer {token}"},
            "header line",
        ),
        # Cookie parameters on the token flow are unknown keys, not a near miss.
        ("https://example.com/login", LoginFlow.TOKEN_CAPTURE, {"cookieKeys": ["s"]}, "cookieKeys"),
    ],
)
def test_login_flow_rejects(
    login_url: str | None, login_flow: LoginFlow | None, params: dict[str, JsonValue] | None, fragment: str
) -> None:
    with pytest.raises(CustomServiceError, match=re.escape(fragment)):
        validate_login_flow("example.com", login_url, login_flow, params)


def test_registration_scope_schema_pins_the_domain_and_the_scheme() -> None:
    # Detent decomposes a request into protocol and domain; a rule on the
    # domain alone would also admit the other scheme, so both are pinned.
    assert build_custom_service_scope_schema("example.com", "http") == {
        "properties": {"domain": {"const": "example.com"}, "protocol": {"const": "http"}},
        "required": ["domain", "protocol"],
    }


@pytest.mark.parametrize(
    "base_api_url,expected",
    [
        ("https://example.com/", "example.com"),
        ("https://EXAMPLE.com/", "example.com"),
        # Hand-edited entries: the hostname is what detent compares, so a
        # port or path must not change which domain we resolve to.
        ("https://example.com:8443/", "example.com"),
        ("https://example.com/v1/api", "example.com"),
        ("not a url", None),
        ("", None),
    ],
)
def test_registration_domain_comes_from_the_base_url_hostname(base_api_url: str, expected: str | None) -> None:
    assert domain_from_base_api_url(base_api_url) == expected


def test_projection_projects_a_custom_service() -> None:
    payload = custom_service_catalog_payload({"custom_api_example_com": {"baseApiUrl": "https://api.example.com/"}})
    assert payload == {
        "custom_api_example_com": [
            {
                "scope": "custom_api_example_com",
                "display_name": "https://api.example.com",
                "description": "Requests to https://api.example.com/.",
                "permissions": [],
                "scope_schema": {
                    "properties": {"domain": {"const": "api.example.com"}, "protocol": {"const": "https"}},
                    "required": ["domain", "protocol"],
                },
            }
        ]
    }


def test_projection_carries_an_http_service_as_http() -> None:
    payload = custom_service_catalog_payload({"custom_intranet_example": {"baseApiUrl": "http://intranet.example/"}})
    entry = payload["custom_intranet_example"][0]
    assert entry["description"] == "Requests to http://intranet.example/."
    assert entry["scope_schema"] == {
        "properties": {"domain": {"const": "intranet.example"}, "protocol": {"const": "http"}},
        "required": ["domain", "protocol"],
    }


def test_projection_skips_a_registration_on_a_scheme_a_custom_service_cannot_use() -> None:
    assert custom_service_catalog_payload({"custom_example_com": {"baseApiUrl": "ftp://example.com/"}}) == {}


def test_projection_the_label_is_the_domain() -> None:
    # The one string that cannot misdescribe what the connection reaches,
    # which is why the agent never gets to supply one.
    payload = custom_service_catalog_payload({"custom_evil_example": {"baseApiUrl": "https://evil.example/"}})
    assert payload["custom_evil_example"][0]["display_name"] == "https://evil.example"


def test_projection_lists_no_permissions_so_the_catalog_offers_only_the_builtin_wildcard() -> None:
    payload = custom_service_catalog_payload({"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    assert payload["custom_example_com"][0]["permissions"] == []


def test_projection_ignores_registrations_that_are_not_ours() -> None:
    payload = custom_service_catalog_payload(
        {
            "claude-ai": {"baseApiUrl": "https://claude.ai/"},
            "my-gitlab": {"baseApiUrl": "https://gitlab.internal/", "serviceFamily": "gitlab"},
            "custom_example_com": {"baseApiUrl": "https://example.com/"},
        }
    )
    assert set(payload) == {"custom_example_com"}


@pytest.mark.parametrize(
    # A registration that is not an object, one with no baseApiUrl at all, one
    # whose baseApiUrl is not a string, and one whose baseApiUrl has no host.
    "bad_entry",
    [
        "not-an-object",
        {},
        {"baseApiUrl": 7},
        {"baseApiUrl": "not a url"},
    ],
)
def test_projection_skips_an_unreadable_entry_without_losing_the_others(bad_entry: JsonValue) -> None:
    # config.json is hand-editable and shared with latchkey itself, so one
    # bad entry must not take down every other service's dialog.
    payload = custom_service_catalog_payload(
        {"custom_broken": bad_entry, "custom_example_com": {"baseApiUrl": "https://example.com/"}}
    )
    assert set(payload) == {"custom_example_com"}


def test_projection_refuses_to_shadow_a_shipped_service() -> None:
    # Latchkey's loader silently skips a registration whose name a builtin
    # holds, so ours would be dead. Drop it here rather than advertise it.
    payload = custom_service_catalog_payload(
        {"custom_example_com": {"baseApiUrl": "https://example.com/"}},
        shipped_service_names=frozenset({"custom_example_com"}),
    )
    assert payload == {}


def test_config_read_reads_the_registered_services_block(tmp_path: Path) -> None:
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    assert set(read_registered_services(tmp_path)) == {"custom_example_com"}


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"registeredServices": []}'])
def test_config_read_degrades_to_empty_rather_than_raising(tmp_path: Path, content: str) -> None:
    # This is read to render the permission dialog and the Connectors page.
    # A service that fails to appear is recoverable (the agent asks again);
    # a permissions page that will not load is not.
    (tmp_path / "config.json").write_text(content, encoding="utf-8")
    assert read_registered_services(tmp_path) == {}


def test_config_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_registered_services(tmp_path) == {}


def test_config_read_registration_entries_carry_only_readable_custom_services(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "claude-ai": {"baseApiUrl": "https://claude.ai/"},
            "custom_broken": {},
            "custom_example_com": {"baseApiUrl": "https://example.com/"},
        },
    )
    entries = custom_service_registration_entries(tmp_path)
    assert entries == {"custom_example_com": {"baseApiUrl": "https://example.com/"}}
    assert all(name.startswith(CUSTOM_SERVICE_NAME_PREFIX) for name in entries)


def test_overlay_catalog_serves_a_custom_service_alongside_the_shipped_ones(tmp_path: Path) -> None:
    _write_config(tmp_path, {"custom_api_example_com": {"baseApiUrl": "https://api.example.com/"}})
    catalog = ServicesCatalog(latchkey_directory=tmp_path)
    info = catalog.get_by_scope("custom_api_example_com")
    assert info is not None
    assert (info.name, info.display_name, info.scope) == (
        "custom_api_example_com",
        "https://api.example.com",
        "custom_api_example_com",
    )
    # The only grantable permission is detent's builtin wildcard, injected
    # by the catalog because the projection lists none of its own.
    assert info.permission_schemas == (WILDCARD_PERMISSION_NAME,)


def test_overlay_overlay_adds_and_never_removes(tmp_path: Path) -> None:
    # The whole migration story for existing installs: nothing a build
    # already served can be displaced by a custom service.
    shipped = ServicesCatalog().all_service_names()
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    overlaid = ServicesCatalog(latchkey_directory=tmp_path).all_service_names()
    assert shipped < overlaid
    assert overlaid - shipped == {"custom_example_com"}


def test_overlay_bundled_claude_ai_is_untouched(tmp_path: Path) -> None:
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    infos = ServicesCatalog(latchkey_directory=tmp_path).get("claude-ai")
    assert infos[0].display_name == "Claude"
    assert infos[0].scope == "claude-ai"
    # Its hand-written permission, which host files in the field name.
    assert "everything" in infos[0].permission_schemas


def test_overlay_no_directory_means_the_shipped_catalog_alone() -> None:
    # What a surface wants when it means "the services this build ships"
    # (the onboarding carousel) rather than "the services this install can reach".
    assert ServicesCatalog().all_service_names() == ServicesCatalog(latchkey_directory=None).all_service_names()


def test_overlay_credential_sync_resolves_a_custom_service_grant(tmp_path: Path) -> None:
    # ``services_for_permissions`` is what decides whose credentials get
    # shipped to a VPS; a custom service has to be resolvable there or its
    # credentials never travel and the remote gateway cannot use it.
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    rule_key, permissions, schemas = build_account_grant(
        "custom_example_com", "", ("any",), build_custom_service_scope_schema("example.com", "https")
    )
    config = LatchkeyPermissionsConfig(rules=({rule_key: list(permissions)},), schemas=schemas)
    catalog = ServicesCatalog(latchkey_directory=tmp_path)
    assert catalog.services_for_permissions(config) == frozenset({"custom_example_com"})


def test_overlay_gateway_catalog_matches_what_the_desktop_serves(tmp_path: Path) -> None:
    # The extensions validate requests against the materialized file while
    # the desktop renders dialogs from ServicesCatalog. They must agree on
    # which services exist, and they do so by sharing one projection.
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    materialized = json.loads(overlaid_services_catalog_content(tmp_path))
    assert set(materialized) == set(ServicesCatalog(latchkey_directory=tmp_path).all_service_names())


def test_overlay_gateway_catalog_without_custom_services_is_the_shipped_file(tmp_path: Path) -> None:
    # The overlay is the only new thing in the system: with nothing to
    # overlay, an install materializes exactly what it ships today.
    _write_config(tmp_path, {})
    assert json.loads(overlaid_services_catalog_content(tmp_path)) == json.loads(
        bundled_gateway_extension_content(SERVICES_CATALOG_FILENAME)
    )


def test_preserve_custom_entries_survive_a_remerge() -> None:
    entry = build_custom_service_registration("example.com", "https")
    first = merge_minds_latchkey_config(None, {"custom_example_com": entry})
    second = merge_minds_latchkey_config(first)
    registered = json.loads(second)["registeredServices"]
    assert registered["custom_example_com"] == entry
    assert "claude-ai" in registered


def test_preserve_a_user_registered_service_survives_too() -> None:
    existing = json.dumps({"registeredServices": {"my-gitlab": {"baseApiUrl": "https://gitlab.internal/"}}})
    registered = json.loads(merge_minds_latchkey_config(existing))["registeredServices"]
    assert registered["my-gitlab"] == {"baseApiUrl": "https://gitlab.internal/"}


def test_preserve_remote_config_receives_the_desktop_custom_services() -> None:
    # A VPS config holds none of them, so they have to be passed in: a
    # gateway with the credentials but not the registration cannot resolve
    # a request to the service at all.
    entry = build_custom_service_registration("example.com", "https")
    remote = merge_minds_latchkey_config('{"settings": {}}', {"custom_example_com": entry})
    assert json.loads(remote)["registeredServices"]["custom_example_com"] == entry


def test_overlay_a_service_registered_after_the_first_read_is_visible(tmp_path: Path) -> None:
    # Minds builds one catalog when it starts and keeps it for the life of the
    # process, so an approval that registers a service mid-session has to show
    # up on the same instance: otherwise the connection exists and works, but
    # every surface that names services keeps claiming it does not.
    _write_config(tmp_path, {})
    catalog = ServicesCatalog(latchkey_directory=tmp_path)
    assert "custom_example_com" not in catalog.all_service_names()

    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})

    assert "custom_example_com" in catalog.all_service_names()
    assert catalog.get_by_scope("custom_example_com") is not None


def test_overlay_the_shipped_half_is_unaffected_by_a_later_read(tmp_path: Path) -> None:
    _write_config(tmp_path, {})
    catalog = ServicesCatalog(latchkey_directory=tmp_path)
    shipped = catalog.all_service_names()
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    assert shipped - catalog.all_service_names() == frozenset()


def test_overlay_carries_the_scope_schema_a_grant_needs(tmp_path: Path) -> None:
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    info = ServicesCatalog(latchkey_directory=tmp_path).get_by_scope("custom_example_com")
    assert info is not None
    assert info.scope_schema == build_custom_service_scope_schema("example.com", "https")


def test_overlay_a_shipped_scope_carries_no_schema_because_detent_ships_one() -> None:
    info = ServicesCatalog().get_by_scope("slack-api")
    assert info is not None
    assert info.scope_schema is None


def test_overlay_a_deregistered_service_stops_being_offered(tmp_path: Path) -> None:
    # The mirror of the "appears without a restart" case: the catalog answers
    # from the file every time, so a service removed from it stops being
    # offered on the same instance, rather than lingering until a restart.
    _write_config(tmp_path, {"custom_example_com": {"baseApiUrl": "https://example.com/"}})
    catalog = ServicesCatalog(latchkey_directory=tmp_path)
    assert "custom_example_com" in catalog.all_service_names()

    _write_config(tmp_path, {})

    assert "custom_example_com" not in catalog.all_service_names()
    assert catalog.get_by_scope("custom_example_com") is None


def test_overlay_the_shipped_half_is_read_once_per_process(tmp_path: Path) -> None:
    # Only the custom half is re-derived per read; the shipped half is package
    # data behind a process-wide cache, so repeated reads keep handing back the
    # very same entries rather than re-validating the file.
    _write_config(tmp_path, {})
    catalog = ServicesCatalog(latchkey_directory=tmp_path)

    assert catalog.as_mapping()["claude-ai"] is catalog.as_mapping()["claude-ai"]
