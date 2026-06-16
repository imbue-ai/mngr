"""Out-of-band, age-based reaper for leaked Vultr test VPS instances.

The per-session leak check in ``conftest.py`` only reaps instances when
the pytest session survives to run ``pytest_sessionfinish``. A
session/runner killed mid-run leaks instances that no *future* session
can match -- each session carries a fresh random ``mngr-vultr-test-session``
uuid, so a later run never recognizes an earlier run's orphans. To make
those reapable anyway, every test-created instance also carries a
``mngr-vultr-test-created=<YYYY-MM-DD-HH-MM-SS>`` (UTC) tag.

This module lists the account's instances, keeps those whose creation tag
is older than a max age, and destroys them -- independent of the session
uuid. The creation tag does double duty: its presence marks an instance
as test-created (so production VPSes, which never carry it, are never
touched), and its value gives the age. Driven by
``scripts/cleanup_old_vultr_test_instances.py`` (e.g. from CI on a
schedule). Mirrors Modal's ``cleanup_old_modal_test_environments``.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from typing import Protocol

from loguru import logger

from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.primitives import VpsInstanceId


class VultrReaperClient(Protocol):
    """The slice of ``VultrVpsClient`` the reaper uses: list + destroy.

    Typing the reaper against this narrow protocol (rather than the concrete
    client) keeps the dependency minimal and lets tests inject a lightweight
    fake instead of mocking the HTTP layer.
    """

    def list_instances(self, tag: str | None = None) -> list[dict[str, Any]]: ...

    def destroy_instance(self, instance_id: VpsInstanceId) -> None: ...


# Tag key marking a VPS as test-created and carrying its UTC creation time.
VULTR_TEST_CREATED_TAG_KEY: Final[str] = "mngr-vultr-test-created"
# Timestamp format embedded in the created tag. No colons -> safe as a Vultr
# tag string; mirrors Modal's TEST_ENV format so the parsing here matches
# what ``conftest.py`` writes via ``build_test_created_tag``.
VULTR_TEST_CREATED_TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d-%H-%M-%S"


def build_test_created_tag(now: datetime) -> str:
    """Build the ``mngr-vultr-test-created=<timestamp>`` tag for a VPS created at ``now`` (UTC)."""
    return f"{VULTR_TEST_CREATED_TAG_KEY}={now.strftime(VULTR_TEST_CREATED_TIMESTAMP_FORMAT)}"


def parse_test_created_at(tags: Sequence[str]) -> datetime | None:
    """Return the UTC creation time from an instance's test-created tag, or ``None``.

    ``None`` means either the instance is not test-created (no such tag) or
    its timestamp is unparseable. In both cases the reaper leaves the
    instance alone: we never destroy an instance whose age we cannot
    establish from the tag we control.
    """
    prefix = f"{VULTR_TEST_CREATED_TAG_KEY}="
    for tag in tags:
        if tag.startswith(prefix):
            raw = tag[len(prefix) :]
            try:
                return datetime.strptime(raw, VULTR_TEST_CREATED_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning("Unparseable Vultr test-created tag {!r}; leaving instance alone", tag)
                return None
    return None


def find_old_test_instances(
    instances: Sequence[dict[str, Any]], max_age: timedelta, now: datetime
) -> list[dict[str, Any]]:
    """Filter ``instances`` to test-created ones whose creation tag is older than ``max_age``."""
    cutoff = now - max_age
    old: list[dict[str, Any]] = []
    for instance in instances:
        created_at = parse_test_created_at(instance.get("tags", []))
        if created_at is not None and created_at < cutoff:
            old.append(instance)
    return old


def cleanup_old_vultr_test_instances(client: VultrReaperClient, max_age: timedelta, now: datetime) -> int:
    """Destroy test-created Vultr instances older than ``max_age``; return the count cleaned up.

    Never raises on an individual destroy failure: logs it and moves on so
    one stuck instance does not block reaping the rest (the next run retries
    it). A 404 means the instance is already gone -- it raced with another
    reaper or a test's own teardown -- and counts as cleaned. Any other
    failure leaves the instance live and is logged at error level so a stuck
    safety-net run is greppable in the cron/CI logs that drive this script.
    """
    old = find_old_test_instances(client.list_instances(), max_age, now)
    if not old:
        logger.info("No leaked Vultr test instances older than {} found", max_age)
        return 0
    logger.info("Found {} leaked Vultr test instance(s) older than {}; destroying", len(old), max_age)
    cleaned = 0
    for instance in old:
        instance_id = instance.get("id", "")
        label = instance.get("label", "")
        try:
            client.destroy_instance(VpsInstanceId(instance_id))
            logger.info("Destroyed leaked Vultr test instance {} (label={})", instance_id, label)
            cleaned += 1
        except VpsApiError as e:
            if e.status_code == 404:
                logger.debug("Leaked Vultr test instance {} already gone (404)", instance_id)
                cleaned += 1
            else:
                logger.error(
                    "Failed to destroy leaked Vultr test instance {}: {}; leaving it for the next run",
                    instance_id,
                    e,
                )
    return cleaned
