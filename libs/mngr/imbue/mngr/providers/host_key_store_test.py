"""Unit tests for the structured host-key pin store and its known_hosts renderer."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import HostKeyPin
from imbue.mngr.providers.host_key_store import HostKeyStoreState
from imbue.mngr.providers.host_key_store import UNATTRIBUTED_HOST_KEY_RECORD_ID
from imbue.mngr.providers.host_key_store import _apply_pin
from imbue.mngr.providers.host_key_store import clear_endpoint_pins
from imbue.mngr.providers.host_key_store import gc_dead_host_key_records
from imbue.mngr.providers.host_key_store import has_host_key_store
from imbue.mngr.providers.host_key_store import has_unpinned_bootstrap_drift
from imbue.mngr.providers.host_key_store import host_key_store_path
from imbue.mngr.providers.host_key_store import load_current_host_key_pins
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.host_key_store import move_host_endpoint_pins
from imbue.mngr.providers.host_key_store import parse_known_hosts_address
from imbue.mngr.providers.host_key_store import pin_host_key
from imbue.mngr.providers.host_key_store import pin_known_hosts_text
from imbue.mngr.providers.host_key_store import pin_sole_endpoint_host_key
from imbue.mngr.providers.host_key_store import remove_host_key_record
from imbue.mngr.providers.host_key_store import render_known_hosts_file
from imbue.mngr.providers.host_key_store import render_pins_as_known_hosts_text
from imbue.mngr.utils.testing import allow_warnings

_ED25519_KEY_A = "ssh-ed25519 AAAAkeyA"
_ED25519_KEY_B = "ssh-ed25519 AAAAkeyB"
_RSA_KEY = "ssh-rsa AAAArsakey"


# =============================================================================
# parse_known_hosts_address
# =============================================================================


def test_parse_known_hosts_address_round_trips_default_and_bracketed_forms() -> None:
    assert parse_known_hosts_address("example.com") == ("example.com", 22)
    assert parse_known_hosts_address("[example.com]:2222") == ("example.com", 2222)
    assert parse_known_hosts_address("[127.0.0.1]:60022") == ("127.0.0.1", 60022)


def test_parse_known_hosts_address_rejects_unrepresentable_patterns() -> None:
    assert parse_known_hosts_address("|1|hashed|entry") is None
    assert parse_known_hosts_address("@cert-authority") is None
    assert parse_known_hosts_address("host1,host2") is None
    assert parse_known_hosts_address("*.example.com") is None
    assert parse_known_hosts_address("") is None


# =============================================================================
# pin_host_key: rendering + replace semantics
# =============================================================================


def test_pin_host_key_creates_store_and_renders_known_hosts(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    assert has_host_key_store(known_hosts)
    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_A}\n"


def test_pin_host_key_uses_bracketed_address_for_nonstandard_port(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"

    pin_host_key(known_hosts, "example.com", 2222, _ED25519_KEY_A, host_id=None, origin=HostKeyOrigin.BOOTSTRAP)

    assert known_hosts.read_text() == f"[example.com]:2222 {_ED25519_KEY_A}\n"


def test_pin_host_key_replaces_same_endpoint_and_keytype(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_B, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    content = known_hosts.read_text()
    assert "AAAAkeyA" not in content
    assert content == f"example.com {_ED25519_KEY_B}\n"


def test_pin_host_key_preserves_other_keytypes_at_same_endpoint(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _RSA_KEY, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    content = known_hosts.read_text()
    assert f"example.com {_RSA_KEY}" in content
    assert f"example.com {_ED25519_KEY_A}" in content


def test_pin_host_key_replaces_endpoint_pin_across_host_records(tmp_path: Path) -> None:
    """A recycled endpoint (e.g. a reshuffled docker port) moves to the new host's record."""
    known_hosts = tmp_path / "known_hosts"
    first_host = HostId.generate()
    second_host = HostId.generate()

    pin_host_key(known_hosts, "127.0.0.1", 3333, _ED25519_KEY_A, host_id=first_host, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(known_hosts, "127.0.0.1", 3333, _ED25519_KEY_B, host_id=second_host, origin=HostKeyOrigin.BOOTSTRAP)

    assert known_hosts.read_text() == f"[127.0.0.1]:3333 {_ED25519_KEY_B}\n"
    first_record = load_host_key_record(known_hosts, first_host)
    assert first_record is None or first_record.pins == ()
    second_record = load_host_key_record(known_hosts, second_host)
    assert second_record is not None
    assert [pin.public_key for pin in second_record.pins] == [_ED25519_KEY_B]


# =============================================================================
# origin precedence
# =============================================================================


def test_bootstrap_pin_never_displaces_user_pin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_B, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_A}\n"


