import json
import os
import stat
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client.latchkey.store import LatchkeyGatewayInfo
from imbue.minds.desktop_client.latchkey.store import LatchkeyPermissionsConfig
from imbue.minds.desktop_client.latchkey.store import LatchkeyStoreError
from imbue.minds.desktop_client.latchkey.store import MalformedPermissionsConfigError
from imbue.minds.desktop_client.latchkey.store import default_permissions_path
from imbue.minds.desktop_client.latchkey.store import delete_gateway_info
from imbue.minds.desktop_client.latchkey.store import delete_legacy_per_agent_gateway_records
from imbue.minds.desktop_client.latchkey.store import gateway_info_path
from imbue.minds.desktop_client.latchkey.store import gateway_log_path
from imbue.minds.desktop_client.latchkey.store import granted_permissions_for_scope
from imbue.minds.desktop_client.latchkey.store import load_gateway_info
from imbue.minds.desktop_client.latchkey.store import load_permissions
from imbue.minds.desktop_client.latchkey.store import permissions_path_for_agent
from imbue.minds.desktop_client.latchkey.store import save_gateway_info
from imbue.minds.desktop_client.latchkey.store import save_permissions
from imbue.minds.desktop_client.latchkey.store import set_permissions_for_scope
from imbue.mngr.primitives import AgentId


