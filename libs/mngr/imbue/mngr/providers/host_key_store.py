"""Structured store of SSH host-key pins, from which known_hosts files are rendered.

The store is the source of truth for which host keys are trusted at which
endpoints; the known_hosts file next to it becomes a derived artifact. Each
known_hosts file gets a JSON sidecar (``<name>.pins.json``) holding one
:class:`HostKeyRecord` per host (plus one shared record for pins that were
imported from pre-store file content and cannot be attributed to a host).

Precedence rules, applied per ``(address, port, keytype)`` endpoint:

- a USER-origin pin (material originating from the user's own devices, e.g. a
  rotation or adoption) is replaced only by newer USER-origin material;
- a BOOTSTRAP-origin pin (material vouched for by an external party, e.g. a
  connector at lease handoff, or a locally-generated provider key) is
  replaceable by anything.

Every mutating operation first imports any lines that were written to the
known_hosts file behind the store's back (as BOOTSTRAP pins), so nothing that
an unmigrated writer appended is ever lost when the file is re-rendered.
"""

import fcntl
import re
from collections.abc import Callable
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from typing import Iterator

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write

# Suffixes of the sidecar files kept next to a known_hosts file: the JSON pin
# store itself, and the lock file serializing all store operations on it.
_STORE_SUFFIX: Final[str] = ".pins.json"
_LOCK_SUFFIX: Final[str] = ".pins.lock"

# Record id under which pins imported from pre-store known_hosts content (or
# written through the host_id-less shim) are kept. Not a valid HostId on
# purpose, so it can never collide with a real host's record.
UNATTRIBUTED_HOST_KEY_RECORD_ID: Final[str] = "unattributed"

# OpenSSH known_hosts ``[host]:port`` pattern for non-default ports.
_BRACKETED_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(r"^\[(?P<host>[^\]]+)\]:(?P<port>\d+)$")


class HostKeyOrigin(UpperCaseStrEnum):
    """Who vouched for a pinned host key: an external bootstrap party, or the user's own devices."""

    BOOTSTRAP = auto()
    USER = auto()


class HostKeyPin(FrozenModel):
    """One trusted host key at one endpoint."""

    address: str = Field(description="Hostname or IP the key is pinned for")
    port: int = Field(description="TCP port the key is pinned for")
    keytype: str = Field(description="OpenSSH key type of the pinned key (e.g. ssh-ed25519)")
    public_key: str = Field(description="Full OpenSSH public key: keytype, base64 blob, optional comment")
    origin: HostKeyOrigin = Field(description="Who vouched for this key")
    updated_at: datetime = Field(description="When this pin was last written")


class HostKeyRecord(FrozenModel):
    """All pins belonging to one host (or to the shared unattributed record)."""

    host_id: str = Field(description="The host these pins belong to, or the unattributed record id")
    pins: tuple[HostKeyPin, ...] = Field(default=(), description="Pins for this host's endpoints")
    client_key_path: Path | None = Field(
        default=None, description="Path of the per-host client private key that reaches this host, if recorded"
    )


class HostKeyStoreState(FrozenModel):
    """The full persisted content of one known_hosts file's pin store."""

    records: tuple[HostKeyRecord, ...] = Field(default=(), description="Per-host pin records")
    preserved_lines: tuple[str, ...] = Field(
        default=(),
        description="known_hosts lines the store cannot represent as pins (kept verbatim at render time)",
    )


@pure
def format_as_known_hosts_address(hostname: str, port: int) -> str:
    """Format a host:port pair as the leading field of an OpenSSH known_hosts line.

    OpenSSH expects a bare hostname for the default SSH port and a ``[host]:port``
    bracketed form for any non-default port.
    """
    if port == 22:
        return hostname
    return f"[{hostname}]:{port}"


