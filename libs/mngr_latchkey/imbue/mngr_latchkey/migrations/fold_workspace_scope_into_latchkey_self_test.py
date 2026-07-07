import json
from pathlib import Path

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.migrations.fold_workspace_scope_into_latchkey_self import FoldWorkspaceScopeIntoLatchkeySelf
from imbue.mngr_latchkey.migrations.fold_workspace_scope_into_latchkey_self import _PermissionsFile
from imbue.mngr_latchkey.migrations.fold_workspace_scope_into_latchkey_self import (
    fold_workspace_scope_into_latchkey_self,
)
from imbue.mngr_latchkey.migrations.fold_workspace_scope_into_latchkey_self import (
    split_workspace_scope_out_of_latchkey_self,
)
from imbue.mngr_latchkey.store import permissions_path_for_host


def _write_permissions_file(path: Path, config: _PermissionsFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json())


_LEGACY_CONFIG = _PermissionsFile(
    rules=(
        {"minds-api-proxy-per-agent-unauthorized": []},
        {"minds-workspaces": ["minds-workspaces-read", "minds-workspaces-destroy-ws1"]},
        {"latchkey-self": ["latchkey-self-read-self-permissions"]},
    ),
    schemas={
        "minds-workspaces": {
            "properties": {"domain": {"const": "latchkey-self.invalid"}},
            "required": ["domain", "path"],
        },
        "minds-workspaces-read": {"properties": {"method": {"const": "GET"}}, "required": ["method"]},
        "minds-workspaces-destroy-ws1": {"properties": {"method": {"const": "POST"}}, "required": ["method"]},
        "latchkey-self-read-self-permissions": {"properties": {"method": {"const": "GET"}}, "required": ["method"]},
    },
)


def test_fold_unions_workspace_permissions_onto_latchkey_self_and_drops_scope() -> None:
    folded = fold_workspace_scope_into_latchkey_self(_LEGACY_CONFIG)
    rule_keys = [next(iter(rule.keys())) for rule in folded.rules]
    # The ``minds-workspaces`` rule is gone; its permissions land on ``latchkey-self``.
    assert rule_keys == ["minds-api-proxy-per-agent-unauthorized", "latchkey-self"]
    latchkey_self_rule = next(rule for rule in folded.rules if "latchkey-self" in rule)
    assert latchkey_self_rule["latchkey-self"] == [
        "latchkey-self-read-self-permissions",
        "minds-workspaces-read",
        "minds-workspaces-destroy-ws1",
    ]
    # The scope schema is dropped; the per-verb permission schemas stay.
    assert "minds-workspaces" not in folded.schemas
    assert "minds-workspaces-read" in folded.schemas
    assert "minds-workspaces-destroy-ws1" in folded.schemas


def test_fold_is_noop_when_no_workspace_rule_present() -> None:
    already_folded = fold_workspace_scope_into_latchkey_self(_LEGACY_CONFIG)
    assert fold_workspace_scope_into_latchkey_self(already_folded) == already_folded


def test_fold_creates_latchkey_self_rule_when_absent() -> None:
    config = _PermissionsFile(rules=({"minds-workspaces": ["minds-workspaces-read"]},), schemas={})
    folded = fold_workspace_scope_into_latchkey_self(config)
    assert folded.rules == ({"latchkey-self": ["minds-workspaces-read"]},)


def test_split_moves_workspace_permissions_back_and_reconstructs_scope_schema() -> None:
    folded = fold_workspace_scope_into_latchkey_self(_LEGACY_CONFIG)
    unfolded = split_workspace_scope_out_of_latchkey_self(folded)
    rule_keys = [next(iter(rule.keys())) for rule in unfolded.rules]
    # A dedicated ``minds-workspaces`` rule is restored immediately before ``latchkey-self``.
    assert rule_keys == ["minds-api-proxy-per-agent-unauthorized", "minds-workspaces", "latchkey-self"]
    workspace_rule = next(rule for rule in unfolded.rules if "minds-workspaces" in rule)
    assert workspace_rule["minds-workspaces"] == ["minds-workspaces-read", "minds-workspaces-destroy-ws1"]
    latchkey_self_rule = next(rule for rule in unfolded.rules if "latchkey-self" in rule)
    assert latchkey_self_rule["latchkey-self"] == ["latchkey-self-read-self-permissions"]
    # The scope schema is reconstructed with the gateway-self domain + path prefix gate.
    assert unfolded.schemas["minds-workspaces"] == {
        "properties": {
            "domain": {"const": "latchkey-self.invalid"},
            "path": {"type": "string", "pattern": "^/minds-api-proxy/api/v1/workspaces(/|$)"},
        },
        "required": ["domain", "path"],
    }


def test_split_is_noop_when_no_workspace_permissions_on_latchkey_self() -> None:
    config = _PermissionsFile(
        rules=({"latchkey-self": ["latchkey-self-read-self-permissions"]},),
        schemas={},
    )
    assert split_workspace_scope_out_of_latchkey_self(config) == config


def test_fold_then_split_preserves_workspace_permission_set() -> None:
    folded = fold_workspace_scope_into_latchkey_self(_LEGACY_CONFIG)
    unfolded = split_workspace_scope_out_of_latchkey_self(folded)
    original_workspace = next(rule for rule in _LEGACY_CONFIG.rules if "minds-workspaces" in rule)["minds-workspaces"]
    roundtripped_workspace = next(rule for rule in unfolded.rules if "minds-workspaces" in rule)["minds-workspaces"]
    assert set(roundtripped_workspace) == set(original_workspace)


def test_apply_up_rewrites_every_host_file(tmp_path: Path) -> None:
    host_a = HostId.generate()
    host_b = HostId.generate()
    _write_permissions_file(permissions_path_for_host(tmp_path, host_a), _LEGACY_CONFIG)
    _write_permissions_file(permissions_path_for_host(tmp_path, host_b), _LEGACY_CONFIG)

    FoldWorkspaceScopeIntoLatchkeySelf(version=1).apply_up(tmp_path)

    for host_id in (host_a, host_b):
        migrated = json.loads(permissions_path_for_host(tmp_path, host_id).read_text())
        rule_keys = [next(iter(rule.keys())) for rule in migrated["rules"]]
        assert "minds-workspaces" not in rule_keys
        assert "minds-workspaces" not in migrated["schemas"]


def test_apply_up_then_down_restores_two_scope_layout(tmp_path: Path) -> None:
    host_id = HostId.generate()
    _write_permissions_file(permissions_path_for_host(tmp_path, host_id), _LEGACY_CONFIG)
    migration = FoldWorkspaceScopeIntoLatchkeySelf(version=1)

    migration.apply_up(tmp_path)
    migration.apply_down(tmp_path)

    restored = json.loads(permissions_path_for_host(tmp_path, host_id).read_text())
    rule_keys = [next(iter(rule.keys())) for rule in restored["rules"]]
    assert "minds-workspaces" in rule_keys
    assert "minds-workspaces" in restored["schemas"]
