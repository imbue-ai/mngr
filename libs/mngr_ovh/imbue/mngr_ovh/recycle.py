"""Recycle cancelled OVH VPSes on ``mngr create`` instead of ordering fresh.

OVH classic VPS billing is monthly and OVH does not prorate cancellations.
``mngr destroy`` cancels via ``PUT /vps/{s}/serviceInfos``
(``renew.deleteAtExpiration=true``); between that flag flip and the
OVH-side ``expiration`` date the VPS keeps running and keeps being
billed. A new ``mngr create`` against the same provider during that
window can reuse one of these cancelled-but-still-alive VPSes for free
instead of ordering a fresh one for a new full month of billing.

This module owns:
- candidate selection (filter cancelled+tagged VPSes by plan/region/state/expiration)
- the cooperative IAM-tag lock that prevents two concurrent ``mngr create``s
  from clobbering each other on the same candidate
- un-cancelling the chosen VPS via the ``PUT /serviceInfos`` read-modify-write
- swapping the ``mngr-host-id`` IAM tag to point at the new host and
  (over)writing the new owner's extra IAM tags (e.g. ``minds_env``) so a
  cross-env recycle reflects the env that now owns the VPS

The caller (``OvhProvider._provision_vps``) is responsible for the
shared post-selection steps (rebuild, TOFU pin, container setup).

**Recycle eligibility requires the ``mngr-provider`` IAM tag.** Candidate
selection runs through :func:`list_vps_resources_for_provider`, which
filters to VPSes whose ``mngr-provider`` tag matches the running
provider instance's name. So a VPS that was ordered by mngr but whose
provisioning aborted *before* the post-delivery tag attach (e.g. an OVH
order that didn't deliver before ``instance_boot_timeout`` elapsed) is
**invisible** to the recycle path and would leak indefinitely on its
own.

The recovery mechanism for that case is the pending-order marker
pattern in ``pending_orders.py`` + ``OvhProvider._reconcile_pending_orders``:
``_provision_vps`` writes a marker on
:class:`OvhOrderDeliveryTimeoutError`, and every subsequent
``mngr create`` polls each marker's order once before its own
provisioning. Any newly-delivered VPS gets tagged + cancelled in that
sweep, becomes a recycle candidate, and is claimed by the very next
``_maybe_claim_recycled_vps`` call -- or by a later bake's call if
delivery is still pending now.
"""

import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.client import RecycleHandle
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.iam_tags import attach_tag
from imbue.mngr_ovh.iam_tags import attach_tags
from imbue.mngr_ovh.iam_tags import delete_tag
from imbue.mngr_ovh.iam_tags import get_vps_resource
from imbue.mngr_ovh.iam_tags import list_vps_resources_for_provider
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.primitives import VpsInstanceId

_UNCANCEL_PROPAGATION_TIMEOUT_SECONDS: Final[float] = 30.0
_UNCANCEL_POLL_INTERVAL_SECONDS: Final[float] = 2.0


class _Candidate(FrozenModel):
    """A cancelled VPS that passed all the recycle eligibility filters."""

    service_name: str
    urn: str
    expiration: datetime