def _make_record() -> LatchkeyGatewayInfo:
    return LatchkeyGatewayInfo(
        host="127.0.0.1",
        port=19999,
        pid=12345,
        started_at=datetime.now(timezone.utc),
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    record = _make_record()
    save_gateway_info(tmp_path, record)
    loaded = load_gateway_info(tmp_path)
    assert loaded == record


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_gateway_info(tmp_path) is None


def test_load_returns_none_when_malformed(tmp_path: Path) -> None:
    path = gateway_info_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert load_gateway_info(tmp_path) is None


def test_delete_is_idempotent(tmp_path: Path) -> None:
    delete_gateway_info(tmp_path)
    record = _make_record()
    save_gateway_info(tmp_path, record)
    delete_gateway_info(tmp_path)
    delete_gateway_info(tmp_path)
    assert load_gateway_info(tmp_path) is None


def test_save_overwrites_existing_record(tmp_path: Path) -> None:
    save_gateway_info(
        tmp_path, LatchkeyGatewayInfo(host="127.0.0.1", port=1, pid=1, started_at=datetime.now(timezone.utc))
    )
    fresh = LatchkeyGatewayInfo(host="127.0.0.1", port=2, pid=2, started_at=datetime.now(timezone.utc))
    save_gateway_info(tmp_path, fresh)
    loaded = load_gateway_info(tmp_path)
    assert loaded == fresh


def test_gateway_log_path_is_top_level(tmp_path: Path) -> None:
    path = gateway_log_path(tmp_path)
    assert path == tmp_path / "latchkey_gateway.log"


def test_default_permissions_path_is_top_level(tmp_path: Path) -> None:
    path = default_permissions_path(tmp_path)
    assert path == tmp_path / "latchkey_default_permissions.json"


def test_delete_legacy_per_agent_gateway_records_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert delete_legacy_per_agent_gateway_records(tmp_path) == []


def test_delete_legacy_per_agent_gateway_records_removes_per_agent_files(tmp_path: Path) -> None:
    """Older minds versions wrote latchkey_gateway.json under each agent dir.

    The shared-gateway architecture has no use for those files, so they
    must be deleted on startup.
    """
    agent_a = AgentId()
    agent_b = AgentId()
    for agent_id in (agent_a, agent_b):
        legacy_path = tmp_path / "agents" / str(agent_id) / "latchkey_gateway.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("{}")

    removed = delete_legacy_per_agent_gateway_records(tmp_path)

    assert set(removed) == {agent_a, agent_b}
    for agent_id in (agent_a, agent_b):
        assert not (tmp_path / "agents" / str(agent_id) / "latchkey_gateway.json").exists()


def test_delete_legacy_per_agent_gateway_records_skips_non_agent_dirs(tmp_path: Path) -> None:
    """Stray non-UUID subdirectories under agents/ must not crash the cleanup."""
    rogue = tmp_path / "agents" / "not-a-uuid"
    rogue.mkdir(parents=True)
    (rogue / "latchkey_gateway.json").write_text("{}")

    removed = delete_legacy_per_agent_gateway_records(tmp_path)
    assert removed == []
    # The file is left alone so the user can investigate.
    assert (rogue / "latchkey_gateway.json").exists()


def test_delete_legacy_per_agent_gateway_records_preserves_other_files(tmp_path: Path) -> None:
    """Per-agent permissions files must not be touched by the legacy cleanup."""
    agent_id = AgentId()
    permissions_path = permissions_path_for_agent(tmp_path, agent_id)
    permissions_path.parent.mkdir(parents=True, exist_ok=True)
    permissions_path.write_text('{"rules": []}')
    legacy_gateway_path = tmp_path / "agents" / str(agent_id) / "latchkey_gateway.json"
    legacy_gateway_path.write_text("{}")

    delete_legacy_per_agent_gateway_records(tmp_path)

    assert not legacy_gateway_path.exists()
    assert permissions_path.exists()


# -- Permissions config tests --


def test_load_permissions_returns_empty_for_missing_file(tmp_path: Path) -> None:
    config = load_permissions(tmp_path / "missing.json")
    assert config == LatchkeyPermissionsConfig()
    assert config.rules == ()


def test_load_permissions_silently_drops_unmodeled_keys(tmp_path: Path) -> None:
    """Detent's ``schemas`` and ``include`` directives are not modeled.

    Minds owns the file and writes it programmatically; hand-edited
    entries for either key are dropped on the next minds-driven save.
    """
    path = tmp_path / "latchkey_permissions.json"
    path.write_text(
        json.dumps(
            {
                "rules": [{"slack-api": ["slack-read-all"]}],
                "schemas": {"my-schema": {"properties": {"method": {"const": "GET"}}}},
                "include": ["shared/example.json"],
            }
        )
    )

    config = load_permissions(path)

    # The rules came through; nothing else does.
    assert config.rules == ({"slack-api": ["slack-read-all"]},)
    assert not hasattr(config, "schemas")
    assert not hasattr(config, "include")

    # Saving back to disk emits ``rules`` only.
    save_permissions(path, config)
    assert sorted(json.loads(path.read_text()).keys()) == ["rules"]


def test_load_permissions_rejects_non_object_top_level(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    path.write_text("[]")

    with pytest.raises(MalformedPermissionsConfigError):
        load_permissions(path)


def test_load_permissions_rejects_non_string_permission_values(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    path.write_text(json.dumps({"rules": [{"slack-api": ["slack-read-all", 123]}]}))

    with pytest.raises(MalformedPermissionsConfigError):
        load_permissions(path)


def test_save_permissions_uses_mode_0o600(tmp_path: Path) -> None:
    path = tmp_path / "agents" / "agent-id" / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    assert path.is_file()


def test_save_permissions_writes_atomically(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    # No leftover .tmp file from the swap.
    leftovers = list(tmp_path.glob("latchkey_permissions.json.*"))
    assert leftovers == []


def test_set_permissions_for_scope_replaces_existing_rule() -> None:
    config = LatchkeyPermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all"]},
            {"github-rest-api": ["github-read-all"]},
        )
    )

    updated = set_permissions_for_scope(
        config,
        scope="slack-api",
        granted_permissions=("slack-read-all", "slack-write-messages"),
    )

    assert updated.rules == (
        {"slack-api": ["slack-read-all", "slack-write-messages"]},
        {"github-rest-api": ["github-read-all"]},
    )


def test_set_permissions_for_scope_appends_new_rule_when_absent() -> None:
    config = LatchkeyPermissionsConfig(rules=({"github-rest-api": ["github-read-all"]},))

    updated = set_permissions_for_scope(
        config,
        scope="slack-api",
        granted_permissions=("slack-read-all",),
    )

    assert updated.rules == (
        {"github-rest-api": ["github-read-all"]},
        {"slack-api": ["slack-read-all"]},
    )


def test_set_permissions_for_scope_called_per_scope_when_iterating() -> None:
    """Multi-scope updates compose by chaining single-scope calls."""
    config = LatchkeyPermissionsConfig()

    for scope in ("aws-s3", "aws-ec2"):
        config = set_permissions_for_scope(
            config,
            scope=scope,
            granted_permissions=("aws-s3-read",),
        )

    assert config.rules == (
        {"aws-s3": ["aws-s3-read"]},
        {"aws-ec2": ["aws-s3-read"]},
    )


def test_set_permissions_for_scope_rejects_empty_grant() -> None:
    config = LatchkeyPermissionsConfig()

    with pytest.raises(LatchkeyStoreError):
        set_permissions_for_scope(
            config,
            scope="slack-api",
            granted_permissions=(),
        )


def test_set_permissions_for_scope_collapses_pre_existing_duplicates() -> None:
    """A hand-edited file with two rules naming the same scope collapses to one on rewrite."""
    config = LatchkeyPermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all"]},
            {"github-rest-api": ["github-read-all"]},
            {"slack-api": ["slack-write-messages"]},
        )
    )

    updated = set_permissions_for_scope(
        config,
        scope="slack-api",
        granted_permissions=("slack-search",),
    )

    assert updated.rules == (
        {"slack-api": ["slack-search"]},
        {"github-rest-api": ["github-read-all"]},
    )