@pure
def parse_known_hosts_address(host_pattern: str) -> tuple[str, int] | None:
    """Parse a known_hosts leading host field back into ``(hostname, port)``.

    Returns None for patterns the store cannot represent: hashed hosts,
    ``@``-marker lines, comma-separated multi-host patterns, and wildcards.
    """
    if not host_pattern or host_pattern.startswith(("|", "@", "#")):
        return None
    if "," in host_pattern or "*" in host_pattern or "?" in host_pattern:
        return None
    bracketed = _BRACKETED_ADDRESS_RE.match(host_pattern)
    if bracketed is not None:
        return bracketed.group("host"), int(bracketed.group("port"))
    if host_pattern.startswith("["):
        return None
    return host_pattern, 22


def host_key_store_path(known_hosts_path: Path) -> Path:
    """The JSON pin-store sidecar for a known_hosts file."""
    return known_hosts_path.with_name(known_hosts_path.name + _STORE_SUFFIX)


def has_host_key_store(known_hosts_path: Path) -> bool:
    """Whether a pin store already exists for this known_hosts file."""
    return host_key_store_path(known_hosts_path).exists()


@pure
def _parse_pin_from_line(line: str, now: datetime, origin: HostKeyOrigin) -> HostKeyPin | None:
    """Parse one known_hosts line into a pin of the given origin, or None when unrepresentable."""
    parts = line.split(None, 1)
    if len(parts) < 2:
        return None
    endpoint = parse_known_hosts_address(parts[0])
    if endpoint is None:
        return None
    key_parts = parts[1].split()
    if len(key_parts) < 2:
        return None
    return HostKeyPin(
        address=endpoint[0],
        port=endpoint[1],
        keytype=key_parts[0],
        public_key=parts[1].strip(),
        origin=origin,
        updated_at=now,
    )


@pure
def _endpoint_key(pin: HostKeyPin) -> tuple[str, int, str]:
    return (pin.address, pin.port, pin.keytype)


@pure
def _record_by_id(state: HostKeyStoreState, record_id: str) -> HostKeyRecord | None:
    for record in state.records:
        if record.host_id == record_id:
            return record
    return None


@pure
def _upsert_record(state: HostKeyStoreState, record: HostKeyRecord) -> HostKeyStoreState:
    other_records = tuple(r for r in state.records if r.host_id != record.host_id)
    kept_records = other_records + (record,) if record.pins or record.client_key_path is not None else other_records
    return state.model_copy_update(to_update(state.field_ref().records, kept_records))


@pure
def _find_pin_for_endpoint(state: HostKeyStoreState, endpoint: tuple[str, int, str]) -> tuple[str, HostKeyPin] | None:
    """Find the endpoint's pin, returning ``(owning_record_id, pin)`` or None."""
    for record in state.records:
        for pin in record.pins:
            if _endpoint_key(pin) == endpoint:
                return record.host_id, pin
    return None


@pure
def _drop_endpoint_from_all_records(state: HostKeyStoreState, endpoint: tuple[str, int, str]) -> HostKeyStoreState:
    updated_records = []
    for record in state.records:
        kept_pins = tuple(pin for pin in record.pins if _endpoint_key(pin) != endpoint)
        if kept_pins or record.client_key_path is not None:
            updated_records.append(record.model_copy_update(to_update(record.field_ref().pins, kept_pins)))
    return state.model_copy_update(to_update(state.field_ref().records, tuple(updated_records)))


