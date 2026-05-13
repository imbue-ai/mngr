import json
import os
import stat
from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.store import LatchkeyGatewayInfo
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import MalformedPermissionsConfigError
from imbue.mngr_latchkey.store import admin_permissions_path
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import delete_gateway_info
from imbue.mngr_latchkey.store import ensure_admin_permissions_file
from imbue.mngr_latchkey.store import gateway_log_path
from imbue.mngr_latchkey.store import granted_permissions_for_scope
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
from imbue.mngr_latchkey.store import load_gateway_info
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_gateway_info
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import set_permissions_for_scope

# Gateway info save/load/delete tests went away when the on-disk
# gateway record did -- gateway lifetime is now fully owned by the
# single ``mngr latchkey forward`` subprocess the supervisor
# guarantees, and cross-process adoption is no longer attempted.
# ``LatchkeyGatewayInfo`` itself remains as an in-memory return-type
# for the spawn path. Forward-supervisor record helpers are tested in
# ``forward_supervisor_test.py`` instead.


def test_gateway_log_path_is_top_level(tmp_path: Path) -> None:
    path = gateway_log_path(tmp_path)
    assert path == tmp_path / "latchkey_gateway.log"


def test_default_permissions_path_is_top_level(tmp_path: Path) -> None:
    path = default_permissions_path(tmp_path)
    assert path == tmp_path / "latchkey_default_permissions.json"


# -- Opaque permissions handle tests --


def test_opaque_permissions_dir_lives_under_data_dir(tmp_path: Path) -> None:
    assert opaque_permissions_dir(tmp_path) == tmp_path / "permissions"


def test_new_opaque_permissions_path_is_unique_uuid_named(tmp_path: Path) -> None:
    a = new_opaque_permissions_path(tmp_path)
    b = new_opaque_permissions_path(tmp_path)
    # Both live under the opaque dir.
    assert a.parent == opaque_permissions_dir(tmp_path)
    assert b.parent == a.parent
    # Suffix is .json, basename is hex-only (UUID4 with dashes stripped).
    assert a.suffix == ".json"
    assert all(c in "0123456789abcdef" for c in a.stem)
    assert len(a.stem) == 32
    # Distinct allocations don't collide.
    assert a != b
    # Paths returned are not yet materialized -- the caller writes the file.
    assert not a.exists()
    assert not b.exists()


def test_new_opaque_permissions_path_creates_parent_dir(tmp_path: Path) -> None:
    """The opaque dir is created lazily so callers don't have to mkdir themselves."""
    assert not opaque_permissions_dir(tmp_path).exists()
    new_opaque_permissions_path(tmp_path)
    assert opaque_permissions_dir(tmp_path).is_dir()


def test_link_opaque_permissions_promotes_baseline_to_host_path(tmp_path: Path) -> None:
    """First creation: opaque baseline file becomes the host's canonical permissions file.

    The baseline (deny-all empty rules) is moved to
    ``permissions_path_for_host(...)`` and ``opaque_path`` is replaced
    by a symlink so the JWT minted for it keeps resolving.
    """
    opaque_path = new_opaque_permissions_path(tmp_path)
    save_permissions(opaque_path, LatchkeyPermissionsConfig())

    host_id = HostId()
    host_path = permissions_path_for_host(tmp_path, host_id)
    assert not host_path.exists()

    link_opaque_permissions_to_host(tmp_path, opaque_path, host_id)

    # The host-keyed file now has the deny-all baseline.
    assert host_path.is_file()
    assert not host_path.is_symlink()
    assert json.loads(host_path.read_text()) == {"rules": []}
    # The opaque path is a symlink to the host path.
    assert opaque_path.is_symlink()
    assert opaque_path.resolve() == host_path.resolve()
    # Reading via the opaque path follows the symlink.
    assert json.loads(opaque_path.read_text()) == {"rules": []}


