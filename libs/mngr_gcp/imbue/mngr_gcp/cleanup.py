"""Out-of-band, age-based reaper for leaked GCP test GCE instances.

The session-end leak check in ``conftest.py`` only reaps instances when the
pytest session survives to run ``pytest_sessionfinish``. A session/runner
killed mid-run leaks instances that only the *next* GCP release session would
reap -- and the release suite is not run in CI, so that next run may be far
off. This module is the age-based reaper that the standalone CI job
(``scripts/cleanup_old_gcp_test_instances.py``, run on every push to main and
pull request) drives, mirroring Modal's ``cleanup_old_modal_test_environments``
and Vultr's ``cleanup_old_vultr_test_instances``.

``GcpVpsClient.create_instance`` labels every GCE instance launched while
``PYTEST_CURRENT_TEST`` is set with ``mngr-pytest-launched=true``. The reaper
matches on that label -- so production instances, which never carry it, are
never touched -- and keeps only those whose ``creation_timestamp`` is older
than a max age. The same ``find_old_test_instances`` / ``force_delete_instances``
functions back the ``conftest.py`` session-end check, so the two paths can
never drift.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from typing import Any

from google.api_core import exceptions as google_api_exceptions
from google.cloud import compute_v1
from loguru import logger

from imbue.mngr_gcp.client import GCP_PYTEST_LAUNCHED_LABEL


def find_old_test_instances(
    instances_client: Any, project: str, zone: str, max_age: timedelta, now: datetime
) -> list[str]:
    """Return names of ``mngr-pytest-launched=true`` GCE instances older than ``max_age``.

    Filters server-side on the pytest-launched label, then keeps only
    instances whose ``creation_timestamp`` is before ``now - max_age``. Younger
    instances are skipped so neither the session-end check nor the reaper ever
    race-kills an in-flight test on a parallel worker. An instance whose
    ``creation_timestamp`` is missing or unparseable is left alone -- we never
    delete an instance whose age we cannot establish (mirroring the AWS / Azure
    / Vultr reapers). A scan failure logs and yields an empty list rather than
    raising.
    """
    cutoff = now - max_age
    leaked: list[str] = []
    request = compute_v1.ListInstancesRequest(
        project=project, zone=zone, filter=f"labels.{GCP_PYTEST_LAUNCHED_LABEL}=true"
    )
    try:
        page_result = instances_client.list(request=request)
        for instance in page_result:
            try:
                created_at = datetime.fromisoformat(instance.creation_timestamp)
            except (ValueError, TypeError):
                logger.warning(
                    "Unparseable creation_timestamp {!r} on GCE instance {}; leaving instance alone",
                    instance.creation_timestamp,
                    instance.name,
                )
                continue
            if created_at < cutoff:
                leaked.append(instance.name)
    except google_api_exceptions.GoogleAPICallError as e:
        logger.warning("Failed to scan for orphaned GCE test instances: {}", e)
    return leaked


def force_delete_instances(instances_client: Any, project: str, zone: str, instance_names: Sequence[str]) -> None:
    """Best-effort delete of the given GCE instances; logs and swallows failures.

    Awaits each ``delete`` operation (like the production ``destroy_instance``
    path) so a server-side failure is caught here rather than silently dropped
    after a fire-and-forget call. Does not raise: a failed delete is logged so
    one stuck instance does not block deleting the rest.
    """
    for instance_name in instance_names:
        try:
            operation = instances_client.delete(project=project, zone=zone, instance=instance_name)
            operation.result()
        except google_api_exceptions.GoogleAPICallError as e:
            logger.warning("Failed to delete leaked GCE instance {}: {}", instance_name, e)


def cleanup_old_gcp_test_instances(
    instances_client: Any, project: str, zone: str, max_age: timedelta, now: datetime
) -> int:
    """Delete ``mngr-pytest-launched`` GCE instances older than ``max_age``; return the count.

    Never raises: scan failures yield an empty list and delete failures are
    logged and swallowed. The returned count is the number of instances
    targeted for deletion.
    """
    old = find_old_test_instances(instances_client, project, zone, max_age, now)
    if not old:
        logger.info("No leaked GCP test instances older than {} found", max_age)
        return 0
    logger.info("Found {} leaked GCP test instance(s) older than {}; deleting", len(old), max_age)
    force_delete_instances(instances_client, project, zone, old)
    return len(old)
