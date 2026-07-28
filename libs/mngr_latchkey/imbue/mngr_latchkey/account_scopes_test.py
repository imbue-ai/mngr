"""Unit tests for generating per-account grants and reading them back structurally.

The ``permission_requests`` gateway extension carries a JavaScript copy of the
two generating helpers (it computes a pending request's effect in-process, where
no Python is available). The tests at the bottom of this module execute that copy
with Node and compare it against this package's definitions, so the two cannot
silently drift.
"""

import json
import shutil
import subprocess
from itertools import product
from pathlib import Path
from typing import Final

import pytest
from pydantic import JsonValue

from imbue.mngr_latchkey.account_scopes import ACCOUNT_SCOPE_SEPARATOR
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.account_scopes import build_account_scope_schema
from imbue.mngr_latchkey.account_scopes import list_account_grants
from imbue.mngr_latchkey.account_scopes import resolve_account_scope
from imbue.mngr_latchkey.account_scopes import resolved_schema_names
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

_NODE_BINARY: Final[str | None] = shutil.which("node")
_EXTENSIONS_DIR: Final[Path] = Path(__file__).resolve().parent / "extensions"


def test_account_scope_key_embeds_both_parts() -> None:
    assert account_scope_key("slack-api", "hynek@imbue-ai") == "slack-api:hynek@imbue-ai"
    # The unnamed default account (the empty string) yields a trailing separator.
    assert account_scope_key("slack-api", "") == "slack-api:"


def test_account_scope_key_is_the_identity_for_every_shipped_scope() -> None:
    """The escaping must not change how a real permissions file reads.

    Every scope name in the bundled catalog is escape-free, so keys look exactly
    like ``slack-api:hynek@imbue-ai``.
    """
    scopes = [info.scope for infos in ServicesCatalog().as_mapping().values() for info in infos]
    assert scopes
    assert [account_scope_key(scope, "a@b") for scope in scopes] == [f"{scope}:a@b" for scope in scopes]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Without escaping the scope half, each of these pairs would collide on
        # a single key -- and the gateway merges rules *by key*, so one grant
        # would overwrite (or widen) the other.
        (("slack-api:", "a@b"), ("slack-api", ":a@b")),
        (("slack-api", ""), ("slack-api:", "")),
        # A scope that literally contains an escape sequence must not collide
        # with one that contains the character it escapes.
        (("odd%3Ascope", "a@b"), ("odd:scope", "a@b")),
        (("odd%25scope", "a@b"), ("odd%scope", "a@b")),
    ],
)
def test_account_scope_key_never_collides(first: tuple[str, str], second: tuple[str, str]) -> None:
    assert account_scope_key(*first) != account_scope_key(*second)


def test_account_scope_key_is_injective_over_adversarial_names() -> None:
    """Brute-force the property the escaping exists for.

    The alphabet is the separator, the escape character, and the characters its
    escape sequences are made of, so pairs like ``(\":\", \"\")`` /
    ``(\"%3A\", \"\")`` -- which a naive or wrongly-ordered escape collapses
    onto one key -- are actually constructed.
    """
    alphabet = (":", "%", "2", "3", "5", "A", "a")
    names = ["".join(word) for length in range(4) for word in product(alphabet, repeat=length)]
    accounts = ("", "a", ":", "%", "%3A", "a:b")
    keys_by_pair = {(scope, account): account_scope_key(scope, account) for scope in names for account in accounts}
    assert len(set(keys_by_pair.values())) == len(keys_by_pair)


def test_generated_schema_pins_the_account_and_composes_the_base_scope() -> None:
    schema = build_account_scope_schema("slack-api", "hynek@imbue-ai")
    assert schema == {
        "allOf": [
            {"$ref": "#/$defs/slack-api"},
            {
                "properties": {
                    "customMetadata": {
                        "type": "object",
                        "properties": {"account": {"const": "hynek@imbue-ai"}},
                        "required": ["account"],
                    },
                },
                "required": ["customMetadata"],
            },
        ],
    }


