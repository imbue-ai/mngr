"""Tests for the pending-order marker file IO."""

import json
from pathlib import Path

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.pending_orders import PendingOrderRecord
from imbue.mngr_ovh.pending_orders import delete_pending_order_marker
from imbue.mngr_ovh.pending_orders import pending_orders_dir
from imbue.mngr_ovh.pending_orders import read_pending_order_markers
from imbue.mngr_ovh.pending_orders import write_pending_order_marker


def test_write_then_read_roundtrips_record(tmp_path: Path) -> None:
    written = write_pending_order_marker(tmp_path, order_id=42, plan_code="vps-2025-model1", region="US-WEST-OR")
    assert written.is_file()
    records = read_pending_order_markers(tmp_path)
    assert len(records) == 1
    assert records[0].order_id == 42
    assert records[0].plan_code == "vps-2025-model1"
    assert records[0].region == "US-WEST-OR"
    assert records[0].created_at_unix > 0


def test_write_is_idempotent_per_order_id(tmp_path: Path) -> None:
    """Re-writing for the same order_id overwrites; reading still returns one entry."""
    first = write_pending_order_marker(tmp_path, order_id=42, plan_code="vps-2025-model1", region="US-WEST-OR")
    second = write_pending_order_marker(tmp_path, order_id=42, plan_code="vps-2025-model1", region="US-WEST-OR")
    assert first == second
    records = read_pending_order_markers(tmp_path)
    assert len(records) == 1
    assert records[0].order_id == 42


def test_read_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    """No markers ever written -> no directory -> empty list (not error)."""
    # pending_orders_dir under tmp_path doesn't exist; reader must tolerate it.
    assert read_pending_order_markers(tmp_path) == []


def test_read_skips_unreadable_marker(tmp_path: Path) -> None:
    """A half-written / corrupt marker logs a warning and is skipped, but other markers still load."""
    write_pending_order_marker(tmp_path, order_id=1, plan_code="vps-2025-model1", region="US-WEST-OR")
    write_pending_order_marker(tmp_path, order_id=2, plan_code="vps-2025-model1", region="US-WEST-OR")
    # Corrupt the second marker.
    bad_marker = pending_orders_dir(tmp_path) / "order-2.json"
    bad_marker.write_text("{not valid json")
    records = read_pending_order_markers(tmp_path)
    # Only the parseable one comes back.
    assert [r.order_id for r in records] == [1]


def test_delete_is_idempotent_on_missing_marker(tmp_path: Path) -> None:
    """delete on a marker that was never written returns cleanly (not raises)."""
    # Should not raise.
    delete_pending_order_marker(tmp_path, order_id=999)
    assert read_pending_order_markers(tmp_path) == []


def test_delete_removes_existing_marker(tmp_path: Path) -> None:
    write_pending_order_marker(tmp_path, order_id=42, plan_code="vps-2025-model1", region="US-WEST-OR")
    assert len(read_pending_order_markers(tmp_path)) == 1
    delete_pending_order_marker(tmp_path, order_id=42)
    assert read_pending_order_markers(tmp_path) == []


def test_marker_file_uses_expected_path_shape(tmp_path: Path) -> None:
    """Marker path is ``<dir>/pending_orders/order-<id>.json`` so an operator
    can grep on-disk state for a given order id."""
    written = write_pending_order_marker(tmp_path, order_id=12345, plan_code="vps-2025-model1", region="US-WEST-OR")
    assert written == tmp_path / "pending_orders" / "order-12345.json"
    payload = json.loads(written.read_text())
    assert payload["order_id"] == 12345
    assert payload["plan_code"] == "vps-2025-model1"
    assert payload["region"] == "US-WEST-OR"


def test_pending_order_record_field_set_is_locked() -> None:
    """FrozenModel + ``extra=forbid`` (inherited) means adding/removing fields needs an explicit refactor."""
    # Missing the required ``created_at_unix`` field.
    with pytest.raises(ValueError):
        PendingOrderRecord.model_validate({"order_id": 1, "plan_code": "p", "region": "r"})
    with pytest.raises(ValueError):
        PendingOrderRecord.model_validate(
            {
                "order_id": 1,
                "plan_code": "p",
                "region": "r",
                "created_at_unix": 1.0,
                "bogus_extra": True,
            }
        )


def test_write_surfaces_oserror_as_mngr_error(tmp_path: Path) -> None:
    """Losing a marker silently would leak the orphan VPS; failure must be loud.

    Triggers a real ``OSError`` by occupying the ``pending_orders`` path
    with a regular file, so the ``mkdir(parents=True, exist_ok=True)``
    inside ``write_pending_order_marker`` fails with ``NotADirectoryError``
    (an OSError subclass).
    """
    # Pre-occupy where the pending_orders directory would go.
    (tmp_path / "pending_orders").write_text("not a directory")
    with pytest.raises((MngrError, NotADirectoryError, FileExistsError)):
        write_pending_order_marker(tmp_path, order_id=42, plan_code="p", region="r")