@pure
def _apply_pin(
    state: HostKeyStoreState,
    record_id: str,
    pin: HostKeyPin,
    is_add_if_absent: bool,
) -> HostKeyStoreState:
    """Apply one pin to the store state, honoring origin precedence.

    Replace-by-(endpoint, keytype) across all records: at most one pin exists
    per endpoint. A USER pin is displaced only by USER material that is not
    older; a BOOTSTRAP pin is displaced by anything. With ``is_add_if_absent``,
    an existing pin of any origin at the endpoint wins outright.

    An identical pin (same key and origin) written under a different record id
    is re-attributed to that record: a legacy line imported onto the
    unattributed record moves to its host's record on the host's next
    attributed re-pin, so per-host removal at deletion actually clears it.
    """
    endpoint = _endpoint_key(pin)
    found = _find_pin_for_endpoint(state, endpoint)
    if found is not None:
        owning_record_id, existing_pin = found
        if is_add_if_absent:
            return state
        is_same_material = existing_pin.public_key == pin.public_key and existing_pin.origin == pin.origin
        if is_same_material and owning_record_id == record_id:
            return state
        if not is_same_material and existing_pin.origin is HostKeyOrigin.USER:
            if pin.origin is not HostKeyOrigin.USER or pin.updated_at < existing_pin.updated_at:
                return state

    state_without_endpoint = _drop_endpoint_from_all_records(state, endpoint)
    record = _record_by_id(state_without_endpoint, record_id) or HostKeyRecord(host_id=record_id)
    updated_record = record.model_copy_update(to_update(record.field_ref().pins, record.pins + (pin,)))
    return _upsert_record(state_without_endpoint, updated_record)


@pure
def _apply_sole_endpoint_pin(state: HostKeyStoreState, record_id: str, pin: HostKeyPin) -> HostKeyStoreState:
    """Apply ``pin`` and drop every pin at any other endpoint (single-endpoint files)."""
    pinned_state = _apply_pin(state, record_id, pin, is_add_if_absent=False)
    updated_records = []
    for record in pinned_state.records:
        kept_pins = tuple(p for p in record.pins if (p.address, p.port) == (pin.address, pin.port))
        if kept_pins or record.client_key_path is not None:
            updated_records.append(record.model_copy_update(to_update(record.field_ref().pins, kept_pins)))
    return pinned_state.model_copy_update(to_update(pinned_state.field_ref().records, tuple(updated_records)))


@pure
def _clear_endpoint_from_state(state: HostKeyStoreState, hostname: str, port: int) -> HostKeyStoreState:
    """Drop every pin (all keytypes) for ``hostname:port`` from every record."""
    updated_state = state
    endpoints = {
        _endpoint_key(pin)
        for record in state.records
        for pin in record.pins
        if pin.address == hostname and pin.port == port
    }
    for endpoint in endpoints:
        updated_state = _drop_endpoint_from_all_records(updated_state, endpoint)
    return updated_state


@pure
def _move_endpoint_pins_in_state(
    state: HostKeyStoreState,
    record_id: str,
    old_endpoint: tuple[str, int],
    new_endpoint: tuple[str, int],
    now: datetime,
) -> HostKeyStoreState:
    """Relocate one host's pins from an old endpoint to a new one, preserving keys and origins.

    The new endpoint was just (re)assigned to this host, so whatever pins sat
    there before (endpoint recycling across hosts) are stale and dropped
    outright before the host's own pins land.
    """
    record = _record_by_id(state, record_id)
    if record is None:
        return state
    moving_pins = tuple(pin for pin in record.pins if (pin.address, pin.port) == old_endpoint)
    if not moving_pins:
        return state
    cleared_state = _clear_endpoint_from_state(state, new_endpoint[0], new_endpoint[1])
    updated_state = cleared_state
    for pin in moving_pins:
        updated_state = _drop_endpoint_from_all_records(updated_state, _endpoint_key(pin))
        moved_pin = pin.model_copy_update(
            to_update(pin.field_ref().address, new_endpoint[0]),
            to_update(pin.field_ref().port, new_endpoint[1]),
            to_update(pin.field_ref().updated_at, now),
        )
        updated_record = _record_by_id(updated_state, record_id) or HostKeyRecord(host_id=record_id)
        updated_state = _upsert_record(
            updated_state,
            updated_record.model_copy_update(
                to_update(updated_record.field_ref().pins, updated_record.pins + (moved_pin,))
            ),
        )
    return updated_state