def try_recycle_cancelled_vps(
    *,
    client: OvhVpsClient,
    provider_name: str,
    new_host_id: HostId,
    requested_plan: str,
    requested_region: str,
    safety_margin_hours: int,
    max_candidates: int,
    # Owner/env IAM tags (e.g. ``minds_env=staging``) to (over)write onto the
    # recycled VPS so it reflects the *current* bake's owner, matching what a
    # fresh order would attach. Reserved keys are already rejected upstream by
    # ``parse_extra_tags_env``.
    extra_tags: Mapping[str, str],
) -> RecycleHandle | None:
    """Lock a cancelled VPS and re-tag it for ``new_host_id``. Returns a handle, or None.

    Side effects on success:
    1. The chosen VPS gets a transient ``mngr-recycling-by`` IAM lock tag.
    2. Its old ``mngr-host-id`` IAM tag is replaced with ``new_host_id``.
    3. Each ``extra_tags`` pair is attached (overwriting any stale value),
       so a VPS recycled across envs ends up owned by the new env rather
       than still advertising the previous owner's ``minds_env`` tag.
    4. ``deleteAtExpiration`` is **NOT** flipped here; the caller flips it
       via ``finalize_recycle`` once the rebuild + container setup + host
       record write have all succeeded.

    Side effects on failure (any step after the lock is acquired):
    - Best-effort attempt to remove the ``mngr-recycling-by`` lock tag.
    - The VPS stays cancelled (``deleteAtExpiration=True``).

    Returns ``None`` for any of: no candidates, all candidates failed
    safety filters, lock acquisition lost a race, mid-recycle API error.
    In all those cases the caller should fall through to ordering fresh.

    Eligibility filter: candidates are sourced via
    :func:`list_vps_resources_for_provider`, which only returns VPSes
    whose ``mngr-provider`` IAM tag matches ``provider_name``. A VPS that
    mngr ordered but failed to tag (e.g. an order whose delivery timed
    out before ``_provision_vps``'s tag-immediately-on-first-sight step
    ran) is invisible here. ``OvhProvider._reconcile_pending_orders``
    (driven by the pending-order markers in ``pending_orders.py``) is
    what attaches that tag retroactively so the orphan becomes a
    recycle candidate.
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
        handle = _try_recycle_one(
            client=client,
            candidate=candidate,
            new_host_id=new_host_id,
            lock_value=lock_value,
            extra_tags=extra_tags,
        )
        if handle is not None:
            logger.info(
                "OVH recycle: claimed cancelled VPS {} (expires {}) for host {}",
                candidate.service_name,
                candidate.expiration.isoformat(),
                new_host_id,
            )
            client.register_recycle_handle(handle)
            return handle
    logger.info("OVH recycle: all {} candidate(s) failed eligibility/lock; ordering fresh", len(candidates))
    return None


def finalize_recycle(client: OvhVpsClient, handle: RecycleHandle) -> bool:
    """Commit the recycle: flip ``deleteAtExpiration=False`` and release the lock.

    Called *after* the host has been fully provisioned (container running,
    host record written) so the VPS only becomes "un-cancelled" once we
    know it'll actually be useful. Returns True if the un-cancel API call
    + propagation poll both succeeded.

    On failure (API error / propagation timeout): the VPS stays cancelled
    and the host record points at a VPS that will auto-decommission at end
    of month. We log loudly so an operator can manually flip the flag if
    the host is meant to be long-lived. Lock release is best-effort either
    way; the lock has no TTL on OVH's side, so leaking it would block
    future recycle attempts of this VPS.
    """
    client.discard_recycle_handle(handle.service_name)
    try:
        client.set_renew_at_expiration(handle.service_name, delete_at_expiration=False)
    except MngrError as e:
        logger.error(
            "OVH recycle: un-cancel of {} failed at finalize ({}); VPS will auto-decommission at end of month",
            handle.service_name,
            e,
        )
        _release_lock(client, handle.urn, handle.lock_value)
        return False
    if not _wait_for_uncancel(client, handle.service_name):
        logger.error(
            "OVH recycle: un-cancel of {} did not propagate at finalize; VPS may auto-decommission at end of month",
            handle.service_name,
        )
        _release_lock(client, handle.urn, handle.lock_value)
        return False
    _release_lock(client, handle.urn, handle.lock_value)
    logger.info("OVH recycle: finalized recycle of {} (un-cancelled, lock released)", handle.service_name)
    return True


def abort_recycle(client: OvhVpsClient, handle: RecycleHandle) -> None:
    """Release the recycle lock without un-cancelling.

    Used on any failure between claim and finalize. The VPS stays
    cancelled and will auto-decommission at end of month, so a partial
    recycle does not leak a still-billing orphan -- the very property the
    deferred-un-cancel design buys us.
    """
    client.discard_recycle_handle(handle.service_name)
    _release_lock(client, handle.urn, handle.lock_value)
    logger.info("OVH recycle: aborted recycle of {} (lock released; VPS stays cancelled)", handle.service_name)


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
    except MngrError as e:
        # Surface as WARNING (not DEBUG): every ``mngr create`` will silently
        # fall back to ordering a fresh VPS if discovery is broken, which is
        # surprising and expensive (OVH bills monthly per VPS). Operators
        # should see this in normal log output.
        logger.warning("OVH recycle: provider VPS listing failed ({}); falling through to fresh order", e)
        return []
    if not all_resources:
        return []

    now = datetime.now(timezone.utc)
    threshold = now.timestamp() + safety_margin_hours * 3600
    candidates: list[_Candidate] = []
    fetched = 0
    for r in all_resources:
        if fetched >= max_candidates:
            logger.debug(
                "OVH recycle: hit max_candidates={} after evaluating {} VPSes; "
                "stopping selection (eligible candidates so far: {})",
                max_candidates,
                fetched,
                len(candidates),
            )
            break
        fetched += 1
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
    if str((vps.get("model") or {}).get("name", "")) != requested_plan:
        return None
    # OVH zone strings embed the datacenter code in lowercase
    # (e.g. ``Region OpenStack: os-us-east-va-vps-1`` for the ``US-EAST-VA``
    # datacenter), so the substring match must be case-insensitive.
    if requested_region.lower() not in str(vps.get("zone", "")).lower():
        return None
    return _Candidate(service_name=resource_name, urn=urn, expiration=expiration)


def _try_recycle_one(
    *,
    client: OvhVpsClient,
    candidate: _Candidate,
    new_host_id: HostId,
    lock_value: str,
    extra_tags: Mapping[str, str],
) -> RecycleHandle | None:
    """Lock + re-tag a candidate. Returns a handle on success, ``None`` on failure.

    Re-tags the VPS for the new owner: swaps ``mngr-host-id`` and
    (over)writes ``extra_tags`` (e.g. ``minds_env``) so a VPS recycled from
    one env to another no longer advertises the previous owner's tags.

    Does **not** un-cancel: that step is deferred to ``finalize_recycle``,
    called by the caller after the host record has been written, so a
    failure between here and the host-record write leaves the VPS still
    cancelled (and therefore harmless: it auto-decommissions at the next
    billing boundary).

    The lock is intentionally **not** released on a successful return.
    Ownership of the lock transfers to the caller via the returned
    ``RecycleHandle`` until either ``finalize_recycle`` or
    ``abort_recycle`` is called.
    """
    urn = candidate.urn
    try:
        attach_tag(client, urn, MNGR_RECYCLING_LOCK_TAG_KEY, lock_value)
    except MngrError as e:
        logger.debug("OVH recycle: failed to acquire lock on {} ({}); skipping", candidate.service_name, e)
        return None
    if not _confirm_lock_held(client, urn, lock_value):
        logger.info("OVH recycle: lost lock race on {}; trying next candidate", candidate.service_name)
        _release_lock(client, urn, lock_value)
        return None
    try:
        _swap_host_id_tag(client, urn, new_host_id)
    except MngrError as e:
        logger.warning("OVH recycle: host-id tag swap failed on {} ({}); aborting", candidate.service_name, e)
        _release_lock(client, urn, lock_value)
        return None
    try:
        attach_tags(client, urn, extra_tags)
    except MngrError as e:
        logger.warning("OVH recycle: extra-tag attach failed on {} ({}); aborting", candidate.service_name, e)
        _release_lock(client, urn, lock_value)
        return None
    return RecycleHandle(urn=urn, service_name=candidate.service_name, lock_value=lock_value)


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


def _release_lock(client: OvhVpsClient, urn: str, lock_value: str) -> None:
    """Best-effort drop of the ``mngr-recycling-by`` lock tag *iff we still own it*.

    Re-reads the tag before deleting so that, in a contention scenario
    where another process overwrote our lock value between ``attach_tag``
    and this finally call, we do not clobber the winning process's lock.
    There is still a TOCTOU window between this re-read and the DELETE
    (OVH IAM has no conditional DELETE), but the worst case shrinks from
    "clobber a real lock holder" to "delete a stale tag" / "racing DELETE
    returns 404".
    """
    try:
        resource = get_vps_resource(client, urn)
    except MngrError as e:
        logger.warning("OVH recycle: failed to re-read lock tag on {} before release: {}", urn, e)
        return
    if resource is None:
        return
    current = resource.tags.get(MNGR_RECYCLING_LOCK_TAG_KEY)
    if current != lock_value:
        logger.debug(
            "OVH recycle: not releasing lock on {} (current value {!r} != ours {!r})",
            urn,
            current,
            lock_value,
        )
        return
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
