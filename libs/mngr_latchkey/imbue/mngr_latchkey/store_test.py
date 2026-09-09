import json
import os
import time
from pathlib import Path

import pytest
from filelock import ReadWriteLock

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.store import LatchkeyForwardOwner
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import _LOCK_ACQUIRE_TIMEOUT_SECONDS
from imbue.mngr_latchkey.store import acquire_forward_lock
from imbue.mngr_latchkey.store import admin_permissions_path
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import ensure_admin_permissions_file
from imbue.mngr_latchkey.store import forward_events_log_path
from imbue.mngr_latchkey.store import forward_info_path
from imbue.mngr_latchkey.store import forward_lock_path
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import forward_owner_path
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
from imbue.mngr_latchkey.store import load_forward_info
from imbue.mngr_latchkey.store import load_forward_owner
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import point_opaque_handle_at_host
from imbue.mngr_latchkey.store import probe_forward_lock
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import update_forward_owner_gateway_port

# The gateway's bound port is stamped onto the owner record beside the
# ownership lock; the password is never persisted (callers derive it via
# ``Latchkey.derive_gateway_password``). The supervisor's use of these helpers
# is covered in ``forward_supervisor_test.py``.


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


# -- Pre-lock forward record ---------------------------------------------------


def test_a_pre_lock_record_on_disk_still_parses(tmp_path: Path) -> None:
    """Every record a pre-lock build wrote carries a gateway port, and must still be read.

    CLEANUP: delete with ``_pre_lock_migration``.

    The model forbids extra keys, so dropping ``gateway_port`` as unread would
    make this record unparseable, and the migration would stop seeing the
    forward that wrote it.
    """
    forward_info_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    forward_info_path(tmp_path).write_text(
        json.dumps({"pid": 4242, "started_at": "2026-01-01T00:00:00Z", "gateway_port": 32867})
    )
    info = load_forward_info(tmp_path)
    assert info is not None
    assert info.pid == 4242


# -- Forward ownership lock ----------------------------------------------------


def test_forward_lock_path_lives_under_data_dir(tmp_path: Path) -> None:
    assert forward_lock_path(tmp_path) == tmp_path / "latchkey_forward.lock"


def test_acquire_forward_lock_stamps_the_owner_and_refuses_a_second_holder(tmp_path: Path) -> None:
    """The holder is identifiable from the directory, and no one else can take the lock.

    ``is_singleton=False`` gives the second acquire its own connection to the
    lock database rather than the first one back, so it contends exactly as a
    second ``mngr latchkey forward`` process would.
    """
    lock = acquire_forward_lock(tmp_path)
    assert lock is not None
    try:
        owner = load_forward_owner(tmp_path)
        assert owner is not None
        assert owner.pid == os.getpid()
        assert acquire_forward_lock(tmp_path) is None
    finally:
        lock.release()


def test_acquire_forward_lock_raises_when_the_lock_file_cannot_be_opened(tmp_path: Path) -> None:
    """A lock that cannot be taken at all is a store failure, never contention.

    ``None`` means "another forward owns this directory", so returning it here
    would name a process that does not exist. A directory standing where the
    lock file belongs is refused by the kernel whatever the caller's privileges
    are, unlike an unreadable file, which root would open anyway.
    """
    forward_lock_path(tmp_path).mkdir(parents=True)
    with pytest.raises(LatchkeyStoreError):
        acquire_forward_lock(tmp_path)


def test_probe_forward_lock_reports_the_holder_and_nothing_once_released(tmp_path: Path) -> None:
    """Ownership is the kernel's answer, so it follows the lock rather than the file.

    The file is left behind untouched by the release, which is exactly the state
    a departed owner leaves; it must still read as unowned.
    """
    lock = acquire_forward_lock(tmp_path)
    assert lock is not None
    held = probe_forward_lock(tmp_path)
    assert held is not None and held.pid == os.getpid()
    lock.release()
    assert forward_lock_path(tmp_path).is_file()
    assert probe_forward_lock(tmp_path) is None


def test_probe_forward_lock_does_not_keep_the_lock_it_tested(tmp_path: Path) -> None:
    """Probing must leave the lock takeable, or it would lock out the next forward."""
    lock = acquire_forward_lock(tmp_path)
    assert lock is not None
    lock.release()
    assert probe_forward_lock(tmp_path) is None
    second_lock = acquire_forward_lock(tmp_path)
    assert second_lock is not None
    second_lock.release()