def test_link_opaque_permissions_preserves_existing_grants_on_recreation(tmp_path: Path) -> None:
    """Re-use case: ``host_path`` already has prior grants; keep them."""
    host_id = HostId()
    host_path = permissions_path_for_host(tmp_path, host_id)
    # Pre-existing grants from a prior agent on the same host.
    save_permissions(
        host_path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )

    opaque_path = new_opaque_permissions_path(tmp_path)
    # Deny-all baseline -- this is what AgentCreator materializes before
    # the canonical host id is known.
    save_permissions(opaque_path, LatchkeyPermissionsConfig())

    link_opaque_permissions_to_host(tmp_path, opaque_path, host_id)

    # Pre-existing grants are preserved (the deny-all baseline is discarded).
    assert host_path.is_file()
    assert not host_path.is_symlink()
    assert json.loads(host_path.read_text()) == {"rules": [{"slack-api": ["slack-read-all"]}]}
    # Opaque path is a symlink and reads back the existing grants.
    assert opaque_path.is_symlink()
    assert json.loads(opaque_path.read_text()) == {"rules": [{"slack-api": ["slack-read-all"]}]}


def test_link_opaque_permissions_survives_save_permissions_atomic_replace(tmp_path: Path) -> None:
    """``save_permissions`` writes via tmp+rename; the symlink target name is unchanged so the link stays valid."""
    opaque_path = new_opaque_permissions_path(tmp_path)
    save_permissions(opaque_path, LatchkeyPermissionsConfig())
    host_id = HostId()
    link_opaque_permissions_to_host(tmp_path, opaque_path, host_id)
    host_path = permissions_path_for_host(tmp_path, host_id)

    # Simulate a permission grant being persisted.
    save_permissions(
        host_path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )

    # The symlink still resolves and the grant is visible through it.
    assert opaque_path.is_symlink()
    assert json.loads(opaque_path.read_text()) == {"rules": [{"slack-api": ["slack-read-all"]}]}


def test_link_opaque_permissions_target_is_absolute(tmp_path: Path) -> None:
    """Symlink target is absolute so it survives directory moves of the symlink itself."""
    opaque_path = new_opaque_permissions_path(tmp_path)
    save_permissions(opaque_path, LatchkeyPermissionsConfig())
    host_id = HostId()
    link_opaque_permissions_to_host(tmp_path, opaque_path, host_id)

    target = os.readlink(opaque_path)
    assert os.path.isabs(target)


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
    path = tmp_path / "hosts" / "host-id" / "latchkey_permissions.json"
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


def test_permissions_path_for_host_uses_hosts_subdir(tmp_path: Path) -> None:
    host_id = HostId()
    path = permissions_path_for_host(tmp_path, host_id)
    assert path == tmp_path / "hosts" / str(host_id) / "latchkey_permissions.json"


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


# -- Admin permissions ---------------------------------------------------------


def test_ensure_admin_permissions_file_materializes_wildcard(tmp_path: Path) -> None:
    """The admin permissions file is created with a wildcard ``{"any": ["any"]}`` rule."""
    path = ensure_admin_permissions_file(tmp_path)
    assert path == admin_permissions_path(tmp_path)
    assert path.is_file()
    on_disk = json.loads(path.read_text())
    assert on_disk == {"rules": [{"any": ["any"]}]}


def test_ensure_admin_permissions_file_is_idempotent(tmp_path: Path) -> None:
    """A pre-existing admin permissions file is left untouched."""
    path = admin_permissions_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    custom = '{"rules": [{"slack-api": ["any"]}]}'
    path.write_text(custom)
    ensure_admin_permissions_file(tmp_path)
    assert path.read_text() == custom


# -- Gateway info --------------------------------------------------------------


def test_save_then_load_gateway_info_round_trip(tmp_path: Path) -> None:
    info = LatchkeyGatewayInfo(url="http://127.0.0.1:12345", password="hunter2")
    save_gateway_info(tmp_path, info)
    loaded = load_gateway_info(tmp_path)
    assert loaded == info


def test_load_gateway_info_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_gateway_info(tmp_path) is None


def test_delete_gateway_info_is_idempotent(tmp_path: Path) -> None:
    # First call must be a no-op (no record on disk yet).
    delete_gateway_info(tmp_path)
    save_gateway_info(tmp_path, LatchkeyGatewayInfo(url="http://127.0.0.1:1", password="p"))
    delete_gateway_info(tmp_path)
    assert load_gateway_info(tmp_path) is None


def test_save_gateway_info_writes_mode_0600(tmp_path: Path) -> None:
    """The password is sensitive; the file must not be world-readable."""
    save_gateway_info(tmp_path, LatchkeyGatewayInfo(url="http://x", password="p"))
    file_mode = stat.S_IMODE(os.stat(tmp_path / "latchkey_gateway.json").st_mode)
    assert file_mode == 0o600
