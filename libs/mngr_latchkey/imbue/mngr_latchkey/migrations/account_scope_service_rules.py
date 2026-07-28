"""Data-format migration 2: make third-party service grants per-account.

Earlier builds granted a third-party service with a single account-agnostic
rule (``{"slack-api": ["slack-read-all"]}``), so every account the user had
signed in to shared one grant. Latchkey >= 3.2.0 reports the account whose
credentials it injects to detent, and detent >= 1.11.0 lets one schema compose
with another, so a grant can now name the account it applies to: the rule key
becomes ``slack-api:<account>`` and the file carries a generated schema
intersecting the built-in ``slack-api`` scope with that account (see
:mod:`imbue.mngr_latchkey.account_scopes` for the live format).

``apply_up`` rewrites each per-host permissions file into that shape: every
rule whose key is a catalog service scope is replaced by one rule per account
currently stored for that service. A service with *no* stored account loses
its rule entirely -- there are no credentials to inject under it, so the grant
could never have applied, and the agent simply asks again once the user signs
in. Rules for minds' own gateway-self scopes (``latchkey-self``,
``minds-api-proxy-*``) are account-agnostic by construction (latchkey attaches
no account metadata to requests an extension serves) and are left alone.
``apply_down`` folds the per-account rules back onto their base scope, unioning
the permissions and dropping the generated schemas.

A file that cannot be read, parsed, or rewritten is *replaced* with the
freshly-created-host default (:data:`AGENT_BASELINE_PERMISSIONS`) rather than
left in a shape the new code cannot reason about: the host keeps working (its
agents re-register themselves and can re-request grants) instead of the whole
migration -- and with it every ``Latchkey.initialize()`` -- failing.

Which accounts exist is not derivable from the files under ``plugin_data_dir``,
so this migration asks latchkey itself (``latchkey auth list --offline``) using
the directory and binary the runner hands it, and maps the answer onto the
catalog's service scopes. That call happens only once there is at least one host
file to rewrite, so a fresh install never shells out. A failure to list the
accounts aborts the migration rather than being read as "no accounts anywhere",
which would drop every service grant.

Following the same doctrine as the previous migration, the on-disk shapes this
module reads and writes are frozen local copies rather than imports from the
live modules: a migration is a historical artifact and must keep performing the
exact same rewrite even if the live format later changes. The one deliberate
exception is the reset-to-defaults fallback, whose intent is precisely "make
this file look like a host created by the *current* code".
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.baseline_permissions import AGENT_BASELINE_PERMISSIONS
from imbue.mngr_latchkey.encryption_key import inject_encryption_key_into_env
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.migrations.interface import DataFormatMigration
from imbue.mngr_latchkey.migrations.interface import LatchkeyMigrationError
from imbue.mngr_latchkey.services_catalog import ServicesCatalog

# Frozen copy of the per-account rule-key format and generated schema this
# migration produces (the live definitions are in
# ``imbue.mngr_latchkey.account_scopes``).
_ACCOUNT_SCOPE_SEPARATOR: Final[str] = ":"
_SCOPE_ESCAPES: Final[tuple[tuple[str, str], ...]] = (("%", "%25"), (_ACCOUNT_SCOPE_SEPARATOR, "%3A"))
_SCHEMA_REFERENCE_PREFIX: Final[str] = "#/$defs/"

# The per-host on-disk layout this migration walks. A frozen copy of the store's
# private layout constants so the migration does not ride on the live values.
_HOSTS_DIR_NAME: Final[str] = "hosts"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"

# How long to wait for ``latchkey auth list --offline`` -- a local, no-network
# read of the credential store, so a generous bound only guards a wedged CLI.
_AUTH_LIST_TIMEOUT_SECONDS: Final[float] = 30.0


class _PermissionsFile(FrozenModel):
    """Migration-local, frozen view of a permissions file.

    A deliberately independent copy of the parts this migration reads and
    rewrites (the ``rules`` array and ``schemas`` object), so the migration
    keeps producing the same historical transform even if
    ``store.LatchkeyPermissionsConfig`` later changes.
    """

    model_config = ConfigDict(extra="ignore")

    rules: tuple[dict[str, list[str]], ...] = Field(default_factory=tuple)
    schemas: dict[str, JsonValue] = Field(default_factory=dict)


@pure
def _account_scope_key(scope: str, account: str) -> str:
    """Frozen copy of :func:`imbue.mngr_latchkey.account_scopes.account_scope_key`.

    Including its percent-escaping of the scope half, which is what keeps the
    (scope, account) -> key mapping injective. It is the identity for every scope
    name the catalog ships, so the keys this migration writes read exactly like
    ``slack-api:hynek@imbue-ai``.
    """
    encoded_scope = scope
    for character, escape in _SCOPE_ESCAPES:
        encoded_scope = encoded_scope.replace(character, escape)
    return f"{encoded_scope}{_ACCOUNT_SCOPE_SEPARATOR}{account}"


@pure
def _base_scope_of_generated_schema(schema: JsonValue | None) -> str | None:
    """Recover the base scope a generated per-account schema composes.

    A frozen copy of the reading half of
    :func:`imbue.mngr_latchkey.account_scopes.resolve_account_scope`, narrowed to
    what the down-migration needs. It inspects the *structure* -- the ``$ref`` to
    the base scope next to a ``customMetadata.account`` gate -- rather than
    splitting the rule key, whose name is only a convention (both a scope and an
    account may contain the separator).
    """
    if not isinstance(schema, dict):
        return None
    members = schema.get("allOf")
    if not isinstance(members, list):
        return None
    scopes: list[str] = []
    is_account_gated = False
    for member in members:
        if not isinstance(member, dict):
            continue
        reference = member.get("$ref")
        if isinstance(reference, str) and reference.startswith(_SCHEMA_REFERENCE_PREFIX):
            scopes.append(reference.removeprefix(_SCHEMA_REFERENCE_PREFIX).split("/", 1)[0])
        properties = member.get("properties")
        custom_metadata = properties.get("customMetadata") if isinstance(properties, dict) else None
        metadata_properties = custom_metadata.get("properties") if isinstance(custom_metadata, dict) else None
        account_gate = metadata_properties.get("account") if isinstance(metadata_properties, dict) else None
        if isinstance(account_gate, dict) and isinstance(account_gate.get("const"), str):
            is_account_gated = True
    if not is_account_gated or len(scopes) != 1 or not scopes[0]:
        return None
    return scopes[0]


@pure
def _build_account_scope_schema(scope: str, account: str) -> dict[str, JsonValue]:
    """Frozen copy of :func:`imbue.mngr_latchkey.account_scopes.build_account_scope_schema`."""
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
def split_service_rules_by_account(
    config: _PermissionsFile,
    accounts_by_service_scope: dict[str, tuple[str, ...]],
) -> _PermissionsFile:
    """Replace each account-agnostic service rule with one rule per stored account.

    ``accounts_by_service_scope`` maps every *catalog* service scope to the
    accounts currently stored for its service (possibly none). A rule whose key
    is absent from that mapping is not a third-party service grant (or is
    already account-scoped) and is copied through untouched. A no-op (returns an
    equal config) when the file holds no account-agnostic service rule, so
    re-running the migration is safe.
    """
    rebuilt_rules: list[dict[str, list[str]]] = []
    rebuilt_schemas: dict[str, JsonValue] = dict(config.schemas)
    is_changed = False
    for rule in config.rules:
        scope = next(iter(rule)) if len(rule) == 1 else None
        accounts = None if scope is None else accounts_by_service_scope.get(scope)
        if scope is None or accounts is None:
            rebuilt_rules.append(dict(rule))
            continue
        is_changed = True
        # Drop the generated scope schema slot for the legacy key, if any:
        # the legacy key referenced a built-in schema by name, never a
        # file-local one, so nothing else in the file can be pointing at it.
        rebuilt_schemas.pop(scope, None)
        if not accounts:
            logger.info(
                "Dropping latchkey rule for scope {} while migrating to per-account grants: "
                "the service has no stored account, so the grant could not apply to anything",
                scope,
            )
            continue
        for account in accounts:
            key = _account_scope_key(scope, account)
            rebuilt_rules.append({key: list(rule[scope])})
            rebuilt_schemas[key] = _build_account_scope_schema(scope, account)
    if not is_changed:
        return config
    return _PermissionsFile(rules=tuple(rebuilt_rules), schemas=rebuilt_schemas)


@pure
def merge_account_rules_into_service_rules(
    config: _PermissionsFile,
    accounts_by_service_scope: dict[str, tuple[str, ...]],
) -> _PermissionsFile:
    """Fold per-account rules back onto their base scope, dropping the generated schemas.

    The inverse of :func:`split_service_rules_by_account`. Which rules are
    per-account grants (and of what) is read off each rule's *schema*, not its
    key. The first per-account rule for a scope becomes the base-scope rule in
    that position; later ones union their permissions into it. A no-op when the
    file holds no per-account service rule.
    """
    rebuilt_rules: list[dict[str, list[str]]] = []
    rebuilt_schemas: dict[str, JsonValue] = dict(config.schemas)
    index_by_scope: dict[str, int] = {}
    is_changed = False
    for rule in config.rules:
        key = next(iter(rule)) if len(rule) == 1 else None
        scope = None if key is None else _base_scope_of_generated_schema(config.schemas.get(key))
        if key is None or scope is None or scope not in accounts_by_service_scope:
            rebuilt_rules.append(dict(rule))
            continue
        is_changed = True
        rebuilt_schemas.pop(key, None)
        existing_index = index_by_scope.get(scope)
        if existing_index is None:
            index_by_scope[scope] = len(rebuilt_rules)
            rebuilt_rules.append({scope: list(rule[key])})
            continue
        merged = list(rebuilt_rules[existing_index][scope])
        for permission in rule[key]:
            if permission not in merged:
                merged.append(permission)
        rebuilt_rules[existing_index] = {scope: merged}
    if not is_changed:
        return config
    return _PermissionsFile(rules=tuple(rebuilt_rules), schemas=rebuilt_schemas)


def _read_stored_accounts_by_service(latchkey_directory: Path, latchkey_binary: str) -> dict[str, tuple[str, ...]]:
    """Ask latchkey which accounts it has credentials stored for, keyed by service.

    Runs ``latchkey auth list --offline`` (a local read of the credential store,
    no network) and returns ``{service: (account, ...)}``, where the unnamed
    default account is the empty string. The spawn is set up the way every other
    local latchkey invocation is -- ``LATCHKEY_DIRECTORY`` pinned and the
    per-directory encryption key injected, so latchkey never falls through to the
    system keychain (which would pop a dialog on macOS).

    Any failure raises :class:`LatchkeyMigrationError`: mistaking a broken CLI
    for "the user has no accounts" would make this migration drop every service
    grant on the machine.
    """
    env = dict(os.environ)
    env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    inject_encryption_key_into_env(env, load_or_create_encryption_key(latchkey_directory))
    command = [latchkey_binary, "auth", "list", "--offline"]
    concurrency_group = ConcurrencyGroup(name="latchkey-migration-auth-list")
    try:
        with concurrency_group:
            result = concurrency_group.run_process_to_completion(
                command=command,
                timeout=_AUTH_LIST_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=env,
            )
    except ConcurrencyExceptionGroup as group:
        raise LatchkeyMigrationError(f"Could not run '{' '.join(command)}' to migrate permissions: {group}") from group
    if result.returncode != 0:
        raise LatchkeyMigrationError(
            f"'{' '.join(command)}' exited {result.returncode} while migrating permissions: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise LatchkeyMigrationError(f"Could not parse '{' '.join(command)}' output as JSON: {e}") from e
    if not isinstance(payload, dict):
        raise LatchkeyMigrationError(f"'{' '.join(command)}' returned non-object JSON: {payload!r}")
    # The value is an object keyed by account name; only the keys matter here.
    return {
        str(service_name): tuple(str(account) for account in account_map)
        for service_name, account_map in payload.items()
        if isinstance(account_map, dict)
    }


def _accounts_by_service_scope(latchkey_directory: Path, latchkey_binary: str) -> dict[str, tuple[str, ...]]:
    """Map every catalog service scope to the accounts stored for its service.

    Deliberately consults the *live* services catalog rather than a frozen copy:
    "which rule keys are third-party service scopes" is a property of the build
    doing the migration, and a scope the installed build no longer knows about
    needs no rewriting (its grant is already inert).
    """
    accounts_by_service = _read_stored_accounts_by_service(latchkey_directory, latchkey_binary)
    return {
        info.scope: accounts_by_service.get(service_name, ())
        for service_name, infos in ServicesCatalog().as_mapping().items()
        for info in infos
    }


def _iter_host_permission_files(plugin_data_dir: Path) -> list[Path]:
    """Return every existing per-host ``latchkey_permissions.json`` under ``plugin_data_dir``."""
    hosts_root = plugin_data_dir / _HOSTS_DIR_NAME
    if not hosts_root.is_dir():
        return []
    paths = [
        host_dir / _PERMISSIONS_FILENAME
        for host_dir in hosts_root.iterdir()
        if host_dir.is_dir() and (host_dir / _PERMISSIONS_FILENAME).is_file()
    ]
    return sorted(paths)


def _read_permissions_file(path: Path) -> _PermissionsFile:
    """Parse a permissions file into the migration-local model."""
    return _PermissionsFile.model_validate_json(path.read_text())


def _write_permissions_file_json(path: Path, serialized: str) -> None:
    """Atomically (re)write a permissions file (mode 0600), mirroring the store's write."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(serialized)
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)