def test_acquire_forward_lock_retries_contention_rather_than_refusing_at_once(tmp_path: Path) -> None:
    """Contention is retried for the full timeout, not answered on the first attempt.

    :func:`probe_forward_lock` takes the lock for an instant to test it, so a
    forward starting at that instant meets contention that clears immediately.
    An acquire that gave its answer on the first attempt would refuse to run at
    all. Elapsed time is what distinguishes the two: against a lock that is
    never released, retrying spends the whole timeout and not retrying returns
    at once.
    """
    held_lock = acquire_forward_lock(tmp_path)
    assert held_lock is not None
    try:
        started_at = time.monotonic()
        assert acquire_forward_lock(tmp_path) is None
        assert time.monotonic() - started_at >= _LOCK_ACQUIRE_TIMEOUT_SECONDS / 2, (
            "the acquire answered on its first attempt instead of retrying contention"
        )
    finally:
        held_lock.release()


def test_probe_forward_lock_is_not_blocked_by_another_probe(tmp_path: Path) -> None:
    """A probe in flight elsewhere must not read as an owner.

    A probe answers "is anyone holding this exclusively" by taking the lock for
    an instant. Taking that instant *exclusively* would make two probes of a
    free directory contend, and the loser would report the departed pid still
    recorded on disk as live -- which the reaper would then signal. The shared
    lock held here is what an in-flight probe holds.
    """
    forward_owner_path(tmp_path).write_text(LatchkeyForwardOwner(pid=os.getpid()).model_dump_json())
    in_flight_probe = ReadWriteLock(str(forward_lock_path(tmp_path)), is_singleton=False)
    in_flight_probe.acquire_read(blocking=False)
    try:
        assert probe_forward_lock(tmp_path) is None
    finally:
        in_flight_probe.release()


def test_the_gateway_port_is_read_back_from_where_it_is_written(tmp_path: Path) -> None:
    """The port's writer and its readers must agree on which file carries it.

    The forward stamps the port after binding, and ``gateway-info`` plus the
    desktop client read it. If those drift apart the port is written somewhere
    nobody looks and the gateway is invisible, with no error anywhere.
    """
    lock = acquire_forward_lock(tmp_path)
    assert lock is not None
    try:
        before = load_forward_owner(tmp_path)
        assert before is not None and before.gateway_port is None
        update_forward_owner_gateway_port(tmp_path, 32867)
        owner = load_forward_owner(tmp_path)
        assert owner is not None
        assert owner.gateway_port == 32867
        assert owner.pid == os.getpid(), "stamping the port must not disturb the recorded owner"
    finally:
        lock.release()


def test_stamping_a_port_with_no_owner_is_refused(tmp_path: Path) -> None:
    """Better to fail than to invent an owner record the lock does not back."""
    with pytest.raises(LatchkeyStoreError, match="No forward owner recorded"):
        update_forward_owner_gateway_port(tmp_path, 32867)


def test_load_forward_owner_reads_none_from_an_absent_empty_or_malformed_record(tmp_path: Path) -> None:
    """Every unusable owner record reads as "nobody owns this", never as a bogus owner.

    A reader is one probe among several on a file it does not lock, so whatever
    it finds there must not raise or invent a pid.
    """
    assert load_forward_owner(tmp_path) is None
    path = forward_owner_path(tmp_path)
    path.write_text("")
    assert load_forward_owner(tmp_path) is None
    path.write_text('{"pid": 4242')
    assert load_forward_owner(tmp_path) is None


# -- schemas block -------------------------------------------------------------


def test_save_and_load_round_trips_schemas(tmp_path: Path) -> None:
    """A non-empty ``schemas`` map survives a save/load round-trip."""
    path = tmp_path / "perms.json"
    config = LatchkeyPermissionsConfig(rules=({"claude-ai": ["everything"]},), schemas={"claude-ai": {"x": 1}})
    save_permissions(path, config)

    assert json.loads(path.read_text())["schemas"] == {"claude-ai": {"x": 1}}
    assert load_permissions(path).schemas == {"claude-ai": {"x": 1}}


def test_save_omits_empty_schemas(tmp_path: Path) -> None:
    """An empty ``schemas`` map is dropped from the file, matching the pre-existing shape."""
    path = tmp_path / "perms.json"
    save_permissions(path, LatchkeyPermissionsConfig())
    assert "schemas" not in json.loads(path.read_text())


def test_load_drops_a_legacy_include_key(tmp_path: Path) -> None:
    """An ``include`` written by an older build is ignored on load and gone on the next save."""
    path = tmp_path / "perms.json"
    path.write_text(json.dumps({"rules": [], "include": ["minds_shared_schemas.json"]}))

    save_permissions(path, load_permissions(path))

    assert "include" not in json.loads(path.read_text())
