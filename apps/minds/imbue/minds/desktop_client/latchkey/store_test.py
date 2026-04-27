import json
import os
import stat
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.store import LatchkeyGatewayInfo
from imbue.minds.desktop_client.latchkey.store import LatchkeyStoreError
from imbue.minds.desktop_client.latchkey.store import MalformedPermissionsConfigError
from imbue.minds.desktop_client.latchkey.store import PermissionsConfig
from imbue.minds.desktop_client.latchkey.store import delete_gateway_info
from imbue.minds.desktop_client.latchkey.store import delete_permissions_for_agent
from imbue.minds.desktop_client.latchkey.store import gateway_log_path
from imbue.minds.desktop_client.latchkey.store import granted_permissions_for_service
from imbue.minds.desktop_client.latchkey.store import list_gateway_infos
from imbue.minds.desktop_client.latchkey.store import load_gateway_info
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_gateway_info
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.latchkey.store import set_permissions_for_service
from imbue.mngr.primitives import AgentId


def _make_record(agent_id: AgentId | None = None) -> LatchkeyGatewayInfo:
    return LatchkeyGatewayInfo(
        agent_id=agent_id or AgentId(),
        host="127.0.0.1",
        port=19999,
        pid=12345,
        started_at=datetime.now(timezone.utc),
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    record = _make_record()
    save_gateway_info(tmp_path, record)
    loaded = load_gateway_info(tmp_path, record.agent_id)
    assert loaded == record


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_gateway_info(tmp_path, AgentId()) is None


def test_load_returns_none_when_malformed(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = tmp_path / "agents" / str(agent_id) / "latchkey_gateway.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert load_gateway_info(tmp_path, agent_id) is None


def test_delete_is_idempotent(tmp_path: Path) -> None:
    agent_id = AgentId()
    delete_gateway_info(tmp_path, agent_id)
    record = _make_record(agent_id)
    save_gateway_info(tmp_path, record)
    delete_gateway_info(tmp_path, agent_id)
    delete_gateway_info(tmp_path, agent_id)
    assert load_gateway_info(tmp_path, agent_id) is None


def test_list_gateway_infos_returns_all_valid_records(tmp_path: Path) -> None:
    record_a = _make_record()
    record_b = _make_record()
    save_gateway_info(tmp_path, record_a)
    save_gateway_info(tmp_path, record_b)

    # Also drop a malformed record to make sure list skips it.
    rogue_agent = AgentId()
    rogue_path = tmp_path / "agents" / str(rogue_agent) / "latchkey_gateway.json"
    rogue_path.parent.mkdir(parents=True, exist_ok=True)
    rogue_path.write_text("definitely not json")

    records = list_gateway_infos(tmp_path)
    assert {r.agent_id for r in records} == {record_a.agent_id, record_b.agent_id}


def test_list_gateway_infos_returns_empty_when_agents_dir_missing(tmp_path: Path) -> None:
    assert list_gateway_infos(tmp_path) == []


def test_gateway_log_path_uses_per_agent_subdirectory(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = gateway_log_path(tmp_path, agent_id)
    assert path == tmp_path / "agents" / str(agent_id) / "latchkey_gateway.log"


# -- Permissions config tests (merged from permissions_store_test.py) --



def test_load_permissions_returns_empty_for_missing_file(tmp_path: Path) -> None:
    config = load_permissions(tmp_path / "missing.json")
    assert config == PermissionsConfig()
    assert config.rules == ()


def test_load_permissions_parses_rules_schemas_and_include(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {"slack-api": ["slack-read-all"]},
                    {"github-rest-api": ["github-read-all", "github-write-issues"]},
                ],
                "schemas": {"my-schema": {"properties": {"method": {"const": "GET"}}}},
                "include": ["shared/example.json"],
            }
        )
    )

    config = load_permissions(path)

    assert config.rules == (
        {"slack-api": ["slack-read-all"]},
        {"github-rest-api": ["github-read-all", "github-write-issues"]},
    )
    assert config.schemas == {"my-schema": {"properties": {"method": {"const": "GET"}}}}
    assert config.include == ("shared/example.json",)