def move_host_endpoint_pins(
    known_hosts_path: Path,
    host_id: HostId,
    old_hostname: str,
    old_port: int,
    new_hostname: str,
    new_port: int,
) -> None:
    """Relocate ``host_id``'s pins from one endpoint to another and re-render the known_hosts file.

    For hosts whose keys survive a move but whose address/port change (e.g. a
    stopped host whose disks a provider restores at a new endpoint): the same
    public keys are re-pinned at the new endpoint with their origins intact --
    so a user-origin pin stays user-origin and no re-trust decision is made --
    and the dead endpoint's pins are dropped. A no-op when the host has no pins
    at the old endpoint.
    """
    now = datetime.now(timezone.utc)
    _mutate_store(
        known_hosts_path,
        lambda state: _move_endpoint_pins_in_state(
            state, str(host_id), (old_hostname, old_port), (new_hostname, new_port), now
        ),
    )


@pure
def _keep_records_in_state(state: HostKeyStoreState, keep: Callable[[str], bool]) -> HostKeyStoreState:
    """Keep only the records whose id satisfies ``keep``."""
    kept_records = tuple(record for record in state.records if keep(record.host_id))
    return state.model_copy_update(to_update(state.field_ref().records, kept_records))


def _import_known_hosts_file(state: HostKeyStoreState, known_hosts_path: Path) -> HostKeyStoreState:
    """Fold lines written to the known_hosts file behind the store's back into the state.

    Parseable lines whose key is not already pinned are imported as BOOTSTRAP
    pins on the unattributed record (so precedence still protects USER pins);
    unrepresentable lines are preserved verbatim.
    """
    if not known_hosts_path.exists():
        return state
    try:
        raw_text = known_hosts_path.read_text()
    except OSError as e:
        logger.warning("Could not read known_hosts file {} for import: {}", known_hosts_path, e)
        return state

    now = datetime.now(timezone.utc)
    updated_state = state
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pin = _parse_pin_from_line(line, now, HostKeyOrigin.BOOTSTRAP)
        if pin is None:
            if line not in updated_state.preserved_lines:
                updated_state = updated_state.model_copy_update(
                    to_update(updated_state.field_ref().preserved_lines, updated_state.preserved_lines + (line,))
                )
            continue
        found = _find_pin_for_endpoint(updated_state, _endpoint_key(pin))
        if found is not None and found[1].public_key == pin.public_key:
            continue
        updated_state = _apply_pin(updated_state, UNATTRIBUTED_HOST_KEY_RECORD_ID, pin, is_add_if_absent=False)
    return updated_state


def _load_state(store_path: Path) -> HostKeyStoreState:
    if not store_path.exists():
        return HostKeyStoreState()
    try:
        return HostKeyStoreState.model_validate_json(store_path.read_text())
    except (OSError, ValueError) as e:
        # The rendered known_hosts file still holds every pin, and the import
        # step re-populates the store from it, so starting empty loses nothing
        # beyond origin/host attribution.
        logger.warning("Could not parse host-key store {} (starting empty; pins re-imported): {}", store_path, e)
        return HostKeyStoreState()


def _save_state(store_path: Path, state: HostKeyStoreState) -> None:
    atomic_write(store_path, state.model_dump_json(indent=2))


@pure
def render_pins_as_known_hosts_text(pins: Sequence[HostKeyPin]) -> str:
    """Render pins as sorted known_hosts lines (one per pin, trailing newline)."""
    pin_lines = sorted(f"{format_as_known_hosts_address(pin.address, pin.port)} {pin.public_key}" for pin in pins)
    return "".join(f"{line}\n" for line in pin_lines)


def _render_known_hosts(known_hosts_path: Path, state: HostKeyStoreState) -> None:
    """Atomically rewrite the known_hosts file from the store state."""
    all_pins = [pin for record in state.records for pin in record.pins]
    preserved_text = "".join(f"{line}\n" for line in state.preserved_lines)
    atomic_write(known_hosts_path, preserved_text + render_pins_as_known_hosts_text(all_pins))


