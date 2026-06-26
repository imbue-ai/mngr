"""Unit tests for the ``minds-workspaces`` per-target grant machinery."""

import json
import re
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.workspace_permissions import MINDS_WORKSPACES_PATH_PREFIX
from imbue.mngr_latchkey.workspace_permissions import MINDS_WORKSPACES_SCOPE
from imbue.mngr_latchkey.workspace_permissions import PERM_WORKSPACES_DESTROY
from imbue.mngr_latchkey.workspace_permissions import PERM_WORKSPACES_LIFECYCLE
from imbue.mngr_latchkey.workspace_permissions import PERM_WORKSPACES_READ
from imbue.mngr_latchkey.workspace_permissions import PERM_WORKSPACES_SSH
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS
from imbue.mngr_latchkey.workspace_permissions import grant_workspace_permissions
from imbue.mngr_latchkey.workspace_permissions import is_targeted_verb


def _bare_host_file(plugin_data_dir: Path, host_id: HostId) -> Path:
    path = plugin_data_dir / "hosts" / str(host_id) / "latchkey_permissions.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"rules": [], "schemas": {}}))
    return path


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def _rule_permissions(config: dict[str, object]) -> list[str]:
    for rule in config["rules"]:
        if list(rule.keys()) == [MINDS_WORKSPACES_SCOPE]:
            return rule[MINDS_WORKSPACES_SCOPE]
    return []


def _anyof_patterns(config: dict[str, object], verb: str) -> list[str]:
    return [entry["pattern"] for entry in config["schemas"][verb]["properties"]["path"]["anyOf"]]


def test_is_targeted_verb_classifies_verbs() -> None:
    assert is_targeted_verb(PERM_WORKSPACES_DESTROY)
    assert is_targeted_verb(PERM_WORKSPACES_SSH)
    assert not is_targeted_verb(PERM_WORKSPACES_READ)
    assert not is_targeted_verb("not-a-verb")


def test_grant_targeted_verb_pins_single_target(tmp_path: Path) -> None:
    host_id = HostId.generate()
    target = AgentId.generate()
    path = _bare_host_file(tmp_path, host_id)

    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_DESTROY], target)

    config = _read(path)
    assert _rule_permissions(config) == [PERM_WORKSPACES_DESTROY]
    patterns = _anyof_patterns(config, PERM_WORKSPACES_DESTROY)
    assert len(patterns) == 1
    compiled = re.compile(patterns[0])
    assert compiled.fullmatch(f"{MINDS_WORKSPACES_PATH_PREFIX}/{target}/destroy")
    # A different workspace id is not covered by the single-target grant.
    other = AgentId.generate()
    assert not compiled.fullmatch(f"{MINDS_WORKSPACES_PATH_PREFIX}/{other}/destroy")


def test_grant_accumulates_targets_across_calls(tmp_path: Path) -> None:
    host_id = HostId.generate()
    target_a = AgentId.generate()
    target_b = AgentId.generate()
    path = _bare_host_file(tmp_path, host_id)

    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_DESTROY], target_a)
    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_DESTROY], target_b)

    config = _read(path)
    patterns = _anyof_patterns(config, PERM_WORKSPACES_DESTROY)
    assert len(patterns) == 2
    joined = "\n".join(patterns)
    assert str(target_a) in joined
    assert str(target_b) in joined
    # The rule still carries the verb exactly once.
    assert _rule_permissions(config) == [PERM_WORKSPACES_DESTROY]


def test_grant_is_idempotent_for_repeated_target(tmp_path: Path) -> None:
    host_id = HostId.generate()
    target = AgentId.generate()
    path = _bare_host_file(tmp_path, host_id)

    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_LIFECYCLE], target)
    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_LIFECYCLE], target)

    config = _read(path)
    assert len(_anyof_patterns(config, PERM_WORKSPACES_LIFECYCLE)) == 1


def test_grant_all_workspaces_uses_wildcard_segment(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = _bare_host_file(tmp_path, host_id)

    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_SSH], None)

    config = _read(path)
    patterns = _anyof_patterns(config, PERM_WORKSPACES_SSH)
    assert len(patterns) == 1
    compiled = re.compile(patterns[0])
    # Any workspace id matches the all-workspaces grant.
    assert compiled.fullmatch(f"{MINDS_WORKSPACES_PATH_PREFIX}/{AgentId.generate()}/ssh")
    assert compiled.fullmatch(f"{MINDS_WORKSPACES_PATH_PREFIX}/{AgentId.generate()}/ssh")
    # But a different verb's path does not.
    assert not compiled.fullmatch(f"{MINDS_WORKSPACES_PATH_PREFIX}/{AgentId.generate()}/destroy")