def test_granted_permissions_for_scope_returns_empty_for_missing_scope() -> None:
    config = LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},))

    assert granted_permissions_for_scope(config, scope="github-rest-api") == ()


def test_granted_permissions_for_scope_returns_existing_grants() -> None:
    config = LatchkeyPermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all", "slack-write-messages"]},
            {"github-rest-api": ["github-read-all"]},
        )
    )

    assert granted_permissions_for_scope(config, scope="slack-api") == (
        "slack-read-all",
        "slack-write-messages",
    )
    assert granted_permissions_for_scope(config, scope="github-rest-api") == ("github-read-all",)


def test_permissions_path_for_agent_uses_agents_subdir(tmp_path: Path) -> None:
    agent_id = AgentId()
    path = permissions_path_for_agent(tmp_path, agent_id)
    assert path == tmp_path / "agents" / str(agent_id) / "latchkey_permissions.json"


def test_save_then_load_round_trip_preserves_rule_order(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    config = LatchkeyPermissionsConfig(
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
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    # Verify the file is valid JSON of the expected shape (no `tuple` markers
    # leaking out, integers vs strings correct, etc.).
    raw = json.loads(path.read_text())
    assert raw == {"rules": [{"slack-api": ["slack-read-all"]}]}


def test_save_permissions_creates_parent_directories(tmp_path: Path) -> None:
    deep_path = tmp_path / "a" / "b" / "c" / "latchkey_permissions.json"
    save_permissions(deep_path, LatchkeyPermissionsConfig())

    assert deep_path.is_file()


def test_set_permissions_for_scope_preserves_unrelated_rules() -> None:
    config = LatchkeyPermissionsConfig(
        rules=(
            {"slack-api": ["slack-read-all"]},
            {"github-rest-api": ["github-read-all"]},
            {"discord-api": ["discord-read-messages"]},
        )
    )

    updated = set_permissions_for_scope(
        config,
        scope="github-rest-api",
        granted_permissions=("github-read-all", "github-write-issues"),
    )

    assert updated.rules == (
        {"slack-api": ["slack-read-all"]},
        {"github-rest-api": ["github-read-all", "github-write-issues"]},
        {"discord-api": ["discord-read-messages"]},
    )


def test_save_permissions_emits_only_rules_key(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    raw = json.loads(path.read_text())
    assert sorted(raw.keys()) == ["rules"]


def test_load_permissions_handles_world_readable_file_without_crashing(tmp_path: Path) -> None:
    # Latchkey enforces secure permissions on its own files, but minds writes
    # this one. Ensure that loading does not care about file mode.
    path = tmp_path / "latchkey_permissions.json"
    path.write_text(json.dumps({"rules": []}))
    path.chmod(0o644)

    config = load_permissions(path)

    assert config.rules == ()
    # Sanity-check the test setup itself.
    assert path.stat().st_mode & stat.S_IROTH


def test_save_permissions_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    save_permissions(
        path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all", "slack-write-messages"]},)),
    )

    raw = json.loads(path.read_text())
    assert raw == {"rules": [{"slack-api": ["slack-read-all", "slack-write-messages"]}]}
    # Ensure no temp file was left behind.
    assert not (tmp_path / "latchkey_permissions.json.tmp").exists()


def test_set_permissions_for_scope_preserves_unrelated_multi_key_rule() -> None:
    """A multi-key rule that does not name the managed scope is kept verbatim."""
    config = LatchkeyPermissionsConfig(rules=({"foo": ["foo-read"], "bar": ["bar-read"]},))

    updated = set_permissions_for_scope(
        config,
        scope="slack-api",
        granted_permissions=("slack-read-all",),
    )

    assert updated.rules == (
        {"foo": ["foo-read"], "bar": ["bar-read"]},
        {"slack-api": ["slack-read-all"]},
    )


def test_load_permissions_propagates_os_errors(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
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
