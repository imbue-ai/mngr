"""Per-account detent grants: generating them, and reading them back structurally.

Latchkey >= 3.2.0 reports the account whose credentials it injects into a
proxied request to detent as the ``account`` key of ``customMetadata`` (the
unnamed "default" account is reported as the empty string, see
:data:`imbue.mngr_latchkey.core.DEFAULT_ACCOUNT`). Detent >= 1.11.0 lets a
named schema compose with another one via ``{"$ref": "#/$defs/<name>"}``.

Together those two features let a permissions file grant a third-party
service *per account* rather than per service: instead of one
``{"slack-api": [...]}`` rule covering every Slack account the user has
signed in to, each account gets its own rule, backed by a generated schema
that intersects the built-in ``slack-api`` scope with an ``account``
equality check::

    {
      "rules": [{"slack-api:hynek@imbue-ai": ["slack-read-all"]}],
      "schemas": {
        "slack-api:hynek@imbue-ai": {
          "allOf": [
            {"$ref": "#/$defs/slack-api"},
            {"properties": {"customMetadata": {"type": "object",
               "properties": {"account": {"const": "hynek@imbue-ai"}},
               "required": ["account"]}},
             "required": ["customMetadata"]}
          ]
        }
      }
    }

Because detent stops at the first rule whose *scope* matches, per-account
scopes compose cleanly: a Slack request made with account ``a`` simply does
not match ``slack-api:b``'s scope and falls through to the next rule.

The `<scope>:<account>` **name is only a naming convention** -- a stable,
human-readable identifier -- and is never parsed: both a detent scope name and
an account may legitimately contain a colon, so splitting the name would be
ambiguous. (The scope half is percent-escaped anyway, so that distinct pairs
cannot share a key; see :func:`account_scope_key`.) Everything that needs to
know what a rule grants inspects the *schema structure* instead, via
:func:`resolve_account_scope` /
:func:`list_account_grants` / :func:`resolved_schema_names`. This module is the
single place that knows either side of that structure, so a change to the
generated shape only has to be made here.

Only catalog-backed third-party service scopes are account-scoped. Minds'
own gateway-self scopes (``latchkey-self``, ``minds-api-proxy-*``) must stay
account-agnostic: latchkey attaches no ``customMetadata`` at all to requests
it serves from a gateway extension, so an account-gated schema would never
match them.

The gateway's ``permission_requests`` extension carries a JavaScript copy of
the two *generating* helpers (it computes a pending request's effect in-process,
with no Python available); the cross-language drift guard lives in
``account_scopes_test.py``. Nothing on the JavaScript side reads grants back.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

# Separator between the base detent scope and the account in a generated rule
# key. The key is an opaque identifier and is never split (see the module
# docstring); the separator exists so the name reads well.
ACCOUNT_SCOPE_SEPARATOR: Final[str] = ":"

# Percent-escapes applied to the *scope* half of a generated key so that
# distinct (scope, account) pairs can never produce the same key -- see
# :func:`account_scope_key`. Ordered: the escape character itself must be
# escaped first, otherwise the second pass would escape the ``%`` the first
# pass just introduced (``":"`` and ``"%3A"`` would both become ``"%253A"``).
_SCOPE_ESCAPES: Final[tuple[tuple[str, str], ...]] = (("%", "%25"), (ACCOUNT_SCOPE_SEPARATOR, "%3A"))

# JSON-pointer prefix detent resolves named schemas through.
_SCHEMA_REFERENCE_PREFIX: Final[str] = "#/$defs/"

# Bound on how deep the ``$ref`` closure walk follows file-local schemas. The
# generated shape is one level deep; the bound only guards a hand-edited file
# with a pathological chain (cycles are already handled by the visited set).
_MAX_SCHEMA_REFERENCE_DEPTH: Final[int] = 16


class AccountScopeGrant(FrozenModel):
    """One rule of a permissions file that grants a single account of one scope."""

    rule_key: str = Field(
        description=(
            "The rule's key in the file, i.e. the name of its generated schema. Opaque: pass it "
            "back to the gateway to rewrite or delete the rule, but never parse it."
        ),
    )
    scope: str = Field(description="Base detent scope the generated schema composes (e.g. ``slack-api``).")
    account: str = Field(
        description='Latchkey account the grant is pinned to (``""`` for the unnamed default account).',
    )
    permissions: tuple[str, ...] = Field(description="Permission schema names granted under the rule.")


@pure
def account_scope_key(scope: str, account: str) -> str:
    """Return the rule key (and generated schema name) granting ``scope`` for ``account``.

    The name embeds both parts almost verbatim so a permissions file stays
    readable, and the same (scope, account) pair always maps to the same key --
    which is what makes re-granting an idempotent overwrite. It carries no
    meaning beyond that; readers go through :func:`resolve_account_scope`.

    Distinct pairs must never share a key, though, or one grant would overwrite
    (or, through the gateway's merge-by-key, quietly widen) another. A plain
    ``f"{scope}:{account}"`` does not guarantee that -- ``("slack:", "a")`` and
    ``("slack", ":a")`` would collide -- so the *scope* half is percent-escaped
    (``%`` -> ``%25``, then ``:`` -> ``%3A``). The encoded scope then contains no
    separator at all, which makes the first separator in the key unambiguously
    the boundary and the mapping injective. The account needs no escaping: it is
    the last field, so it may contain anything, including separators and escape
    sequences.

    No detent scope name contains either character today, so this is the
    identity for every scope we ship; it exists so an exotic future name cannot
    silently merge two grants.
    """
    encoded_scope = scope
    for character, escape in _SCOPE_ESCAPES:
        encoded_scope = encoded_scope.replace(character, escape)
    return f"{encoded_scope}{ACCOUNT_SCOPE_SEPARATOR}{account}"


@pure
def build_account_scope_schema(scope: str, account: str) -> dict[str, JsonValue]:
    """Build the generated schema backing :func:`account_scope_key`.

    The schema intersects the named base ``scope`` (referenced through
    detent's ``#/$defs/`` mechanism, which resolves both built-in and
    file-local schemas) with an exact match on the injected account. The
    base scope must exist by the time the file is evaluated: detent fails
    the *entire* permission check when a referenced schema is unknown.
    """
    return {
        "allOf": [
            {"$ref": f"{_SCHEMA_REFERENCE_PREFIX}{scope}"},
            {
                "properties": {
                    "customMetadata": {
                        "type": "object",
                        "properties": {"account": {"const": account}},
                        "required": ["account"],
                    },
                },
                "required": ["customMetadata"],
            },
        ],
    }


@pure
def build_account_grant(
    scope: str,
    account: str,
    permissions: Sequence[str],
) -> tuple[str, tuple[str, ...], dict[str, JsonValue]]:
    """Assemble everything needed to write one per-account grant.

    Returns ``(rule_key, permissions, schemas)``: the rule key to upsert, the
    permission names to put under it, and the schema definitions that must land
    in the file's ``schemas`` object for that key to resolve. Callers hand the
    triple straight to the gateway's ``permissions`` extension, which only
    merges what it is given -- this module is the sole author of the shape.
    """
    rule_key = account_scope_key(scope, account)
    return rule_key, tuple(permissions), {rule_key: build_account_scope_schema(scope, account)}


def _as_object(value: JsonValue | None) -> Mapping[str, JsonValue] | None:
    """Narrow a JSON value to an object, or ``None`` when it is anything else."""
    return value if isinstance(value, dict) else None


def _referenced_schema_name(value: JsonValue) -> str | None:
    """Return the schema name a ``{"$ref": "#/$defs/<name>"}`` node points at."""
    node = _as_object(value)
    if node is None:
        return None
    reference = node.get("$ref")
    if not isinstance(reference, str) or not reference.startswith(_SCHEMA_REFERENCE_PREFIX):
        return None
    # Detent lets a pointer reach *into* a definition
    # (``#/$defs/<name>/properties/domain``); the enclosing definition is what
    # matters here.
    return reference.removeprefix(_SCHEMA_REFERENCE_PREFIX).split("/", 1)[0] or None


def _account_from_metadata_gate(value: JsonValue) -> str | None:
    """Return the account a ``customMetadata.account`` const gate pins, if any."""
    node = _as_object(value)
    if node is None:
        return None
    properties = _as_object(node.get("properties"))
    custom_metadata = None if properties is None else _as_object(properties.get("customMetadata"))
    metadata_properties = None if custom_metadata is None else _as_object(custom_metadata.get("properties"))
    account_gate = None if metadata_properties is None else _as_object(metadata_properties.get("account"))
    if account_gate is None:
        return None
    account = account_gate.get("const")
    return account if isinstance(account, str) else None


@pure
def resolve_account_scope(schema: JsonValue) -> tuple[str, str] | None:
    """Recover ``(scope, account)`` from a generated per-account schema.

    Inspects the *structure* produced by :func:`build_account_scope_schema` --
    an ``allOf`` whose members are a ``$ref`` to the base scope and a
    ``customMetadata.account`` const gate -- and returns ``None`` for anything
    else (a plain scope schema, a hand-written schema, a file-sharing
    permission, ...). The rule key is never consulted, so a scope or an account
    containing the naming convention's separator resolves correctly.
    """
    node = _as_object(schema)
    if node is None:
        return None
    members = node.get("allOf")
    if not isinstance(members, list):
        return None
    scopes = [name for name in (_referenced_schema_name(member) for member in members) if name is not None]
    accounts = [
        account for account in (_account_from_metadata_gate(member) for member in members) if account is not None
    ]
    # Exactly one of each: anything else is not the shape we generate, and
    # guessing would risk reporting a grant as narrower than it really is.
    if len(scopes) != 1 or len(accounts) != 1:
        return None
    return scopes[0], accounts[0]


@pure
def list_account_grants(config: LatchkeyPermissionsConfig) -> tuple[AccountScopeGrant, ...]:
    """Return every per-account grant in ``config``, in file order.

    Rules whose schema is not a generated per-account schema (the gateway-self
    scopes, hand-edited rules, legacy account-agnostic service rules) are
    skipped, so callers see exactly the grants this module wrote.
    """
    grants: list[AccountScopeGrant] = []
    for rule in config.rules:
        if len(rule) != 1:
            continue
        rule_key = next(iter(rule))
        resolved = resolve_account_scope(config.schemas.get(rule_key))
        if resolved is None:
            continue
        scope, account = resolved
        grants.append(
            AccountScopeGrant(
                rule_key=rule_key,
                scope=scope,
                account=account,
                permissions=tuple(rule[rule_key]),
            )
        )
    return tuple(grants)


@pure
def resolved_schema_names(rule_key: str, schemas: Mapping[str, JsonValue]) -> frozenset[str]:
    """Return every schema name a rule key resolves to: itself plus its ``$ref`` closure.

    A rule key is a schema name, which may be defined in the file (like the
    generated per-account schemas) and may in turn reference other named schemas
    -- built-in ones such as ``slack-api``, or further file-local ones. Callers
    that need to know *which* underlying scopes a rule can match (e.g. mapping a
    grant back to the service whose credentials it needs) intersect this set
    with the names they care about, instead of reading anything into the key.
    """
    names: set[str] = {rule_key}
    pending: list[tuple[str, int]] = [(rule_key, 0)]
    while pending:
        name, depth = pending.pop()
        definition = schemas.get(name)
        if definition is None or depth >= _MAX_SCHEMA_REFERENCE_DEPTH:
            continue
        for referenced in _iter_referenced_schema_names(definition):
            if referenced not in names:
                names.add(referenced)
                pending.append((referenced, depth + 1))
    return frozenset(names)


def _iter_referenced_schema_names(value: JsonValue) -> list[str]:
    """Collect every ``#/$defs/<name>`` reference anywhere inside a JSON value."""
    found: list[str] = []
    stack: list[JsonValue] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        referenced = _referenced_schema_name(node)
        if referenced is not None:
            found.append(referenced)
        stack.extend(node.values())
    return found