def test_build_account_grant_returns_the_key_permissions_and_backing_schema() -> None:
    rule_key, permissions, schemas = build_account_grant("slack-api", "a@b", ("slack-read-all",))

    assert rule_key == account_scope_key("slack-api", "a@b")
    assert permissions == ("slack-read-all",)
    # The schema for the rule key travels with the grant: the gateway extension
    # authors nothing of its own.
    assert schemas == {rule_key: build_account_scope_schema("slack-api", "a@b")}


# -- Reading grants back (structurally, never by parsing the key) ---------------


@pytest.mark.parametrize(
    ("scope", "account"),
    [
        ("slack-api", "hynek@imbue-ai"),
        # The unnamed default account.
        ("slack-api", ""),
        # A scope and an account that both contain the naming convention's
        # separator: splitting the key would be ambiguous, the structure is not.
        ("odd:scope", "we:ird@example.com"),
    ],
)
def test_resolve_account_scope_round_trips_the_generated_schema(scope: str, account: str) -> None:
    assert resolve_account_scope(build_account_scope_schema(scope, account)) == (scope, account)


@pytest.mark.parametrize(
    "schema",
    [
        # A plain scope schema (the gateway-self scope).
        {"properties": {"domain": {"const": "latchkey-self.invalid"}}, "required": ["domain"]},
        # A composition with no account gate.
        {"allOf": [{"$ref": "#/$defs/slack-api"}]},
        # An account gate with no base scope reference.
        {"properties": {"customMetadata": {"properties": {"account": {"const": "a@b"}}}}},
        # Two base scopes: not a shape we generate, and guessing could report a
        # grant as narrower than it is.
        {
            "allOf": [
                {"$ref": "#/$defs/slack-api"},
                {"$ref": "#/$defs/github-rest-api"},
                {"properties": {"customMetadata": {"properties": {"account": {"const": "a@b"}}}}},
            ]
        },
        # Not an object at all.
        "nonsense",
        # A rule key with no schema of its own (e.g. a built-in scope name).
        None,
    ],
)
def test_resolve_account_scope_returns_none_for_other_shapes(schema: JsonValue) -> None:
    assert resolve_account_scope(schema) is None


def test_list_account_grants_reports_only_generated_per_account_rules() -> None:
    slack_key, slack_permissions, slack_schemas = build_account_grant("slack-api", "a@b", ("slack-read-all",))
    github_key, github_permissions, github_schemas = build_account_grant("github-rest-api", "", ("github-read-all",))
    config = LatchkeyPermissionsConfig(
        rules=(
            {"latchkey-self": ["latchkey-self-read-self-permissions"]},
            {slack_key: list(slack_permissions)},
            # A legacy account-agnostic service rule: no generated schema, so it
            # is not a per-account grant.
            {"linear-api": ["any"]},
            {github_key: list(github_permissions)},
        ),
        schemas={
            "latchkey-self": {"properties": {"domain": {"const": "latchkey-self.invalid"}}},
            **slack_schemas,
            **github_schemas,
        },
    )

    grants = list_account_grants(config)

    assert [(grant.scope, grant.account, grant.permissions) for grant in grants] == [
        ("slack-api", "a@b", ("slack-read-all",)),
        ("github-rest-api", "", ("github-read-all",)),
    ]
    assert [grant.rule_key for grant in grants] == [slack_key, github_key]


def test_resolved_schema_names_follows_the_reference_closure() -> None:
    rule_key, _permissions, schemas = build_account_grant("slack-api", "a@b", ("slack-read-all",))

    names = resolved_schema_names(rule_key, schemas)

    # The rule key itself plus the built-in scope its schema composes.
    assert names == frozenset({rule_key, "slack-api"})


def test_resolved_schema_names_of_an_undefined_key_is_just_the_key() -> None:
    # A rule keyed by a built-in schema name defines nothing in the file.
    assert resolved_schema_names("slack-api", {}) == frozenset({"slack-api"})


