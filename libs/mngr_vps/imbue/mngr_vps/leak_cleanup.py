"""Shared, age-based reaper for leaked VPS test instances across providers.

Each provider's ``pytest_sessionfinish`` hook reaps survivors at session end, but a session
killed mid-run leaks instances that no in-process hook can reach. The standalone CI reaper
scripts use the shared logic here.

The reaper is keyed on two seams:

* a narrow ``VpsReaperClient`` Protocol (``list_instances`` + ``destroy_instance``) that
  every provider's concrete VPS client already satisfies;
* a per-provider ``CreatedAtExtractor`` that reads an instance's UTC creation time from
  whatever field that provider stamps it in. The extractor returns ``None`` for any
  instance that is not a reapable test instance, so the reaper never touches a production
  instance.

Failures are surfaced: ``list_instances`` errors propagate, and a non-404 destroy failure
raises ``VpsLeakCleanupError`` after every instance has been attempted.
"""

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Protocol

from loguru import logger

from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsError
from imbue.mngr_vps.primitives import VpsInstanceId

CreatedAtExtractor = Callable[[Mapping[str, Any]], "datetime | None"]


class VpsReaperClient(Protocol):
    """The slice of a concrete VPS client the reaper needs: list + destroy."""

    def list_instances(self) -> list[dict[str, Any]]: ...

    def destroy_instance(self, instance_id: VpsInstanceId) -> None: ...


class VpsLeakCleanupError(VpsError):
    """Raised by ``cleanup_old_test_instances`` when one or more destroys failed (non-404)."""

    def __init__(self, failed_instance_ids: Sequence[str]) -> None:
        self.failed_instance_ids = tuple(failed_instance_ids)
        super().__init__(
            f"Failed to destroy {len(self.failed_instance_ids)} leaked test instance(s): {failed_instance_ids}"
        )


def parse_tag_value(tags: Sequence[str], key: str) -> str | None:
    """Return the value of the ``"<key>=<value>"`` entry in ``tags``, or ``None`` if absent."""
    prefix = f"{key}="
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def parse_iso_utc(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime, or ``None`` if unparseable/absent."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Unparseable ISO creation timestamp {!r}; leaving instance alone", raw)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_strptime_utc(raw: str | None, timestamp_format: str) -> datetime | None:
    """Parse a ``strptime``-format (always-UTC) timestamp into a tz-aware UTC datetime, or ``None``."""
    if raw is None:
        return None
    try:
        return datetime.strptime(raw, timestamp_format).replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning(
            "Unparseable creation timestamp {!r} (format {!r}); leaving instance alone", raw, timestamp_format
        )
        return None


def has_launched_marker(instance: Mapping[str, Any], marker_tag: str) -> bool:
    """Return True iff the instance carries the ``"<marker_tag>=true"`` pytest-launched marker."""
    return f"{marker_tag}=true" in instance.get("tags", ())


def find_old_test_instances(
    instances: Sequence[Mapping[str, Any]],
    created_at_of: CreatedAtExtractor,
    max_age: timedelta,
    now: datetime,
) -> list[dict[str, Any]]:
    """Filter ``instances`` to reapable test instances whose creation time is older than ``max_age``.

    An instance is kept iff ``created_at_of`` returns a (non-``None``) creation time strictly
    older than ``now - max_age``. Younger instances are skipped so neither the session-end check
    nor the reaper ever race-kills an in-flight test on a parallel worker.
    """
    cutoff = now - max_age
    old: list[dict[str, Any]] = []
    for instance in instances:
        created_at = created_at_of(instance)
        if created_at is not None and created_at < cutoff:
            old.append(dict(instance))
    return old


def destroy_leaked_instances(client: VpsReaperClient, instances: Sequence[Mapping[str, Any]]) -> list[str]:
    """Best-effort destroy each instance; return the ids whose destroy failed (non-404)."""
    failed: list[str] = []
    for instance in instances:
        instance_id = instance.get("id", "")
        try:
            client.destroy_instance(VpsInstanceId(instance_id))
            logger.info("Destroyed leaked test instance {}", instance_id)
        except VpsApiError as e:
            if e.status_code == 404:
                logger.debug("Leaked test instance {} already gone (404)", instance_id)
            else:
                logger.error("Failed to destroy leaked test instance {}: {}", instance_id, e)
                failed.append(instance_id)
    return failed


def cleanup_old_test_instances(
    client: VpsReaperClient,
    created_at_of: CreatedAtExtractor,
    max_age: timedelta,
    now: datetime,
) -> int:
    """Destroy test instances older than ``max_age``; return the count cleaned up."""
    old = find_old_test_instances(client.list_instances(), created_at_of, max_age, now)
    if not old:
        logger.info("No leaked test instances older than {} found", max_age)
        return 0
    logger.info("Found {} leaked test instance(s) older than {}; destroying", len(old), max_age)
    failed = destroy_leaked_instances(client, old)
    if failed:
        raise VpsLeakCleanupError(failed)
    return len(old)
