"""Shared, age-based reaper for leaked VPS test instances across providers.

Every VPS-family provider (``mngr_aws``, ``mngr_gcp``, ``mngr_azure``, ``mngr_vultr``, ...)
creates real cloud instances in its ``@pytest.mark.release`` tests. Each test destroys its
own instance in a ``finally`` block, and each provider's ``pytest_sessionfinish`` hook reaps
any survivor at session end -- but a session/runner killed mid-run leaks instances that no
in-process hook can reach, and the release suites are not run in CI, so the next reaping pass
may be far off. The standalone CI reaper scripts (``scripts/cleanup_old_<provider>_test_instances.py``,
run on every push to main and pull request) close that gap.

The scan + destroy logic is identical across providers and lives here, keyed on two seams:

* a narrow ``VpsReaperClient`` Protocol (``list_instances`` + ``destroy_instance``) that every
  provider's concrete VPS client already satisfies -- ``destroy_instance`` is on the shared
  ``VpsClientInterface``; ``list_instances`` is a concrete method returning the normalized
  ``{"id", "tags", ...}`` dicts every provider produces; and
* a per-provider ``CreatedAtExtractor`` that reads an instance's UTC creation time from
  whatever field that provider stamps it in (an ``mngr-created-at`` tag for AWS/Azure, instance
  metadata for GCP, an ``mngr-vultr-test-created`` tag for Vultr). The extractor returns ``None``
  for any instance that is not a reapable test instance (no test marker, or an age that cannot
  be established), so the reaper never destroys an instance whose age it cannot prove -- and a
  production instance, which carries no test marker, is therefore never touched.

Failures are surfaced, not swallowed: ``list_instances`` errors propagate (a reaper that cannot
scan must not report "nothing leaked"), and a non-404 destroy failure raises
``VpsLeakCleanupError`` after every instance has been attempted (one stuck instance does not
block reaping the rest, but the run still fails loudly). A 404 on destroy means the instance
disappeared between the scan and the destroy (a race with a test's own teardown) and counts as
cleaned.
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

# Reads the UTC creation time of a ``list_instances()`` dict, or ``None`` when the instance is
# not a reapable test instance (no test marker) or its age cannot be established. ``None`` always
# means "leave this instance alone".
CreatedAtExtractor = Callable[[Mapping[str, Any]], "datetime | None"]


class VpsReaperClient(Protocol):
    """The slice of a concrete VPS client the reaper needs: list + destroy.

    Typing the reaper against this narrow Protocol (rather than a concrete client) keeps the
    dependency minimal, lets tests inject a lightweight fake, and works for every provider whose
    client exposes ``list_instances`` (concrete, normalized dicts) and ``destroy_instance`` (on
    the shared ``VpsClientInterface``).
    """

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]: ...

    def destroy_instance(self, instance_id: VpsInstanceId) -> None: ...


class VpsLeakCleanupError(VpsError):
    """Raised by ``cleanup_old_test_instances`` when one or more destroys failed (non-404).

    Carries the failed instance ids so the standalone reaper script exits non-zero -- a leak the
    reaper could not clean up must turn the CI job red, not pass silently.
    """

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
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime, or ``None`` if unparseable/absent.

    A naive timestamp (no offset) is assumed UTC. Used for the providers whose creation marker is
    written via ``datetime.now(timezone.utc).isoformat()`` (AWS/Azure tags, GCP metadata).
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Unparseable ISO creation timestamp {!r}; leaving instance alone", raw)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_strptime_utc(raw: str | None, timestamp_format: str) -> datetime | None:
    """Parse a ``strptime``-format (always-UTC) timestamp into a tz-aware UTC datetime, or ``None``.

    Used for providers whose creation marker is a colon-free, fixed-format string (e.g. Vultr's
    ``mngr-vultr-test-created=%Y-%m-%d-%H-%M-%S``), since colons are unsafe in some tag systems.
    """
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
    """Return True iff the instance carries the ``"<marker_tag>=true"`` pytest-launched marker.

    Each provider's ``CreatedAtExtractor`` calls this first so a production instance -- which never
    carries the marker -- is never considered reapable, however old it is.
    """
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
    """Best-effort destroy each instance; return the ids whose destroy failed (non-404).

    Does not raise: every instance is attempted so one stuck instance does not block the rest. A
    404 means the instance already disappeared (a race with a test's own teardown) and is treated
    as cleaned. Any other ``VpsApiError`` is logged at error level and its id returned, so the
    caller can apply its own policy (the reaper script raises; the session-end hook fails the
    session). Callers decide whether finding instances at all is itself a failure.
    """
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
    """Destroy test instances older than ``max_age``; return the count cleaned up.

    The standalone CI reaper entry point. ``list_instances`` failures propagate (a reaper that
    cannot scan must not report success). Destroy failures are collected and, if any non-404
    failure occurred, raised as ``VpsLeakCleanupError`` after every instance has been attempted,
    so the CI job goes red rather than green-with-a-leak.
    """
    old = find_old_test_instances(client.list_instances(), created_at_of, max_age, now)
    if not old:
        logger.info("No leaked test instances older than {} found", max_age)
        return 0
    logger.info("Found {} leaked test instance(s) older than {}; destroying", len(old), max_age)
    failed = destroy_leaked_instances(client, old)
    if failed:
        raise VpsLeakCleanupError(failed)
    return len(old)