def test_resolved_schema_names_terminates_on_a_reference_cycle() -> None:
    schemas = {
        "a": {"allOf": [{"$ref": "#/$defs/b"}]},
        "b": {"allOf": [{"$ref": "#/$defs/a"}]},
    }

    assert resolved_schema_names("a", schemas) == frozenset({"a", "b"})


def test_resolved_schema_names_ignores_pointers_into_a_definition() -> None:
    # Detent lets a pointer reach into a definition; the enclosing definition is
    # what identifies the schema.
    schemas = {"custom": {"properties": {"domain": {"$ref": "#/$defs/slack-api/properties/domain"}}}}

    assert resolved_schema_names("custom", schemas) == frozenset({"custom", "slack-api"})


# -- Cross-language drift guards ------------------------------------------------


def _run_node(script: str) -> str:
    assert _NODE_BINARY is not None
    result = subprocess.run(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert result.returncode == 0, f"node exited {result.returncode}: {result.stderr}"
    return result.stdout


@pytest.mark.skipif(_NODE_BINARY is None, reason="node binary not available on PATH")
def test_extension_copy_of_the_separator_matches() -> None:
    """The ``permission_requests`` extension's separator equals the Python one."""
    source = (_EXTENSIONS_DIR / "permission_requests.mjs").read_text()
    assert f"const ACCOUNT_SCOPE_SEPARATOR = '{ACCOUNT_SCOPE_SEPARATOR}';" in source


@pytest.mark.skipif(_NODE_BINARY is None, reason="node binary not available on PATH")
def test_extension_copy_of_the_grant_builders_matches(tmp_path: Path) -> None:
    """The extension's key + schema builders produce exactly the Python ones.

    They are module-private, so the guard copies the extension (with them
    exported) into a temp directory, imports it with Node, and compares its
    output for a named account, the default account, and an account carrying the
    naming separator. The copy lives outside the package tree so a crashed run
    can never leave a stray ``.mjs`` where a real gateway would load it as an
    extension; the JSON data files the extension reads at module load are copied
    next to it.
    """
    source = (_EXTENSIONS_DIR / "permission_requests.mjs").read_text()
    for exported in (
        "function accountScopeKey(scope, account) {",
        "function buildAccountScopeSchema(scope, account) {",
    ):
        assert exported in source
        source = source.replace(exported, f"export {exported}")
    for data_file in _EXTENSIONS_DIR.glob("*.json"):
        (tmp_path / data_file.name).write_text(data_file.read_text())
    patched_path = tmp_path / "permission_requests.mjs"
    patched_path.write_text(source)

    cases = (
        ("slack-api", "hynek@imbue-ai"),
        ("github-rest-api", ""),
        # Names that exercise the scope half's escaping (and an account that is
        # deliberately left verbatim).
        ("odd:scope", "we:ird@example.com"),
        ("odd%scope", "100%@example.com"),
        ("odd%3Ascope", "a@b"),
        # Several occurrences: JavaScript's ``String.replace`` with a string
        # pattern only replaces the first one, so this pins the JS copy to
        # replacing all of them (as Python's ``str.replace`` does).
        ("a:b:c", "a@b"),
        ("a%b%c", "a@b"),
    )
    script = (
        f"import {{ accountScopeKey, buildAccountScopeSchema }} from {json.dumps(patched_path.as_uri())};\n"
        f"const cases = {json.dumps([list(case) for case in cases])};\n"
        "process.stdout.write(JSON.stringify(cases.map(([scope, account]) => [\n"
        "  accountScopeKey(scope, account),\n"
        "  buildAccountScopeSchema(scope, account),\n"
        "])));\n"
    )
    from_node = json.loads(_run_node(script))

    assert from_node == [
        [account_scope_key(scope, account), build_account_scope_schema(scope, account)] for scope, account in cases
    ]
