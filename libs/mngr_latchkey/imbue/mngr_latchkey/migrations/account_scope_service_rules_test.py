"""Unit tests for data-format migration 2 (per-account service grants)."""

import json
from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_scope_schema
from imbue.mngr_latchkey.baseline_permissions import AGENT_BASELINE_PERMISSIONS
from imbue.mngr_latchkey.migrations.account_scope_service_rules import AccountScopeServiceRules
from imbue.mngr_latchkey.migrations.account_scope_service_rules import _PermissionsFile
from imbue.mngr_latchkey.migrations.account_scope_service_rules import merge_account_rules_into_service_rules
from imbue.mngr_latchkey.migrations.account_scope_service_rules import split_service_rules_by_account
from imbue.mngr_latchkey.migrations.interface import LatchkeyMigrationError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions


def _fake_auth_list_binary(tmp_path: Path, accounts_by_service: dict[str, list[str]]) -> Path:
    """Write a fake ``latchkey`` CLI whose ``auth list`` reports these stored accounts.

    The migration reads the credential store by shelling out, so its tests give
    it a real (if tiny) binary to shell out to, in latchkey's own output shape.
    """
    payload = json.dumps(
        {
            service: {account: {"credentialType": "rawCurl", "credentialStatus": "unknown"} for account in accounts}
            for service, accounts in accounts_by_service.items()
        }
    )
    binary = tmp_path / "latchkey"
    binary.write_text(
        "#!/usr/bin/env python3\nimport sys\nassert sys.argv[1:] == ['auth', 'list', '--offline'], sys.argv\n"
        f"print({payload!r})\n"
    )
    binary.chmod(0o755)
    return binary


# What the fake ``latchkey auth list`` reports, and the per-scope view of it the
# pure transforms take (``linear`` is signed out entirely).
_ACCOUNTS_BY_SERVICE = {
    "slack": ["alice@example.com", "bob@example.com"],
    "github": [""],
}
_ACCOUNTS_BY_SCOPE = {
    "slack-api": ("alice@example.com", "bob@example.com"),
    "github-rest-api": ("",),
    "linear-api": (),
}

_LEGACY_CONFIG = LatchkeyPermissionsConfig(
    rules=(
        {"slack-api": ["slack-read-all"]},
        {"linear-api": ["any"]},
        {"latchkey-self": ["latchkey-self-read-self-permissions"]},
    ),
)


def test_up_replaces_a_service_rule_with_one_rule_per_stored_account() -> None:
    migrated = split_service_rules_by_account(
        _PermissionsFile(rules=({"slack-api": ["slack-read-all"]},)),
        _ACCOUNTS_BY_SCOPE,
    )

    assert migrated.rules == (
        {account_scope_key("slack-api", "alice@example.com"): ["slack-read-all"]},
        {account_scope_key("slack-api", "bob@example.com"): ["slack-read-all"]},
    )
    # Each new key is backed by its generated schema, so the rules resolve.
    assert migrated.schemas == {
        account_scope_key("slack-api", "alice@example.com"): build_account_scope_schema(
            "slack-api", "alice@example.com"
        ),
        account_scope_key("slack-api", "bob@example.com"): build_account_scope_schema("slack-api", "bob@example.com"),
    }


def test_up_keeps_the_default_account_as_a_gated_account() -> None:
    migrated = split_service_rules_by_account(
        _PermissionsFile(rules=({"github-rest-api": ["github-read-all"]},)),
        _ACCOUNTS_BY_SCOPE,
    )

    assert migrated.rules == ({account_scope_key("github-rest-api", ""): ["github-read-all"]},)


def test_up_drops_a_service_rule_with_no_stored_account() -> None:
    # Nothing could ever be injected under it, so the grant is inert; the agent
    # asks again once the user signs in.
    migrated = split_service_rules_by_account(
        _PermissionsFile(rules=({"linear-api": ["any"]},)),
        _ACCOUNTS_BY_SCOPE,
    )

    assert migrated.rules == ()


def test_up_leaves_non_service_rules_untouched() -> None:
    gateway_self = {"latchkey-self": ["latchkey-self-read-self-permissions"]}
    config = _PermissionsFile(rules=(gateway_self, {"any": ["any"]}))

    assert split_service_rules_by_account(config, _ACCOUNTS_BY_SCOPE) == config