@contextmanager
def _locked_store(known_hosts_path: Path) -> Iterator[None]:
    lock_path = known_hosts_path.with_name(known_hosts_path.name + _LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def _mutate_store(
    known_hosts_path: Path,
    mutate: Callable[[HostKeyStoreState], HostKeyStoreState],
) -> None:
    """Run one locked import -> mutate -> save -> render cycle on the store."""
    store_path = host_key_store_path(known_hosts_path)
    with _locked_store(known_hosts_path):
        loaded_state = _load_state(store_path)
        imported_state = _import_known_hosts_file(loaded_state, known_hosts_path)
        updated_state = mutate(imported_state)
        _save_state(store_path, updated_state)
        _render_known_hosts(known_hosts_path, updated_state)


def _validated_pin(hostname: str, port: int, public_key: str, origin: HostKeyOrigin) -> HostKeyPin:
    """Build a pin stamped with the current time, validating the OpenSSH public key format."""
    key_parts = public_key.split()
    if len(key_parts) < 2:
        raise MngrError(f"Malformed OpenSSH public key (expected '<type> <base64> [comment]'): {public_key!r}")
    return HostKeyPin(
        address=hostname,
        port=port,
        keytype=key_parts[0],
        public_key=public_key.strip(),
        origin=origin,
        updated_at=datetime.now(timezone.utc),
    )


def pin_host_key(
    known_hosts_path: Path,
    hostname: str,
    port: int,
    public_key: str,
    host_id: HostId | None,
    origin: HostKeyOrigin,
    is_add_if_absent: bool = False,
) -> None:
    """Pin ``public_key`` for ``hostname:port`` in the store and re-render the known_hosts file.

    Replace-by-(endpoint, keytype) with origin precedence; with
    ``is_add_if_absent`` an existing same-endpoint+keytype pin is left alone.
    Pins written without a ``host_id`` land on the shared unattributed record.
    """
    pin = _validated_pin(hostname, port, public_key, origin)
    record_id = str(host_id) if host_id is not None else UNATTRIBUTED_HOST_KEY_RECORD_ID
    _mutate_store(known_hosts_path, lambda state: _apply_pin(state, record_id, pin, is_add_if_absent))


def pin_sole_endpoint_host_key(
    known_hosts_path: Path,
    hostname: str,
    port: int,
    public_key: str,
    host_id: HostId,
    origin: HostKeyOrigin,
) -> None:
    """Pin ``public_key`` for ``hostname:port`` and drop every other endpoint's pins.

    For single-host known_hosts files whose endpoint moves across restarts
    (e.g. lima's reassigned forwarded port): the rendered file reflects only
    the current endpoint, with no stale entries from prior ports.
    """
    pin = _validated_pin(hostname, port, public_key, origin)
    _mutate_store(known_hosts_path, lambda state: _apply_sole_endpoint_pin(state, str(host_id), pin))


def clear_endpoint_pins(known_hosts_path: Path, hostname: str, port: int) -> None:
    """Drop every pin for ``hostname:port`` (all keytypes) and re-render the known_hosts file."""
    _mutate_store(known_hosts_path, lambda state: _clear_endpoint_from_state(state, hostname, port))


def remove_host_key_record(known_hosts_path: Path, host_id: HostId) -> None:
    """Forget every pin belonging to ``host_id`` and re-render the known_hosts file.

    The dead-endpoint GC for one host: called when the host is permanently
    deleted so its pins stop being emitted.
    """
    _mutate_store(known_hosts_path, lambda state: _keep_records_in_state(state, lambda rid: rid != str(host_id)))


def gc_dead_host_key_records(known_hosts_path: Path, live_host_ids: AbstractSet[HostId]) -> None:
    """Drop host-attributed records whose host is no longer live, and re-render.

    The unattributed record is kept: its pins cannot be tied to a host, so the
    store never claims to know they are dead.
    """
    live_ids = {str(live_id) for live_id in live_host_ids}
    _mutate_store(
        known_hosts_path,
        lambda state: _keep_records_in_state(
            state, lambda rid: rid == UNATTRIBUTED_HOST_KEY_RECORD_ID or rid in live_ids
        ),
    )


def render_known_hosts_file(known_hosts_path: Path) -> None:
    """Re-render the known_hosts file from its store (importing any out-of-band lines first)."""
    _mutate_store(known_hosts_path, lambda state: state)


def load_host_key_record(known_hosts_path: Path, host_id: HostId) -> HostKeyRecord | None:
    """Read one host's pin record from the store, or None when absent."""
    with _locked_store(known_hosts_path):
        state = _load_state(host_key_store_path(known_hosts_path))
    return _record_by_id(state, str(host_id))


def load_current_host_key_pins(known_hosts_path: Path) -> tuple[HostKeyPin, ...]:
    """Read every pin currently trusted for this known_hosts file, without writing anything.

    The store's records are the base; lines written to the known_hosts file
    behind the store's back (including a whole pre-store file with no sidecar)
    are folded in in-memory under the usual import rules, so the result is the
    same deduplicated per-(endpoint, keytype) pin set the next render would
    emit -- never the file's raw (possibly stale or duplicated) lines.
    """
    if not known_hosts_path.exists() and not host_key_store_path(known_hosts_path).exists():
        return ()
    with _locked_store(known_hosts_path):
        loaded_state = _load_state(host_key_store_path(known_hosts_path))
        imported_state = _import_known_hosts_file(loaded_state, known_hosts_path)
    return tuple(pin for record in imported_state.records for pin in record.pins)


def has_unpinned_bootstrap_drift(known_hosts_path: Path, known_hosts_text: str) -> bool:
    """Whether applying ``known_hosts_text`` would add trust the store still lacks.

    True when some parseable line's endpoint has no pin at all, or holds a
    *different* BOOTSTRAP-origin key (bootstrap material carries no deliberate
    trust decision, so the text's key was never knowingly superseded). A
    differing USER-origin pin does NOT count as drift: the user's own devices
    made a newer trust decision at that endpoint (e.g. a local rotation), and
    this deferral is what protects it -- re-applied text would land as freshly
    timestamped USER-origin pins, which origin precedence alone would accept.
    Unparseable lines are ignored, mirroring :func:`pin_known_hosts_text`.
    """
    now = datetime.now(timezone.utc)
    pin_by_endpoint = {_endpoint_key(pin): pin for pin in load_current_host_key_pins(known_hosts_path)}
    for raw_line in known_hosts_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parsed = _parse_pin_from_line(line, now, HostKeyOrigin.USER)
        if parsed is None:
            continue
        existing = pin_by_endpoint.get(_endpoint_key(parsed))
        if existing is None:
            return True
        if existing.public_key != parsed.public_key and existing.origin is HostKeyOrigin.BOOTSTRAP:
            return True
    return False


@pure
def _apply_pins_to_state(state: HostKeyStoreState, record_id: str, pins: Sequence[HostKeyPin]) -> HostKeyStoreState:
    updated_state = state
    for pin in pins:
        updated_state = _apply_pin(updated_state, record_id, pin, is_add_if_absent=False)
    return updated_state


def pin_known_hosts_text(
    known_hosts_path: Path,
    known_hosts_text: str,
    host_id: HostId | None,
    origin: HostKeyOrigin,
) -> None:
    """Parse known_hosts text into pins and apply each through the store, then re-render the file.

    One locked mutation: every parseable line becomes a pin of the given
    origin attributed to ``host_id`` (the unattributed record when None),
    applied with the usual replace-by-(endpoint, keytype) origin precedence.
    Unparseable lines are skipped with a warning -- unlike the out-of-band
    file import, imported text is never preserved verbatim.
    """
    now = datetime.now(timezone.utc)
    pins: list[HostKeyPin] = []
    for raw_line in known_hosts_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pin = _parse_pin_from_line(line, now, origin)
        if pin is None:
            logger.warning("Skipping an unparseable known_hosts line during import: {!r}", line)
            continue
        pins.append(pin)
    if not pins:
        return
    record_id = str(host_id) if host_id is not None else UNATTRIBUTED_HOST_KEY_RECORD_ID
    _mutate_store(known_hosts_path, lambda state: _apply_pins_to_state(state, record_id, pins))