def test_load_permissions_round_trip_preserves_unknown_schemas(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    original_schemas = {"custom": {"properties": {"path": {"const": "/v1/widgets"}}}}
    config = PermissionsConfig(
        rules=({"custom": ["custom-read"]},),
        schemas=original_schemas,
    )

    save_permissions(path, config)
    reloaded = load_permissions(path)

    assert reloaded == config


def test_load_permissions_rejects_non_object_top_level(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    path.write_text("[]")

    with pytest.raises(MalformedPermissionsConfigError):
        load_permissions(path)


def test_load_permissions_rejects_non_string_permission_values(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    path.write_text(json.dumps({"rules": [{"slack-api": ["slack-read-all", 123]}]}))

    with pytest.raises(MalformedPermissionsConfigError):
        load_permissions(path)


def test_save_permissions_uses_mode_0o600(tmp_path: Path) -> None:
    path = tmp_path / "agents" / "agent-id" / "permissions.json"
    save_permissions(path, PermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    assert path.is_file()


def test_save_permissions_writes_atomically(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    save_permissions(path, PermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    # No leftover .tmp file from the swap.
    leftovers = list(tmp_path.glob("permissions.json.*"))
    assert leftovers == []


def test_set_permissions_for_service_replaces_existing_rule_for_scope() -> None:
    config = PermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all"]},
            {"github-rest-api": ["github-read-all"]},
        )
    )

    updated = set_permissions_for_service(
        config,
        scope_schemas=("slack-api",),
        granted_permissions=("slack-read-all", "slack-write-messages"),
    )

    assert updated.rules == (
        {"slack-api": ["slack-read-all", "slack-write-messages"]},
        {"github-rest-api": ["github-read-all"]},
    )


def test_set_permissions_for_service_appends_new_rule_when_scope_absent() -> None:
    config = PermissionsConfig(rules=({"github-rest-api": ["github-read-all"]},))

    updated = set_permissions_for_service(
        config,
        scope_schemas=("slack-api",),
        granted_permissions=("slack-read-all",),
    )

    assert updated.rules == (
        {"github-rest-api": ["github-read-all"]},
        {"slack-api": ["slack-read-all"]},
    )


def test_set_permissions_for_service_writes_one_rule_per_scope() -> None:
    config = PermissionsConfig()

    updated = set_permissions_for_service(
        config,
        scope_schemas=("aws-s3", "aws-ec2"),
        granted_permissions=("aws-s3-read",),
    )

    assert updated.rules == (
        {"aws-s3": ["aws-s3-read"]},
        {"aws-ec2": ["aws-s3-read"]},
    )


def test_set_permissions_for_service_rejects_empty_grant() -> None:
    config = PermissionsConfig()

    with pytest.raises(LatchkeyStoreError):
        set_permissions_for_service(
            config,
            scope_schemas=("slack-api",),
            granted_permissions=(),
        )


def test_set_permissions_for_service_rejects_empty_scope_list() -> None:
    config = PermissionsConfig()

    with pytest.raises(LatchkeyStoreError):
        set_permissions_for_service(
            config,
            scope_schemas=(),
            granted_permissions=("slack-read-all",),
        )


def test_granted_permissions_for_service_returns_empty_for_missing_scope() -> None:
    config = PermissionsConfig(rules=({"slack-api": ["slack-read-all"]},))

    granted = granted_permissions_for_service(config, scope_schemas=("github-rest-api",))

    assert granted == {"github-rest-api": ()}


def test_granted_permissions_for_service_returns_existing_grants() -> None:
    config = PermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all", "slack-write-messages"]},
            {"github-rest-api": ["github-read-all"]},
        )
    )

    granted = granted_permissions_for_service(
        config,
        scope_schemas=("slack-api", "github-rest-api"),
    )

    assert granted == {
        "slack-api": ("slack-read-all", "slack-write-messages"),
        "github-rest-api": ("github-read-all",),
    }


def test_permissions_path_for_agent_uses_agents_subdir(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = permissions_path_for_agent(tmp_path, agent_id)
    assert path == tmp_path / "agents" / str(agent_id) / "permissions.json"


def test_delete_permissions_for_agent_is_noop_when_absent(tmp_path: Path) -> None:
    delete_permissions_for_agent(tmp_path, AgentId())


def test_delete_permissions_for_agent_removes_existing_file(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = permissions_path_for_agent(tmp_path, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")

    delete_permissions_for_agent(tmp_path, agent_id)

    assert not path.exists()


def test_save_then_load_round_trip_preserves_rule_order(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    config = PermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all"]},
            {"github-rest-api": ["github-read-all"]},
            {"discord-api": ["discord-read-messages"]},
        )
    )

    save_permissions(path, config)
    reloaded = load_permissions(path)

    assert reloaded.rules == config.rules


def test_save_permissions_serializes_to_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    save_permissions(path, PermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    # Verify the file is valid JSON of the expected shape (no `tuple` markers
    # leaking out, integers vs strings correct, etc.).
    raw = json.loads(path.read_text())
    assert raw == {"rules": [{"slack-api": ["slack-read-all"]}]}


def test_save_permissions_creates_parent_directories(tmp_path: Path) -> None:
    deep_path = tmp_path / "a" / "b" / "c" / "permissions.json"
    save_permissions(deep_path, PermissionsConfig())

    assert deep_path.is_file()


def test_set_permissions_for_service_preserves_unrelated_rules() -> None:
    config = PermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all"]},
            {"github-rest-api": ["github-read-all"]},
            {"discord-api": ["discord-read-messages"]},
        )
    )

    updated = set_permissions_for_service(
        config,
        scope_schemas=("github-rest-api",),
        granted_permissions=("github-read-all", "github-write-issues"),
    )

    assert updated.rules == (
        {"slack-api": ["slack-read-all"]},
        {"github-rest-api": ["github-read-all", "github-write-issues"]},
        {"discord-api": ["discord-read-messages"]},
    )


def test_save_permissions_excludes_optional_keys_when_unset(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    save_permissions(path, PermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    raw = json.loads(path.read_text())
    assert "schemas" not in raw
    assert "include" not in raw


def test_load_permissions_handles_world_readable_file_without_crashing(tmp_path: Path) -> None:
    # Latchkey enforces secure permissions on its own files, but minds writes
    # this one. Ensure that loading does not care about file mode.
    path = tmp_path / "permissions.json"
    path.write_text(json.dumps({"rules": []}))
    path.chmod(0o644)

    config = load_permissions(path)

    assert config.rules == ()
    # Sanity-check the test setup itself.
    assert path.stat().st_mode & stat.S_IROTH


def test_save_permissions_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    save_permissions(path, PermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    save_permissions(
        path,
        PermissionsConfig(rules=({"slack-api": ["slack-read-all", "slack-write-messages"]},)),
    )

    raw = json.loads(path.read_text())
    assert raw == {"rules": [{"slack-api": ["slack-read-all", "slack-write-messages"]}]}
    # Ensure no temp file was left behind.
    assert not (tmp_path / "permissions.json.tmp").exists()


def test_set_permissions_for_service_handles_multi_key_rule_unrelated_to_managed_scope() -> None:
    # A multi-key rule (length > 1) cannot be matched as a single-scope rule;
    # we must preserve it verbatim.
    config = PermissionsConfig(rules=({"foo": ["foo-read"], "bar": ["bar-read"]},))

    updated = set_permissions_for_service(
        config,
        scope_schemas=("slack-api",),
        granted_permissions=("slack-read-all",),
    )

    assert updated.rules == (
        {"foo": ["foo-read"], "bar": ["bar-read"]},
        {"slack-api": ["slack-read-all"]},
    )


def test_load_permissions_propagates_os_errors(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    path.write_text("{}")
    path.chmod(0)

    try:
        # Skip on platforms (e.g. running as root) where the unreadable
        # permission cannot be enforced.
        if os.access(path, os.R_OK):
            pytest.skip("Cannot make file unreadable in this environment")
        with pytest.raises(LatchkeyStoreError):
            load_permissions(path)
    finally:
        path.chmod(0o600)
