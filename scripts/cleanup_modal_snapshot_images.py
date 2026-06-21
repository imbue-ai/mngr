#!/usr/bin/env python3
"""Cleanup for the minds-snapshot Modal images built by
``scripts/snapshot_minds_e2e_state.py``.

``sandbox.snapshot_filesystem()`` images persist until explicitly deleted,
and Modal exposes no list-images API. So this script keeps a durable ledger
of built image ids in a Modal ``Dict`` (``image_id -> created-at unix
timestamp``) and uses Modal's ``ImageDelete`` RPC to remove them. Three
modes, matching the CI lifecycle:

- ``record <image_id>``: add a freshly-built image to the ledger. Run by the
  ``build-minds-snapshot`` CI job right after the build, so every built image
  is tracked even if the run never reaches the delete step.
- ``delete <image_id>``: delete the image and drop it from the ledger. Run by
  the ``test-minds-snapshot`` CI job on success -- the steady-state path that
  keeps things clean after every green run.
- ``sweep --max-age-hours H``: delete (and drop) every ledger entry older than
  H hours. Run by the periodic ``cleanup-modal-environments`` CI job as the
  safety net for leaked images whose run never reached the delete step. Mirrors
  the name+age sweep that ``cleanup_old_modal_test_environments.py`` does for
  Modal test environments.

Usage:
    uv run python scripts/cleanup_modal_snapshot_images.py record im-01...
    uv run python scripts/cleanup_modal_snapshot_images.py delete im-01...
    uv run python scripts/cleanup_modal_snapshot_images.py sweep --max-age-hours 1.0

Requires Modal credentials (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET, or an active
Modal profile). The ledger Dict and the images live in whatever Modal
environment those credentials resolve to, so record/delete/sweep must all run
with the same credentials -- which they do in CI.
"""

import argparse
import asyncio
import time
from collections.abc import Sequence
from typing import Final

import modal
import modal.exception
import modal_proto.api_pb2 as api_pb2
from loguru import logger
from modal.client import _Client

from imbue.imbue_common.logging import setup_logging

# Modal Dict used as the durable ledger of built snapshot image ids. Without
# it a leaked image would be undiscoverable (no list-images API).
_LEDGER_DICT_NAME: Final[str] = "minds-snapshot-image-ledger"
_SECONDS_PER_HOUR: Final[float] = 3600.0


def _get_ledger() -> modal.Dict:
    return modal.Dict.from_name(_LEDGER_DICT_NAME, create_if_missing=True)


async def _delete_images_by_id(image_ids: Sequence[str]) -> set[str]:
    """Delete each image via Modal's ImageDelete RPC, sharing one client.

    Returns the ids that were actually deleted. An id that is already gone
    (NotFoundError) or malformed (InvalidError) is omitted from the returned
    set, but the caller still drops it from the ledger -- neither case is a
    real image we can reclaim, and a bad ledger entry must not wedge the
    sweep for every later id.
    """
    client = await _Client.from_env()
    deleted_image_ids: set[str] = set()
    for image_id in image_ids:
        try:
            await client.stub.ImageDelete(api_pb2.ImageDeleteRequest(image_id=image_id))
        except modal.exception.NotFoundError:
            logger.debug("Image {} was already gone; nothing to delete", image_id)
            continue
        except modal.exception.InvalidError:
            logger.warning("Ledger entry {!r} is not a valid image id; dropping it", image_id)
            continue
        deleted_image_ids.add(image_id)
    return deleted_image_ids


def _record_image(image_id: str) -> None:
    ledger = _get_ledger()
    created_at = time.time()
    ledger[image_id] = created_at
    logger.info("Recorded snapshot image {} in the cleanup ledger", image_id)


def _delete_image(image_id: str) -> None:
    deleted_image_ids = asyncio.run(_delete_images_by_id([image_id]))
    ledger = _get_ledger()
    ledger.pop(image_id, None)
    if image_id in deleted_image_ids:
        logger.info("Deleted snapshot image {} and dropped it from the ledger", image_id)
    else:
        logger.info("Snapshot image {} was already gone; dropped its stale ledger entry", image_id)


def _sweep_images(max_age_hours: float) -> int:
    ledger = _get_ledger()
    max_age_seconds = max_age_hours * _SECONDS_PER_HOUR
    now = time.time()

    # Collect everything past the age threshold first, then delete in one
    # shared-client batch and drop all of them from the ledger.
    expired_image_ids = tuple(
        image_id for image_id, created_at in ledger.items() if (now - created_at) > max_age_seconds
    )
    if not expired_image_ids:
        logger.info("No snapshot images older than {} hours to sweep", max_age_hours)
        return 0

    logger.info("Sweeping {} snapshot image(s) older than {} hours", len(expired_image_ids), max_age_hours)
    asyncio.run(_delete_images_by_id(expired_image_ids))
    for image_id in expired_image_ids:
        ledger.pop(image_id, None)
    return len(expired_image_ids)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record, delete, or sweep minds-snapshot Modal images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="Add a built image id to the cleanup ledger.")
    record_parser.add_argument("image_id", help="The Modal image id (im-...) to record.")

    delete_parser = subparsers.add_parser("delete", help="Delete an image and drop it from the ledger.")
    delete_parser.add_argument("image_id", help="The Modal image id (im-...) to delete.")

    sweep_parser = subparsers.add_parser("sweep", help="Delete every ledger image older than --max-age-hours.")
    sweep_parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for images to keep (default: 1.0).",
    )
    return parser.parse_args()


def main() -> None:
    setup_logging(level="INFO")
    args = _parse_args()
    if args.command == "record":
        _record_image(args.image_id)
    elif args.command == "delete":
        _delete_image(args.image_id)
    elif args.command == "sweep":
        _sweep_images(args.max_age_hours)
    else:
        # argparse's required=True guarantees one of the above, so this is
        # unreachable; raise rather than silently no-op if that ever changes.
        raise ValueError(f"Unknown command: {args.command!r}")


if __name__ == "__main__":
    main()
