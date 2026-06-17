import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import admin_permissions_path
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import ensure_admin_permissions_file
from imbue.mngr_latchkey.store import forward_events_log_path
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
from imbue.mngr_latchkey.store import load_forward_info
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import point_opaque_handle_at_host
from imbue.mngr_latchkey.store import save_forward_info
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import update_forward_info_gateway_port

# Gateway-record save/load/delete tests went away when the on-disk
# gateway record did. The supervisor's bound gateway port is now
# stamped onto the existing ``LatchkeyForwardInfo`` record via
# ``update_forward_info_gateway_port``; the password is never
# persisted (callers derive it via ``Latchkey.derive_gateway_password``).
# Forward-supervisor record helpers are tested in
# ``forward_supervisor_test.py`` and below.


def test_forward_log_paths_are_distinct(tmp_path: Path) -> None:
    raw = forward_log_path(tmp_path)
    structured = forward_events_log_path(tmp_path)
    assert raw == tmp_path / "latchkey_forward.log"
    # Named ``events.jsonl`` (directly in the plugin dir, no nested subdir) so
    # the standard mngr JSONL sink prunes its rotated copies.
    assert structured == tmp_path / "events.jsonl"
    assert raw != structured


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


def test_point_opaque_handle_creates_symlink_when_absent(tmp_path: Path) -> None:
    """``point_opaque_handle_at_host`` creates the handle symlink without moving anything."""
    host_id = HostId()
    host_path = permissions_path_for_host(tmp_path, host_id)
    save_permissions(host_path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    # A handle path under the opaque dir that was never materialized.
    opaque_path = opaque_permissions_dir(tmp_path) / "deadbeefdeadbeefdeadbeefdeadbeef.json"
    assert not opaque_path.exists()

    point_opaque_handle_at_host(tmp_path, opaque_path, host_id)

    assert opaque_path.is_symlink()
    assert opaque_path.resolve() == host_path.resolve()
    assert os.path.isabs(os.readlink(opaque_path))
    # The canonical file is untouched (nothing was moved into it).
    assert json.loads(opaque_path.read_text()) == {"rules": [{"slack-api": ["slack-read-all"]}]}


def test_point_opaque_handle_repoints_existing_symlink(tmp_path: Path) -> None:
    """An existing handle pointing elsewhere is atomically repointed at the host file."""
    host_id = HostId()
    host_path = permissions_path_for_host(tmp_path, host_id)
    save_permissions(host_path, LatchkeyPermissionsConfig())
    opaque_path = new_opaque_permissions_path(tmp_path)
    # Make the handle a (stale) symlink to an unrelated target.
    stale_target = tmp_path / "somewhere-else.json"
    stale_target.write_text("{}")
    opaque_path.symlink_to(stale_target)

    point_opaque_handle_at_host(tmp_path, opaque_path, host_id)

    assert opaque_path.is_symlink()
    assert opaque_path.resolve() == host_path.resolve()


# -- Permissions config tests --


def test_save_permissions_uses_mode_0o600(tmp_path: Path) -> None:
    path = tmp_path / "hosts" / "host-id" / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    assert path.is_file()


def test_save_permissions_writes_atomically_with_no_leftover_temp(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))

    leftovers = list(tmp_path.glob("latchkey_permissions.json.*"))
    assert leftovers == []


def test_save_permissions_serializes_rules_only(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    raw = json.loads(path.read_text())
    assert raw == {"rules": [{"slack-api": ["slack-read-all"]}]}


def test_save_permissions_creates_parent_directories(tmp_path: Path) -> None:
    deep_path = tmp_path / "a" / "b" / "c" / "latchkey_permissions.json"
    save_permissions(deep_path, LatchkeyPermissionsConfig())

    assert deep_path.is_file()


def test_save_permissions_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "latchkey_permissions.json"
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    save_permissions(
        path,
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all", "slack-write-messages"]},)),
    )

    raw = json.loads(path.read_text())
    assert raw == {"rules": [{"slack-api": ["slack-read-all", "slack-write-messages"]}]}
    assert not (tmp_path / "latchkey_permissions.json.tmp").exists()


def test_permissions_path_for_host_uses_hosts_subdir(tmp_path: Path) -> None:
    host_id = HostId()
    path = permissions_path_for_host(tmp_path, host_id)
    assert path == tmp_path / "hosts" / str(host_id) / "latchkey_permissions.json"


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


# -- Forward-info gateway-port stamping ----------------------------------------


def _make_forward_info(pid: int = 4242, gateway_port: int | None = None) -> LatchkeyForwardInfo:
    return LatchkeyForwardInfo(
        pid=pid,
        started_at=datetime.now(timezone.utc),
        gateway_port=gateway_port,
    )


def test_forward_info_defaults_gateway_port_to_none() -> None:
    """Records written before the gateway binds carry an absent port."""
    info = LatchkeyForwardInfo(pid=1, started_at=datetime.now(timezone.utc))
    assert info.gateway_port is None


def test_update_forward_info_gateway_port_stamps_existing_record(tmp_path: Path) -> None:
    """Stamping the bound port preserves pid/started_at on the existing record."""
    original = _make_forward_info(pid=4242)
    save_forward_info(tmp_path, original)
    update_forward_info_gateway_port(tmp_path, gateway_port=32867)
    updated = load_forward_info(tmp_path)
    assert updated is not None
    assert updated.pid == 4242
    assert updated.started_at == original.started_at
    assert updated.gateway_port == 32867


def test_update_forward_info_gateway_port_raises_when_record_absent(tmp_path: Path) -> None:
    """Missing record => :class:`LatchkeyStoreError`; never silently drops the stamp.

    Silently moving on would leave the gateway running but invisible
    to anything polling for ``gateway_port`` (notably the minds
    desktop client during startup), which is a worse failure mode
    than crashing the supervisor.
    """
    with pytest.raises(LatchkeyStoreError) as exc_info:
        update_forward_info_gateway_port(tmp_path, gateway_port=32867)
    assert "32867" in str(exc_info.value)
    assert load_forward_info(tmp_path) is None
