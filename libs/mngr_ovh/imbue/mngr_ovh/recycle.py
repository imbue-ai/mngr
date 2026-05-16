"""Recycle cancelled OVH VPSes on ``mngr create`` instead of ordering fresh.

OVH classic VPS billing is monthly and termination is an
email-confirmation-required, end-of-month event. Between
``destroy_host`` (which calls ``POST /vps/{s}/terminate``) and the actual
end-of-month decommission, the VPS keeps running and keeps being billed.
A new ``mngr create`` against the same provider during that window can
reuse one of these cancelled-but-still-alive VPSes for free instead of
ordering a fresh one for a new full month of billing.

This module owns:
- candidate selection (filter cancelled+tagged VPSes by plan/region/state/expiration)
- the cooperative IAM-tag lock that prevents two concurrent ``mngr create``s
  from clobbering each other on the same candidate
- un-cancelling the chosen VPS via the ``PUT /serviceInfos`` read-modify-write
- swapping the ``mngr-host-id`` IAM tag to point at the new host

The caller (``OvhProvider._provision_vps``) is responsible for the
shared post-selection steps (rebuild, TOFU pin, container setup).
"""

import time
import uuid
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.iam_tags import attach_tag
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import get_vps_resource
from imbue.mngr_ovh.iam_tags import list_vps_resources_for_provider
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.primitives import VpsInstanceId

_UNCANCEL_PROPAGATION_TIMEOUT_SECONDS: Final[float] = 30.0
_UNCANCEL_POLL_INTERVAL_SECONDS: Final[float] = 2.0


class _Candidate(FrozenModel):
    """A cancelled VPS that passed all the recycle eligibility filters."""

    service_name: str
    urn: str
    expiration: datetime
    plan_code: str
    region: str


def try_recycle_cancelled_vps(
    *,
    client: OvhVpsClient,
    provider_name: str,
    new_host_id: HostId,
    requested_plan: str,
    requested_region: str,
    safety_margin_hours: int,
    max_candidates: int,
) -> str | None:
    """Attempt to reuse a cancelled VPS for a new host. Returns its serviceName, or None.

    Side effects on success:
    1. The chosen VPS's ``renew.deleteAtExpiration`` is set to ``False``.
    2. Its old ``mngr-host-id`` IAM tag is replaced with ``new_host_id``.
    3. Its transient ``mngr-recycling-by`` lock tag is removed.

    Side effects on failure (any step after the lock is acquired):
    - Best-effort attempt to remove the ``mngr-recycling-by`` lock tag.
    - The VPS may have been un-cancelled but not fully recycled; the caller
      is responsible for either retrying or re-terminating it via
      ``OvhVpsClient.destroy_instance``.

    Returns ``None`` (with logs) for any of: no candidates, all candidates
    fail safety filters, lock acquisition lost a race, mid-recycle API
    error. In all those cases the caller should fall through to ordering
    a fresh VPS.
    """
    if client.is_unconfigured:
        return None

    candidates = _select_candidates(
        client=client,
        provider_name=provider_name,
        requested_plan=requested_plan,
        requested_region=requested_region,
        safety_margin_hours=safety_margin_hours,
        max_candidates=max_candidates,
    )
    if not candidates:
        logger.debug("OVH recycle: no eligible cancelled VPSes; will order fresh")
        return None

    lock_value = uuid.uuid4().hex
    for candidate in candidates:
        if _try_recycle_one(
            client=client,
            candidate=candidate,
            new_host_id=new_host_id,
            lock_value=lock_value,
        ):
            logger.info(
                "OVH recycle: reusing cancelled VPS {} (expires {}) for host {}",
                candidate.service_name,
                candidate.expiration.isoformat(),
                new_host_id,
            )
            return candidate.service_name
    logger.info("OVH recycle: all {} candidate(s) failed eligibility/lock; ordering fresh", len(candidates))
    return None


def _select_candidates(
    *,
    client: OvhVpsClient,
    provider_name: str,
    requested_plan: str,
    requested_region: str,
    safety_margin_hours: int,
    max_candidates: int,
) -> list[_Candidate]:
    """Apply the eligibility filters and return up to ``max_candidates`` candidates.

    Candidates are sorted by expiration *descending* (most billing left
    first) so the caller-selection loop tries the safest one first.
    """
    try:
        all_resources = list_vps_resources_for_provider(client, provider_name=provider_name)
    except (VpsApiError, MngrError) as e:
        logger.debug("OVH recycle: provider VPS listing failed ({}); falling through", e)
        return []
    if not all_resources:
        return []
    if len(all_resources) > max_candidates:
        logger.debug(
            "OVH recycle: provider has {} tagged VPSes, capping candidates at {}",
            len(all_resources),
            max_candidates,
        )
        all_resources = all_resources[:max_candidates]

    now = datetime.now(timezone.utc)
    threshold = now.timestamp() + safety_margin_hours * 3600
    candidates: list[_Candidate] = []
    for r in all_resources:
        candidate = _evaluate_candidate(
            client=client,
            resource_name=r.name,
            urn=r.urn,
            tags=dict(r.tags),
            requested_plan=requested_plan,
            requested_region=requested_region,
            now_threshold_unix=threshold,
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c.expiration, reverse=True)
    return candidates