def test_newer_user_pin_replaces_user_pin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_B, host_id=host_id, origin=HostKeyOrigin.USER)

    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_B}\n"


def test_user_pin_replaces_bootstrap_pin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_B, host_id=host_id, origin=HostKeyOrigin.USER)

    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_B}\n"


def test_older_user_material_does_not_replace_newer_user_pin() -> None:
    now = datetime.now(timezone.utc)
    newer_pin = HostKeyPin(
        address="example.com",
        port=22,
        keytype="ssh-ed25519",
        public_key=_ED25519_KEY_A,
        origin=HostKeyOrigin.USER,
        updated_at=now,
    )
    state = _apply_pin(HostKeyStoreState(), "host-a", newer_pin, is_add_if_absent=False)

    older_pin = HostKeyPin(
        address="example.com",
        port=22,
        keytype="ssh-ed25519",
        public_key=_ED25519_KEY_B,
        origin=HostKeyOrigin.USER,
        updated_at=now - timedelta(hours=1),
    )
    updated_state = _apply_pin(state, "host-a", older_pin, is_add_if_absent=False)

    assert [pin.public_key for record in updated_state.records for pin in record.pins] == [_ED25519_KEY_A]


def test_add_if_absent_keeps_existing_bootstrap_pin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(
        known_hosts,
        "example.com",
        22,
        _ED25519_KEY_B,
        host_id=host_id,
        origin=HostKeyOrigin.BOOTSTRAP,
        is_add_if_absent=True,
    )

    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_A}\n"


def test_add_if_absent_pins_when_endpoint_has_no_entry(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(
        known_hosts,
        "example.com",
        22,
        _ED25519_KEY_A,
        host_id=host_id,
        origin=HostKeyOrigin.BOOTSTRAP,
        is_add_if_absent=True,
    )

    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_A}\n"