def _reset_to_fresh_host_defaults(path: Path) -> None:
    """Overwrite an unmigratable file with what a freshly-created host would have.

    Deliberately reaches for the *live* baseline rather than a frozen copy: the
    point is to leave behind exactly the file the current code would create for
    a new host. The host's agents lose their grants (they can ask again) and
    their Minds API registrations (minds re-registers them on discovery), which
    is a far better outcome than refusing to start.
    """
    _write_permissions_file_json(path, AGENT_BASELINE_PERMISSIONS.model_dump_json(indent=2))


# Type of the pure per-file transform each migration direction dispatches to.
_ConfigTransform = Callable[[_PermissionsFile, dict[str, tuple[str, ...]]], _PermissionsFile]


class AccountScopeServiceRules(DataFormatMigration):
    """Rewrite every per-host permissions file between per-service and per-account grants."""

    def apply_up(self, plugin_data_dir: Path, latchkey_directory: Path, latchkey_binary: str) -> None:
        self._rewrite_each_host_file(
            plugin_data_dir, latchkey_directory, latchkey_binary, split_service_rules_by_account
        )

    def apply_down(self, plugin_data_dir: Path, latchkey_directory: Path, latchkey_binary: str) -> None:
        self._rewrite_each_host_file(
            plugin_data_dir, latchkey_directory, latchkey_binary, merge_account_rules_into_service_rules
        )

    def _rewrite_each_host_file(
        self,
        plugin_data_dir: Path,
        latchkey_directory: Path,
        latchkey_binary: str,
        transform: _ConfigTransform,
    ) -> None:
        paths = _iter_host_permission_files(plugin_data_dir)
        if not paths:
            return
        # Listing the accounts spawns latchkey, so it happens only once there is
        # at least one file to rewrite (a fresh install has none).
        accounts_by_service_scope = _accounts_by_service_scope(latchkey_directory, latchkey_binary)
        for path in paths:
            try:
                config = _read_permissions_file(path)
                transformed = transform(config, accounts_by_service_scope)
            except (OSError, ValidationError, ValueError) as e:
                logger.warning(
                    "Could not migrate latchkey permissions file {} to the per-account format ({}); "
                    "resetting it to the defaults a freshly-created host gets",
                    path,
                    e,
                )
                _reset_to_fresh_host_defaults(path)
                continue
            if transformed == config:
                continue
            logger.debug("Migrating permissions file {} for data-format change", path)
            try:
                _write_permissions_file_json(path, transformed.model_dump_json(indent=2))
            except OSError as e:
                logger.warning(
                    "Could not write the migrated latchkey permissions file {} ({}); "
                    "resetting it to the defaults a freshly-created host gets",
                    path,
                    e,
                )
                _reset_to_fresh_host_defaults(path)