def _evaluate_candidate(
    *,
    client: OvhVpsClient,
    resource_name: str,
    urn: str,
    tags: dict[str, str],
    requested_plan: str,
    requested_region: str,
    now_threshold_unix: float,
) -> _Candidate | None:
    """Return a ``_Candidate`` iff the VPS passes every eligibility filter."""
    if MNGR_RECYCLING_LOCK_TAG_KEY in tags:
        return None
    try:
        info = client.get_service_info(resource_name)
    except VpsApiError as e:
        logger.debug("OVH recycle: serviceInfos GET failed for {} ({}); skipping", resource_name, e)
        return None
    renew = info.get("renew") or {}
    if not bool(renew.get("deleteAtExpiration")):
        return None
    if info.get("status") != "ok":
        return None
    engaged = info.get("engagedUpTo")
    if engaged:
        return None
    expiration_raw = info.get("expiration")
    if not expiration_raw:
        return None
    try:
        expiration = _parse_ovh_date(str(expiration_raw))
    except ValueError as e:
        logger.debug("OVH recycle: cannot parse expiration={} for {} ({}); skipping", expiration_raw, resource_name, e)
        return None
    if expiration.timestamp() < now_threshold_unix:
        return None
    try:
        vps = client.get_instance(VpsInstanceId(resource_name))
    except VpsApiError as e:
        logger.debug("OVH recycle: /vps GET failed for {} ({}); skipping", resource_name, e)
        return None
    state = str(vps.get("state", ""))
    if state not in {"running", "stopped"}:
        return None
    plan_code = str((vps.get("model") or {}).get("name", ""))
    if plan_code != requested_plan:
        return None
    region = str(vps.get("zone", ""))
    # OVH zone strings embed the datacenter code in lowercase
    # (e.g. ``Region OpenStack: os-us-east-va-vps-1`` for the ``US-EAST-VA``
    # datacenter), so the substring match must be case-insensitive.
    if requested_region.lower() not in region.lower():
        return None
    return _Candidate(
        service_name=resource_name,
        urn=urn,
        expiration=expiration,
        plan_code=plan_code,
        region=region,
    )


def _try_recycle_one(
    *,
    client: OvhVpsClient,
    candidate: _Candidate,
    new_host_id: HostId,
    lock_value: str,
) -> bool:
    """Acquire the lock, un-cancel, replace identity tags. Returns True on success."""
    urn = candidate.urn
    try:
        attach_tag(client, urn, MNGR_RECYCLING_LOCK_TAG_KEY, lock_value)
    except (VpsApiError, MngrError) as e:
        logger.debug("OVH recycle: failed to acquire lock on {} ({}); skipping", candidate.service_name, e)
        return False
    try:
        if not _confirm_lock_held(client, urn, lock_value):
            logger.info("OVH recycle: lost lock race on {}; trying next candidate", candidate.service_name)
            return False
        try:
            client.set_renew_at_expiration(candidate.service_name, delete_at_expiration=False)
        except (VpsApiError, MngrError) as e:
            logger.warning("OVH recycle: un-cancel failed on {} ({}); aborting", candidate.service_name, e)
            return False
        if not _wait_for_uncancel(client, candidate.service_name):
            logger.warning("OVH recycle: un-cancel did not propagate on {}; aborting", candidate.service_name)
            return False
        _swap_host_id_tag(client, urn, new_host_id)
        return True
    finally:
        _release_lock(client, urn)


def _confirm_lock_held(client: OvhVpsClient, urn: str, lock_value: str) -> bool:
    """Re-read tags and verify our lock_value is the one set on this URN."""
    resource = get_vps_resource(client, urn)
    if resource is None:
        return False
    return resource.tags.get(MNGR_RECYCLING_LOCK_TAG_KEY) == lock_value


def _wait_for_uncancel(client: OvhVpsClient, service_name: str) -> bool:
    """Poll until ``serviceInfos.renew.deleteAtExpiration`` flips to False."""
    deadline = time.monotonic() + _UNCANCEL_PROPAGATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            info = client.get_service_info(service_name)
        except VpsApiError as e:
            logger.debug("OVH recycle: poll for un-cancel propagation failed ({}); retrying", e)
            time.sleep(_UNCANCEL_POLL_INTERVAL_SECONDS)
            continue
        if not bool((info.get("renew") or {}).get("deleteAtExpiration")):
            return True
        time.sleep(_UNCANCEL_POLL_INTERVAL_SECONDS)
    return False


def _swap_host_id_tag(client: OvhVpsClient, urn: str, new_host_id: HostId) -> None:
    """Replace the ``mngr-host-id`` IAM tag on a VPS in place.

    DELETE-then-POST: the old host_id is removed (404-tolerant in case the
    tag wasn't there) and the new one is attached. Brief window with no
    host-id tag is acceptable -- discovery uses ``mngr-provider`` as the
    primary filter, and we re-attach within milliseconds.
    """
    try:
        delete_tag(client, urn, MNGR_HOST_ID_TAG_KEY)
    except VpsApiError as e:
        if e.status_code != 404:
            raise
    attach_tag(client, urn, MNGR_HOST_ID_TAG_KEY, str(new_host_id))


def _release_lock(client: OvhVpsClient, urn: str) -> None:
    """Best-effort drop of the ``mngr-recycling-by`` lock tag."""
    try:
        delete_tag(client, urn, MNGR_RECYCLING_LOCK_TAG_KEY)
    except VpsApiError as e:
        if e.status_code != 404:
            logger.warning("OVH recycle: failed to release lock tag on {}: {}", urn, e)


def _parse_ovh_date(value: str) -> datetime:
    """Parse an OVH date/datetime string to a timezone-aware UTC datetime."""
    if "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        dt = datetime.strptime(value, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