def test_add_if_absent_allows_pin_for_different_keytype(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _RSA_KEY, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(
        known_hosts,
        "example.com",
        22,
        _ED25519_KEY_A,
        host_id=host_id,
        origin=HostKeyOrigin.BOOTSTRAP,
        is_add_if_absent=True,
    )

    content = known_hosts.read_text()
    assert f"example.com {_RSA_KEY}" in content
    assert f"example.com {_ED25519_KEY_A}" in content


# =============================================================================
# import of out-of-band known_hosts content
# =============================================================================


def test_first_store_write_imports_preexisting_file_lines(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"legacy.example.com {_RSA_KEY}\n")

    pin_host_key(
        known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=HostId.generate(), origin=HostKeyOrigin.BOOTSTRAP
    )

    content = known_hosts.read_text()
    assert f"legacy.example.com {_RSA_KEY}" in content
    assert f"example.com {_ED25519_KEY_A}" in content


def test_lines_written_behind_the_store_survive_the_next_render(tmp_path: Path) -> None:
    """A pre-store writer (e.g. an old minds materializer) appending to the file loses nothing."""
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    with open(known_hosts, "a") as f:
        f.write(f"other.example.com {_ED25519_KEY_B}\n")
    render_known_hosts_file(known_hosts)

    content = known_hosts.read_text()
    assert f"example.com {_ED25519_KEY_A}" in content
    assert f"other.example.com {_ED25519_KEY_B}" in content


def test_out_of_band_replacement_of_an_endpoint_line_wins_over_bootstrap_pin(tmp_path: Path) -> None:
    """A synced record's replace-by-endpoint merge into the file is honored, not reverted."""
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    known_hosts.write_text(f"example.com {_ED25519_KEY_B}\n")
    render_known_hosts_file(known_hosts)

    assert known_hosts.read_text() == f"example.com {_ED25519_KEY_B}\n"


def test_unparseable_lines_are_preserved_verbatim(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    hashed_line = "|1|abcdef|012345= ssh-ed25519 AAAAhashed"
    known_hosts.write_text(f"{hashed_line}\n")

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=None, origin=HostKeyOrigin.BOOTSTRAP)

    content = known_hosts.read_text()
    assert hashed_line in content
    assert f"example.com {_ED25519_KEY_A}" in content


# =============================================================================
# sole-endpoint pinning
# =============================================================================


def test_pin_sole_endpoint_host_key_drops_prior_endpoints(tmp_path: Path) -> None:
    """Single-endpoint files (lima's reassigned forwarded port) keep only the current endpoint."""
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    pin_sole_endpoint_host_key(known_hosts, "127.0.0.1", 60022, _ED25519_KEY_A, host_id, HostKeyOrigin.BOOTSTRAP)
    pin_sole_endpoint_host_key(known_hosts, "127.0.0.1", 60099, _ED25519_KEY_A, host_id, HostKeyOrigin.BOOTSTRAP)

    assert known_hosts.read_text() == f"[127.0.0.1]:60099 {_ED25519_KEY_A}\n"


def test_pin_sole_endpoint_host_key_drops_imported_stale_endpoint_lines(tmp_path: Path) -> None:
    """A pre-store single-line file at an old port is superseded, not accumulated."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:60022 {_ED25519_KEY_A}\n")

    pin_sole_endpoint_host_key(
        known_hosts, "127.0.0.1", 60099, _ED25519_KEY_A, HostId.generate(), HostKeyOrigin.BOOTSTRAP
    )

    assert known_hosts.read_text() == f"[127.0.0.1]:60099 {_ED25519_KEY_A}\n"


# =============================================================================
# clearing, per-host removal, GC
# =============================================================================


def test_clear_endpoint_pins_drops_all_keytypes_for_endpoint_only(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(known_hosts, "example.com", 22, _RSA_KEY, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)
    pin_host_key(known_hosts, "other.com", 22, _ED25519_KEY_B, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    clear_endpoint_pins(known_hosts, "example.com", 22)

    assert known_hosts.read_text() == f"other.com {_ED25519_KEY_B}\n"


def test_remove_host_key_record_drops_only_that_hosts_pins(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    dead_host = HostId.generate()
    live_host = HostId.generate()
    pin_host_key(
        known_hosts, "dead.example.com", 22, _ED25519_KEY_A, host_id=dead_host, origin=HostKeyOrigin.BOOTSTRAP
    )
    pin_host_key(
        known_hosts, "live.example.com", 22, _ED25519_KEY_B, host_id=live_host, origin=HostKeyOrigin.BOOTSTRAP
    )

    remove_host_key_record(known_hosts, dead_host)

    assert known_hosts.read_text() == f"live.example.com {_ED25519_KEY_B}\n"
    assert load_host_key_record(known_hosts, dead_host) is None


def test_gc_dead_host_key_records_keeps_live_and_unattributed_pins(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    dead_host = HostId.generate()
    live_host = HostId.generate()
    pin_host_key(
        known_hosts, "dead.example.com", 22, _ED25519_KEY_A, host_id=dead_host, origin=HostKeyOrigin.BOOTSTRAP
    )
    pin_host_key(
        known_hosts, "live.example.com", 22, _ED25519_KEY_B, host_id=live_host, origin=HostKeyOrigin.BOOTSTRAP
    )
    pin_host_key(known_hosts, "shared.example.com", 22, _RSA_KEY, host_id=None, origin=HostKeyOrigin.BOOTSTRAP)

    gc_dead_host_key_records(known_hosts, {live_host})

    content = known_hosts.read_text()
    assert "dead.example.com" not in content
    assert f"live.example.com {_ED25519_KEY_B}" in content
    assert f"shared.example.com {_RSA_KEY}" in content


# =============================================================================
# record attribution
# =============================================================================


def test_load_host_key_record_returns_pins_attributed_to_host(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "example.com", 2222, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)

    record = load_host_key_record(known_hosts, host_id)

    assert record is not None
    assert len(record.pins) == 1
    only_pin = record.pins[0]
    assert only_pin.address == "example.com"
    assert only_pin.port == 2222
    assert only_pin.keytype == "ssh-ed25519"
    assert only_pin.origin is HostKeyOrigin.USER


def test_attributed_repin_of_imported_key_moves_pin_to_host_record(tmp_path: Path) -> None:
    """A legacy line imported as unattributed is re-attributed on the host's next pin, so GC clears it."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"example.com {_ED25519_KEY_A}\n")
    host_id = HostId.generate()

    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    record = load_host_key_record(known_hosts, host_id)
    assert record is not None
    assert [pin.public_key for pin in record.pins] == [_ED25519_KEY_A]

    remove_host_key_record(known_hosts, host_id)
    assert known_hosts.read_text() == ""


def test_pins_without_host_id_land_on_the_unattributed_record(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=None, origin=HostKeyOrigin.BOOTSTRAP)

    store_content = host_key_store_path(known_hosts).read_text()
    assert UNATTRIBUTED_HOST_KEY_RECORD_ID in store_content


# =============================================================================
# move_host_endpoint_pins
# =============================================================================


def test_move_host_endpoint_pins_relocates_keys_and_preserves_origin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "1.2.3.4", 22010, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)
    pin_host_key(known_hosts, "1.2.3.4", 22011, _ED25519_KEY_B, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    move_host_endpoint_pins(known_hosts, host_id, "1.2.3.4", 22010, "5.6.7.8", 23010)
    move_host_endpoint_pins(known_hosts, host_id, "1.2.3.4", 22011, "5.6.7.8", 23011)

    record = load_host_key_record(known_hosts, host_id)
    assert record is not None
    pins_by_endpoint = {(pin.address, pin.port): pin for pin in record.pins}
    assert set(pins_by_endpoint) == {("5.6.7.8", 23010), ("5.6.7.8", 23011)}
    assert pins_by_endpoint[("5.6.7.8", 23010)].public_key == _ED25519_KEY_A
    assert pins_by_endpoint[("5.6.7.8", 23010)].origin is HostKeyOrigin.USER
    assert pins_by_endpoint[("5.6.7.8", 23011)].public_key == _ED25519_KEY_B
    assert pins_by_endpoint[("5.6.7.8", 23011)].origin is HostKeyOrigin.BOOTSTRAP
    rendered = known_hosts.read_text()
    assert "1.2.3.4" not in rendered
    assert f"[5.6.7.8]:23010 {_ED25519_KEY_A}\n" in rendered


def test_move_host_endpoint_pins_evicts_a_recycled_endpoints_stale_pins(tmp_path: Path) -> None:
    """The new endpoint may have hosted a different slice before; whatever was pinned
    there is stale the moment the endpoint is reassigned, even a user-origin pin."""
    known_hosts = tmp_path / "known_hosts"
    moving_host = HostId.generate()
    prior_host = HostId.generate()
    pin_host_key(known_hosts, "1.2.3.4", 22010, _ED25519_KEY_A, host_id=moving_host, origin=HostKeyOrigin.USER)
    pin_host_key(known_hosts, "5.6.7.8", 23010, _RSA_KEY, host_id=prior_host, origin=HostKeyOrigin.USER)

    move_host_endpoint_pins(known_hosts, moving_host, "1.2.3.4", 22010, "5.6.7.8", 23010)

    record = load_host_key_record(known_hosts, moving_host)
    assert record is not None
    assert [(pin.address, pin.port, pin.public_key) for pin in record.pins] == [("5.6.7.8", 23010, _ED25519_KEY_A)]
    prior_record = load_host_key_record(known_hosts, prior_host)
    assert prior_record is None or prior_record.pins == ()


def test_move_host_endpoint_pins_is_a_noop_without_pins_at_the_old_endpoint(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    other_host = HostId.generate()
    pin_host_key(known_hosts, "9.9.9.9", 22, _ED25519_KEY_A, host_id=other_host, origin=HostKeyOrigin.USER)

    move_host_endpoint_pins(known_hosts, host_id, "1.2.3.4", 22010, "5.6.7.8", 23010)

    other_record = load_host_key_record(known_hosts, other_host)
    assert other_record is not None
    assert [pin.public_key for pin in other_record.pins] == [_ED25519_KEY_A]
    assert load_host_key_record(known_hosts, host_id) is None


# =============================================================================
# load_current_host_key_pins / render_pins_as_known_hosts_text
# =============================================================================


def test_load_current_host_key_pins_returns_store_pins_without_writing(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)
    rendered_before = known_hosts.read_text()

    pins = load_current_host_key_pins(known_hosts)

    assert [(pin.address, pin.port, pin.public_key, pin.origin) for pin in pins] == [
        ("example.com", 22, _ED25519_KEY_A, HostKeyOrigin.USER)
    ]
    assert known_hosts.read_text() == rendered_before


def test_load_current_host_key_pins_folds_a_pre_store_file_in_memory(tmp_path: Path) -> None:
    """A legacy known_hosts file with no sidecar still yields its pins -- deduplicated
    per (endpoint, keytype), so a stale line an old append-only writer left behind an
    updated one collapses to the later line -- without a sidecar being created."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        f"[1.2.3.4]:22001 {_ED25519_KEY_A}\n[1.2.3.4]:22001 {_ED25519_KEY_B}\n[1.2.3.4]:23001 {_RSA_KEY}\n"
    )

    pins = load_current_host_key_pins(known_hosts)

    assert sorted((pin.address, pin.port, pin.public_key) for pin in pins) == [
        ("1.2.3.4", 22001, _ED25519_KEY_B),
        ("1.2.3.4", 23001, _RSA_KEY),
    ]
    assert not has_host_key_store(known_hosts)


def test_load_current_host_key_pins_returns_empty_when_nothing_exists(tmp_path: Path) -> None:
    assert load_current_host_key_pins(tmp_path / "known_hosts") == ()


def test_render_pins_as_known_hosts_text_round_trips_through_the_parser(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "example.com", 22, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)
    pin_host_key(known_hosts, "example.com", 2222, _RSA_KEY, host_id=host_id, origin=HostKeyOrigin.USER)

    text = render_pins_as_known_hosts_text(load_current_host_key_pins(known_hosts))

    assert text == f"[example.com]:2222 {_RSA_KEY}\nexample.com {_ED25519_KEY_A}\n"
    # The rendered text parses back into the same endpoint/key set.
    other_file = tmp_path / "other_known_hosts"
    pin_known_hosts_text(other_file, text, host_id=host_id, origin=HostKeyOrigin.USER)
    reparsed = load_current_host_key_pins(other_file)
    assert sorted((pin.address, pin.port, pin.public_key) for pin in reparsed) == [
        ("example.com", 22, _ED25519_KEY_A),
        ("example.com", 2222, _RSA_KEY),
    ]


# =============================================================================
# pin_known_hosts_text
# =============================================================================


def test_pin_known_hosts_text_applies_parsed_pins_with_replace_semantics(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "1.2.3.4", 22001, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.BOOTSTRAP)

    pin_known_hosts_text(
        known_hosts,
        f"[1.2.3.4]:22001 {_ED25519_KEY_B}\n[1.2.3.4]:23001 {_RSA_KEY}\n",
        host_id=host_id,
        origin=HostKeyOrigin.USER,
    )

    content = known_hosts.read_text()
    assert "AAAAkeyA" not in content
    assert f"[1.2.3.4]:22001 {_ED25519_KEY_B}" in content
    assert f"[1.2.3.4]:23001 {_RSA_KEY}" in content
    record = load_host_key_record(known_hosts, host_id)
    assert record is not None
    assert all(pin.origin is HostKeyOrigin.USER for pin in record.pins)


def test_pin_known_hosts_text_never_displaces_a_newer_user_pin(tmp_path: Path) -> None:
    """Origin precedence still applies to imported text: an already-present user pin
    stamped no earlier than the import stays (the import is not newer material)."""
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    pin_host_key(known_hosts, "1.2.3.4", 22001, _ED25519_KEY_A, host_id=host_id, origin=HostKeyOrigin.USER)
    record = load_host_key_record(known_hosts, host_id)
    assert record is not None
    future_pin = record.pins[0].model_copy_update(
        to_update(record.pins[0].field_ref().updated_at, datetime.now(timezone.utc) + timedelta(hours=1)),
    )
    _save_future_stamped_pin(known_hosts, host_id, future_pin)

    pin_known_hosts_text(
        known_hosts, f"[1.2.3.4]:22001 {_ED25519_KEY_B}\n", host_id=host_id, origin=HostKeyOrigin.USER
    )

    assert f"[1.2.3.4]:22001 {_ED25519_KEY_A}" in known_hosts.read_text()
    assert "AAAAkeyB" not in known_hosts.read_text()


def _save_future_stamped_pin(known_hosts: Path, host_id: HostId, pin: HostKeyPin) -> None:
    """Rewrite one host's record so its sole pin carries the given (future) timestamp."""
    store_path = host_key_store_path(known_hosts)
    state = HostKeyStoreState.model_validate_json(store_path.read_text())
    updated_records = tuple(
        record.model_copy_update(to_update(record.field_ref().pins, (pin,)))
        if record.host_id == str(host_id)
        else record
        for record in state.records
    )
    store_path.write_text(
        state.model_copy_update(to_update(state.field_ref().records, updated_records)).model_dump_json(indent=2)
    )


def test_pin_known_hosts_text_skips_unparseable_lines_and_blank_lines(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    with allow_warnings():
        pin_known_hosts_text(
            known_hosts,
            f"|1|hashed|entry ssh-ed25519 AAAA\n\nnot-enough-fields\n[1.2.3.4]:22001 {_ED25519_KEY_A}\n",
            host_id=host_id,
            origin=HostKeyOrigin.USER,
        )

    assert known_hosts.read_text() == f"[1.2.3.4]:22001 {_ED25519_KEY_A}\n"


def test_pin_known_hosts_text_with_no_parseable_lines_touches_nothing(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"

    with allow_warnings():
        pin_known_hosts_text(known_hosts, "junk line\n", host_id=None, origin=HostKeyOrigin.USER)

    assert not known_hosts.exists()
    assert not has_host_key_store(known_hosts)


# =============================================================================
# has_unpinned_bootstrap_drift
# =============================================================================


def test_has_unpinned_bootstrap_drift_reports_a_wholly_unpinned_endpoint(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"

    assert has_unpinned_bootstrap_drift(known_hosts, f"[203.0.113.5]:2222 {_ED25519_KEY_A}\n") is True


def test_has_unpinned_bootstrap_drift_is_false_when_every_line_is_already_pinned(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    pin_host_key(known_hosts, "203.0.113.5", 2222, _ED25519_KEY_A, host_id=None, origin=HostKeyOrigin.BOOTSTRAP)

    assert has_unpinned_bootstrap_drift(known_hosts, f"[203.0.113.5]:2222 {_ED25519_KEY_A}\n") is False


def test_has_unpinned_bootstrap_drift_reports_a_differing_bootstrap_pin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    pin_host_key(known_hosts, "203.0.113.5", 2222, _ED25519_KEY_B, host_id=None, origin=HostKeyOrigin.BOOTSTRAP)

    assert has_unpinned_bootstrap_drift(known_hosts, f"[203.0.113.5]:2222 {_ED25519_KEY_A}\n") is True


def test_has_unpinned_bootstrap_drift_defers_to_a_differing_user_pin(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    pin_host_key(known_hosts, "203.0.113.5", 2222, _ED25519_KEY_B, host_id=None, origin=HostKeyOrigin.USER)

    assert has_unpinned_bootstrap_drift(known_hosts, f"[203.0.113.5]:2222 {_ED25519_KEY_A}\n") is False


def test_has_unpinned_bootstrap_drift_ignores_unparseable_and_blank_lines(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"

    assert has_unpinned_bootstrap_drift(known_hosts, "junk line\n\n") is False