def test_up_is_idempotent_on_an_already_migrated_file() -> None:
    once = split_service_rules_by_account(
        _PermissionsFile(rules=({"slack-api": ["slack-read-all"]},)),
        _ACCOUNTS_BY_SCOPE,
    )

    assert split_service_rules_by_account(once, _ACCOUNTS_BY_SCOPE) == once


def test_down_folds_the_per_account_rules_back_onto_the_base_scope() -> None:
    migrated = split_service_rules_by_account(
        _PermissionsFile(
            rules=(
                {"slack-api": ["slack-read-all"]},
                {"latchkey-self": ["latchkey-self-read-self-permissions"]},
            )
        ),
        _ACCOUNTS_BY_SCOPE,
    )

    reverted = merge_account_rules_into_service_rules(migrated, _ACCOUNTS_BY_SCOPE)

    assert reverted.rules == (
        {"slack-api": ["slack-read-all"]},
        {"latchkey-self": ["latchkey-self-read-self-permissions"]},
    )
    # The generated schemas are gone again.
    assert reverted.schemas == {}


def test_down_unions_permissions_granted_to_different_accounts() -> None:
    per_account = _PermissionsFile(
        rules=(
            {account_scope_key("slack-api", "alice@example.com"): ["slack-read-all"]},
            {account_scope_key("slack-api", "bob@example.com"): ["slack-write-all"]},
        ),
        schemas={
            account_scope_key("slack-api", account): build_account_scope_schema("slack-api", account)
            for account in ("alice@example.com", "bob@example.com")
        },
    )

    reverted = merge_account_rules_into_service_rules(per_account, _ACCOUNTS_BY_SCOPE)

    assert reverted.rules == ({"slack-api": ["slack-read-all", "slack-write-all"]},)


def test_apply_up_rewrites_every_host_file(tmp_path: Path) -> None:
    first_host = HostId.generate()
    second_host = HostId.generate()
    for host_id in (first_host, second_host):
        save_permissions(permissions_path_for_host(tmp_path, host_id), _LEGACY_CONFIG)

    binary = _fake_auth_list_binary(tmp_path, _ACCOUNTS_BY_SERVICE)

    AccountScopeServiceRules(version=2).apply_up(tmp_path, tmp_path, str(binary))

    for host_id in (first_host, second_host):
        migrated = json.loads(permissions_path_for_host(tmp_path, host_id).read_text())
        rule_keys = [next(iter(rule)) for rule in migrated["rules"]]
        assert rule_keys == [
            account_scope_key("slack-api", "alice@example.com"),
            account_scope_key("slack-api", "bob@example.com"),
            "latchkey-self",
        ]


def test_apply_up_resets_an_unparsable_file_to_the_fresh_host_defaults(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = permissions_path_for_host(tmp_path, host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json")
    binary = _fake_auth_list_binary(tmp_path, _ACCOUNTS_BY_SERVICE)

    AccountScopeServiceRules(version=2).apply_up(tmp_path, tmp_path, str(binary))

    # The host keeps working from a clean slate rather than the whole migration
    # (and with it every ``Latchkey.initialize()``) failing.
    assert json.loads(path.read_text()) == json.loads(AGENT_BASELINE_PERMISSIONS.model_dump_json())


def test_apply_up_does_not_ask_for_accounts_when_there_is_nothing_to_migrate(tmp_path: Path) -> None:
    # A fresh install has no host files, so the migration must not shell out to
    # latchkey at all -- pointing it at a binary that does not exist proves it.
    AccountScopeServiceRules(version=2).apply_up(tmp_path, tmp_path, str(tmp_path / "does-not-exist"))


def test_apply_up_aborts_when_the_accounts_cannot_be_listed(tmp_path: Path) -> None:
    # Reading a failed listing as "no accounts anywhere" would drop every
    # service grant on the machine, so the migration refuses to guess.
    host_id = HostId.generate()
    path = permissions_path_for_host(tmp_path, host_id)
    save_permissions(path, _LEGACY_CONFIG)
    before = path.read_text()

    with pytest.raises(LatchkeyMigrationError):
        AccountScopeServiceRules(version=2).apply_up(tmp_path, tmp_path, str(tmp_path / "does-not-exist"))

    assert path.read_text() == before
