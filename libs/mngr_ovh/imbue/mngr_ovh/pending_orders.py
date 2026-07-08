"""On-disk markers for OVH orders whose delivery timed out, used by the reconcile sweep.

OVH's order pipeline is asynchronous: ``POST /order/cart/.../checkout`` returns
immediately with an ``orderId``, but the actual VPS ``serviceName`` is only
assigned during a separate delivery phase. ``mngr create`` waits up to
``instance_boot_timeout`` for that. When OVH is slow (busy region, new-account
fraud-review hold, etc.), the order's VPS arrives *after* the timeout and
mngr never gets a chance to tag it. Without intervention, the VPS leaks --
no ``mngr-provider`` IAM tag, invisible to ``list_vps_resources_for_provider``,
ignored by the recycle path.

This module owns the recovery mechanism: on
:class:`OvhOrderDeliveryTimeoutError`, the bake writes one marker file
per pending order into ``<profile_dir>/providers/ovh/<instance>/pending_orders/``.
Every subsequent ``mngr create`` against the same provider reads those
markers at the top of ``_provision_vps``, does a single short poll for
each order's delivery, and (if delivered) tags + cancels the VPS so the
recycle path's normal eligibility filter sees it as a candidate.
Markers are deleted on successful adoption; markers whose orders are
still pending stay around for the next reconcile.

The design is intentionally eventually-consistent rather than inline:
the failing bake exits at its normal timeout (no extra wait), and the
next bake's reconcile sweep picks up where it left off. Mirrors the
shape of ``minds.envs.recover``'s recover-target file pattern.
"""

import os
import time
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError

_PENDING_ORDERS_SUBDIR: Final[str] = "pending_orders"
_MARKER_FILENAME_FMT: Final[str] = "order-{order_id}.json"


class PendingOrderRecord(FrozenModel):
    """One on-disk marker representing an OVH order whose delivery timed out.

    Persisted as JSON under ``<provider_state_dir>/pending_orders/order-<id>.json``
    by :func:`write_pending_order_marker` and read back by
    :func:`read_pending_order_markers`. Carries everything the reconcile
    sweep needs to poll OVH for the VPS and -- if delivered -- attach
    the right tags + flip ``deleteAtExpiration``:

    - ``order_id`` -- the OVH order id (the checkout returned this).
    - ``plan_code`` -- needed to identify the VPS detail in the order
      among the bundled OS / install / backup line items.
    - ``region`` -- the OVH datacenter the order targeted. Carried for
      diagnostics; not required for the IAM-tag URN (URN region is
      derived from the OVH endpoint, not the VPS datacenter).
    - ``created_at_unix`` -- write time. The reconcile sweep skips
      markers older than a sanity bound (logging a warning) so a
      genuinely-dead order doesn't burn a poll on every bake forever.
    """

    order_id: int = Field(description="OVH order id, primary key for the marker file.")
    plan_code: str = Field(description="OVH plan code (e.g. ``vps-2025-model1``).")
    region: str = Field(description="OVH datacenter code the order targeted (e.g. ``US-WEST-OR``).")
    created_at_unix: float = Field(description="Wallclock time when the marker was written, for staleness checks.")


def pending_orders_dir(provider_state_dir: Path) -> Path:
    """Return the subdir under ``provider_state_dir`` that holds the marker files.

    Caller is expected to have ensured ``provider_state_dir`` exists.
    This function lazily mkdirs the ``pending_orders/`` subdir on first
    use so the directory doesn't appear on disk for providers that never
    hit a delivery timeout.
    """
    return provider_state_dir / _PENDING_ORDERS_SUBDIR


def write_pending_order_marker(
    provider_state_dir: Path,
    *,
    order_id: int,
    plan_code: str,
    region: str,
) -> Path:
    """Atomically write a marker for ``order_id`` and return its path.

    Idempotent: re-writing for the same ``order_id`` overwrites in place
    (newer ``created_at_unix``). Atomic via tmp-file + rename so a
    concurrent reader never sees a half-written file.

    Failures (disk full, permission denied) raise :class:`MngrError` --
    losing the marker means we'll silently leak the VPS the order
    eventually produces, which is worse than failing loudly.
    """
    directory = pending_orders_dir(provider_state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    record = PendingOrderRecord(
        order_id=order_id,
        plan_code=plan_code,
        region=region,
        created_at_unix=time.time(),
    )
    target = directory / _MARKER_FILENAME_FMT.format(order_id=order_id)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp_path.write_text(record.model_dump_json())
        os.replace(tmp_path, target)
    except OSError as exc:
        raise MngrError(f"Failed to write pending-order marker for OVH order {order_id} at {target}: {exc}") from exc
    logger.warning(
        "OVH order {} delivery timed out; wrote pending-order marker at {}. "
        "The next ``mngr create`` against this provider will poll OVH for the "
        "VPS and adopt it as a recycle candidate if it has delivered.",
        order_id,
        target,
    )
    return target


def read_pending_order_markers(provider_state_dir: Path) -> list[PendingOrderRecord]:
    """Return every parseable marker under the pending-orders dir.

    A marker that fails to parse (e.g. a half-written file from an
    older crash) is logged at WARNING and skipped, not raised --
    one corrupt marker shouldn't block the rest of the reconcile.
    Returns an empty list when the directory doesn't exist (i.e. no
    delivery timeout has ever fired against this provider).
    """
    directory = pending_orders_dir(provider_state_dir)
    if not directory.is_dir():
        return []
    records: list[PendingOrderRecord] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.endswith(".json"):
            continue
        try:
            records.append(PendingOrderRecord.model_validate_json(path.read_text()))
        except (OSError, ValueError) as exc:
            logger.warning("OVH pending-order marker {} is unreadable; skipping: {}", path, exc)
    return records


def delete_pending_order_marker(provider_state_dir: Path, *, order_id: int) -> None:
    """Idempotently remove the marker for ``order_id``.

    A missing file is treated as success (the reconcile may race with a
    parallel adoption that already cleaned up). Other ``OSError`` raises
    :class:`MngrError` -- a marker we couldn't delete means we'll
    re-poll the same already-adopted order on every subsequent bake,
    which is wasteful but not catastrophic; surfacing the error lets
    the operator notice.
    """
    target = pending_orders_dir(provider_state_dir) / _MARKER_FILENAME_FMT.format(order_id=order_id)
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MngrError(f"Failed to delete pending-order marker {target} for order {order_id}: {exc}") from exc