def test_grant_non_targeted_verb_adds_rule_only(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = _bare_host_file(tmp_path, host_id)

    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_READ], None)

    config = _read(path)
    assert _rule_permissions(config) == [PERM_WORKSPACES_READ]
    # Read uses a broad path pattern (no per-target anyOf).
    read_schema = config["schemas"][PERM_WORKSPACES_READ]["properties"]["path"]
    assert "pattern" in read_schema
    assert "anyOf" not in read_schema


def test_grant_mixed_verbs_targets_only_targeted(tmp_path: Path) -> None:
    host_id = HostId.generate()
    target = AgentId.generate()
    path = _bare_host_file(tmp_path, host_id)

    grant_workspace_permissions(
        tmp_path,
        host_id,
        [PERM_WORKSPACES_READ, PERM_WORKSPACES_DESTROY],
        target,
    )

    config = _read(path)
    assert set(_rule_permissions(config)) == {PERM_WORKSPACES_READ, PERM_WORKSPACES_DESTROY}
    # Targeted destroy pins the single target; broad read has no anyOf.
    assert _anyof_patterns(config, PERM_WORKSPACES_DESTROY) == [f"^{MINDS_WORKSPACES_PATH_PREFIX}/{target}/destroy$"]
    assert "anyOf" not in config["schemas"][PERM_WORKSPACES_READ]["properties"]["path"]


def test_grant_rejects_unknown_verb(tmp_path: Path) -> None:
    host_id = HostId.generate()
    _bare_host_file(tmp_path, host_id)
    with pytest.raises(LatchkeyStoreError):
        grant_workspace_permissions(tmp_path, host_id, ["bogus-verb"], None)


def test_grant_rejects_empty_verbs(tmp_path: Path) -> None:
    host_id = HostId.generate()
    _bare_host_file(tmp_path, host_id)
    with pytest.raises(LatchkeyStoreError):
        grant_workspace_permissions(tmp_path, host_id, [], None)


def test_grant_raises_when_host_file_missing(tmp_path: Path) -> None:
    host_id = HostId.generate()
    target = AgentId.generate()
    with pytest.raises(LatchkeyStoreError):
        grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_DESTROY], target)


def test_grant_preserves_unrelated_rules_and_schemas(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = tmp_path / "hosts" / str(host_id) / "latchkey_permissions.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "rules": [{"latchkey-self": ["latchkey-self-read-self-permissions"]}],
                "schemas": {"latchkey-self": {"properties": {"domain": {"const": "latchkey-self.invalid"}}}},
            }
        )
    )

    target = AgentId.generate()
    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_DESTROY], target)

    config = _read(path)
    assert {"latchkey-self": ["latchkey-self-read-self-permissions"]} in config["rules"]
    assert "latchkey-self" in config["schemas"]
    # The scope gate is materialized so detent can resolve the new rule.
    assert MINDS_WORKSPACES_SCOPE in config["schemas"]


def test_grant_rebuilds_stale_broad_targeted_schema_into_anyof(tmp_path: Path) -> None:
    """A pre-existing broad-pattern targeted schema is replaced by an anyOf on grant."""
    host_id = HostId.generate()
    path = tmp_path / "hosts" / str(host_id) / "latchkey_permissions.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "rules": [],
                "schemas": {
                    PERM_WORKSPACES_DESTROY: {
                        "properties": {
                            "method": {"const": "POST"},
                            "path": {"type": "string", "pattern": "^.*/destroy$"},
                        },
                        "required": ["method", "path"],
                    }
                },
            }
        )
    )

    target = AgentId.generate()
    grant_workspace_permissions(tmp_path, host_id, [PERM_WORKSPACES_DESTROY], target)

    config = _read(path)
    path_schema = config["schemas"][PERM_WORKSPACES_DESTROY]["properties"]["path"]
    assert "pattern" not in path_schema
    assert _anyof_patterns(config, PERM_WORKSPACES_DESTROY) == [f"^{MINDS_WORKSPACES_PATH_PREFIX}/{target}/destroy$"]


def test_all_verbs_have_dialog_metadata() -> None:
    for verb in WORKSPACE_VERBS:
        assert verb.permission.startswith("minds-workspaces")
        assert verb.display_name
        assert verb.description
