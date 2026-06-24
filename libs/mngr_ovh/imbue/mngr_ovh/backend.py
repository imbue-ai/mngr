import os
import uuid
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_ovh import hookimpl
from imbue.mngr_ovh.bootstrap import bootstrap_root_authorized_keys_via_user
from imbue.mngr_ovh.bootstrap import pin_host_key_via_tofu
from imbue.mngr_ovh.bootstrap import verify_root_ssh
from imbue.mngr_ovh.bootstrap import wait_for_ssh_after_rebuild
from imbue.mngr_ovh.catalog import resolve_image_id
from imbue.mngr_ovh.cli import ovh as ovh_cli_group
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.client import RecycleHandle
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import attach_tag
from imbue.mngr_ovh.iam_tags import attach_tags
from imbue.mngr_ovh.iam_tags import iam_region_code_for_endpoint
from imbue.mngr_ovh.iam_tags import list_vps_resources_for_provider
from imbue.mngr_ovh.iam_tags import parse_extra_tags_env
from imbue.mngr_ovh.iam_tags import vps_urn_for
from imbue.mngr_ovh.ordering import OvhOrderDeliveryTimeoutError
from imbue.mngr_ovh.ordering import order_and_wait_for_vps
from imbue.mngr_ovh.ordering import rebuild_vps_with_public_key
from imbue.mngr_ovh.ordering import try_poll_order_for_delivered_vps
from imbue.mngr_ovh.pending_orders import delete_pending_order_marker
from imbue.mngr_ovh.pending_orders import read_pending_order_markers
from imbue.mngr_ovh.pending_orders import write_pending_order_marker
from imbue.mngr_ovh.recycle import abort_recycle
from imbue.mngr_ovh.recycle import finalize_recycle
from imbue.mngr_ovh.recycle import try_recycle_cancelled_vps
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import parse_vps_build_args
from imbue.mngr_vps.host_setup import apply_host_setup_on_outer
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.primitives import VpsInstanceId

OVH_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("ovh")

_OVH_REBUILD_TASK_TIMEOUT_SECONDS: Final[float] = 1800.0


class OvhProvider(VpsProvider):
    """OVH classic-VPS provider built on top of ``VpsProvider``.

    Implements the provider-specific VPS listing via OVH IAM v2 tags and
    overrides ``_provision_vps`` with OVH's order + rebuild + TOFU flow,
    since OVH classic VPS has no cloud-init / userData support and uses a
    multi-step order/cart purchase rather than a single-POST instance API.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ovh_client: OvhVpsClient = Field(frozen=True, description="OVH API client")
    ovh_config: OvhProviderConfig = Field(frozen=True, description="OVH-specific configuration")

    _vps_iam_cache: list[str] | None = PrivateAttr(default=None)

    def reset_caches(self) -> None:
        super().reset_caches()
        self._vps_iam_cache = None

    # =========================================================================
    # Build-args parsing -- OVH uses --ovh-datacenter (alias for --ovh-region)
    # =========================================================================

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        """Parse OVH-prefixed build args. ``--ovh-datacenter=`` is an alias for ``--ovh-region=``."""
        # Rewrite the OVH datacenter alias to the canonical region form so the
        # shared parser handles the lookup uniformly.
        normalized: list[str] | None = None
        if build_args is not None:
            normalized = [
                arg.replace("--ovh-datacenter=", "--ovh-region=", 1) if arg.startswith("--ovh-datacenter=") else arg
                for arg in build_args
            ]
        return parse_vps_build_args(
            normalized,
            provider_prefix="ovh",
            default_region=self.ovh_config.default_region,
            default_plan=self.ovh_config.default_plan,
            plan_arg_name="plan",
        )

    # =========================================================================
    # Discovery -- list our VPSes via IAM v2 tags
    # =========================================================================

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return SSH hostnames for OVH VPSes tagged with this provider's name.

        Each entry is an OVH ``serviceName`` like
        ``vps-eec8860b.vps.ovh.us`` -- a DNS name that resolves to the
        VPS's public IPv4, which is what paramiko / pyinfra ultimately
        connect to.
        """
        if self._vps_iam_cache is not None:
            return list(self._vps_iam_cache)
        # No is_unconfigured guard here: the backend raises ProviderNotAuthorizedError
        # at construction when OVH has no resolvable credentials, so a constructed
        # provider always has credentials to attempt the listing with.
        # Deliberately do NOT catch IAM-listing errors here. Swallowing to an
        # empty list would make a transient OVH outage / expired credentials
        # look like "this provider has zero hosts" -- which the discovery layer
        # cannot distinguish from a real empty result, and which defeats mngr's
        # "mark hosts UNKNOWN when a provider's discovery fails" safeguard. We
        # let it propagate so `mngr list --on-error continue` records the
        # failure instead of silently dropping live hosts.
        resources = list_vps_resources_for_provider(self.ovh_client, provider_name=str(self.name))
        hostnames = [r.name for r in resources if r.name]
        self._vps_iam_cache = hostnames
        return list(hostnames)

    # =========================================================================
    # Pending-order reconciliation -- adopt VPSes from previously-timed-out orders
    # =========================================================================

    def _provider_state_dir(self) -> Path:
        """``<profile_dir>/providers/<backend>/<instance_name>/`` -- mngr's per-instance state dir.

        Mirrors :meth:`VpsProvider._key_dir` minus the ``keys/``
        leaf -- this is the dir under which all per-instance state lives
        (SSH keys, pending-order markers, ...).
        """
        state_dir = self.mngr_ctx.profile_dir / "providers" / str(self.config.backend) / str(self.name)
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    def _reconcile_pending_orders(self) -> None:
        """Walk pending-order markers and adopt any newly-delivered VPSes as recycle candidates.

        Runs at the top of every :meth:`_provision_vps`. For each marker
        under ``<provider_state_dir>/pending_orders/``:

        - One short poll of OVH (``try_poll_order_for_delivered_vps``).
        - If the order has delivered: attach the ``mngr-provider`` /
          ``mngr-host-id`` IAM tags, flip ``deleteAtExpiration=true``,
          delete the marker. The VPS is now a recycle candidate; the
          following ``_maybe_claim_recycled_vps`` call in the same bake
          can pick it up immediately.
        - If still pending: keep the marker for the next bake's reconcile.

        Failure modes are all swallowed (logged at WARNING) so a
        broken marker / transient OVH error doesn't block the current
        bake from proceeding to its normal recycle/order path. Worst
        case the orphan VPS gets retried on the next bake.
        """
        try:
            markers = read_pending_order_markers(self._provider_state_dir())
        except MngrError as exc:
            logger.warning("OVH pending-orders reconcile: marker read failed ({}); skipping reconcile", exc)
            return
        if not markers:
            return
        region_code = iam_region_code_for_endpoint(self.ovh_config.endpoint)
        provider_name = str(self.name)
        for record in markers:
            try:
                service_name = try_poll_order_for_delivered_vps(
                    self.ovh_client,
                    order_id=record.order_id,
                    plan_code=record.plan_code,
                )
            except MngrError as exc:
                logger.warning(
                    "OVH pending-orders reconcile: poll for order {} failed ({}); keeping marker for next bake",
                    record.order_id,
                    exc,
                )
                continue
            if service_name is None:
                logger.info(
                    "OVH pending-orders reconcile: order {} still has no delivered VPS; keeping marker",
                    record.order_id,
                )
                continue
            try:
                self._adopt_delivered_orphan(
                    service_name=service_name,
                    order_id=record.order_id,
                    provider_name=provider_name,
                    region_code=region_code,
                )
            except MngrError as exc:
                logger.warning(
                    "OVH pending-orders reconcile: adoption of {} (order {}) failed ({}); keeping marker",
                    service_name,
                    record.order_id,
                    exc,
                )
                continue
            try:
                delete_pending_order_marker(self._provider_state_dir(), order_id=record.order_id)
            except MngrError as exc:
                logger.warning(
                    "OVH pending-orders reconcile: marker delete for order {} failed ({}); "
                    "VPS was adopted but the marker stays -- next bake will poll once and no-op",
                    record.order_id,
                    exc,
                )

    def _adopt_delivered_orphan(
        self,
        *,
        service_name: str,
        order_id: int,
        provider_name: str,
        region_code: str,
    ) -> None:
        """Tag a newly-discovered post-timeout VPS + flip cancel, so the recycle path sees it.

        Three operations, in order:
          1. Attach ``mngr-provider=<provider_name>`` (the recycle path's
             primary filter).
          2. Attach ``mngr-host-id=host-orphan-from-order-<id>-<uuid>``
             so the orphan is traceable in ``mngr ovh list --all``.
             The recycle path swaps this tag for the new host's real id
             at claim time, so the placeholder value doesn't have to
             match any in-mngr record.
          3. ``set_renew_at_expiration(..., True)`` so the VPS satisfies
             the recycle path's ``deleteAtExpiration`` eligibility filter.
        """
        placeholder_host_id = f"host-orphan-from-order-{order_id}-{uuid.uuid4().hex}"
        urn = vps_urn_for(service_name, region_code=region_code)
        attach_tag(self.ovh_client, urn, MNGR_PROVIDER_TAG_KEY, provider_name)
        attach_tag(self.ovh_client, urn, MNGR_HOST_ID_TAG_KEY, placeholder_host_id)
        self.ovh_client.set_renew_at_expiration(service_name, True)
        # Invalidate the cached IAM list so the in-process recycle check
        # immediately after sees the freshly-tagged VPS.
        self._vps_iam_cache = None
        logger.warning(
            "OVH pending-orders reconcile: adopted slowly-delivered VPS {} from order {} "
            "(provider={}, host_id={}); cancelled so the recycle path treats it as a candidate.",
            service_name,
            order_id,
            provider_name,
            placeholder_host_id,
        )

    # =========================================================================
    # VPS provisioning -- OVH order + rebuild + TOFU + IAM tag attach
    # =========================================================================

    def _maybe_claim_recycled_vps(
        self,
        *,
        new_host_id: HostId,
        requested_plan: str,
        requested_region: str,
        extra_tags: Mapping[str, str],
    ) -> RecycleHandle | None:
        """Try to lock + re-tag a cancelled VPS; return the recycle handle or None.

        The un-cancel (``deleteAtExpiration=false``) is **not** applied
        here; the handle is registered on ``self.ovh_client`` so that the
        base ``create_host`` cleanup -- which calls
        ``vps_client.destroy_instance`` on failure -- releases the
        recycle lock instead of re-terminating. ``_on_host_finalized``
        commits the un-cancel once the host record is durably written.
        See ``recycle.try_recycle_cancelled_vps`` for the eligibility filters.
        """
        if not self.ovh_config.enable_recycle_cancelled:
            return None
        return try_recycle_cancelled_vps(
            client=self.ovh_client,
            provider_name=str(self.name),
            new_host_id=new_host_id,
            requested_plan=requested_plan,
            requested_region=requested_region,
            safety_margin_hours=self.ovh_config.recycle_safety_margin_hours,
            max_candidates=self.ovh_config.recycle_max_candidates_considered,
            extra_tags=extra_tags,
        )

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Commit a pending recycle (un-cancel + release lock) once the host is durable.

        ``vps_ip`` is the OVH ``serviceName`` for OVH-backed hosts (e.g.
        ``vps-eec8860b.vps.ovh.us`` -- a DNS name, not an IP). The
        parameter is named ``vps_ip`` for consistency with the base
        class hook signature.

        Only fires for recycled hosts; fresh-order hosts have no pending
        recycle handle and this is a no-op. ``finalize_recycle`` may
        raise via ``client.set_renew_at_expiration`` if OVH's API
        misbehaves; that's caught here so we never fail ``create_host``
        over a billing-state flip after the host record is already
        durably written. The downside is that an unfinalized recycle
        leaves the VPS in its still-cancelled state, where it will
        auto-decommission at end of month -- the operator sees the
        ``ERROR`` log line and can flip ``deleteAtExpiration=false`` by
        hand if the host is meant to be long-lived.
        """
        del host_id
        handle = self.ovh_client.get_recycle_handle(vps_ip)
        if handle is None:
            return
        try:
            finalize_recycle(self.ovh_client, handle)
        except MngrError as e:
            logger.error(
                "OVH recycle: finalize_recycle raised for {} after host record was written; "
                "the VPS may auto-decommission at end of month -- manual un-cancel may be needed. {}",
                vps_ip,
                e,
            )

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        parsed: ParsedVpsBuildOptions,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
        vps_public_key: str,
    ) -> tuple[VpsInstanceId, str]:
        """Drive the OVH classic-VPS provisioning flow.

        Unlike the Vultr-shaped base implementation, this:
        0. Tries to recycle a cancelled OVH VPS owned by this provider
           (un-cancels it via ``PUT /serviceInfos``) instead of ordering a
           fresh one. Controlled by ``OvhProviderConfig.enable_recycle_cancelled``.
        1. Otherwise orders a fresh VPS via the order/cart API.
        2. Rebuilds it with our local SSH public key pre-installed.
        3. Pins the SSH host key on first connect (TOFU; see README caveat).
        4. Attaches the ``mngr-provider`` / ``mngr-host-id`` IAM tags used
           for cross-process discovery.

        ``vps_host_key_path`` / ``vps_host_public_key`` are accepted but
        unused: OVH provides no mechanism to inject host keys at install
        time. The locally-generated host key files are left on disk; they
        do no harm and keep the base ``create_host`` flow uniform.
        ``vps_public_key`` is likewise unused here: OVH installs the SSH
        public key via the rebuild API (``get_cached_public_key`` below),
        not via the base cloud-init path that consumes it.

        The OS image is taken from ``OvhProviderConfig.default_image_name``;
        per-host image overrides are not supported via build args, matching
        the Vultr convention. (AWS now allows a per-host override via
        ``--aws-ami=<ami-id>``.)

        Returns the OVH ``serviceName`` for both the instance id *and* the
        SSH-reachable hostname -- OVH's serviceName is itself a DNS name
        like ``vps-eec8860b.vps.ovh.us`` that resolves to the VPS's IP.
        """
        del vps_host_key_path, vps_host_public_key, vps_public_key
        region = parsed.region
        plan = parsed.plan
        image_name = self.ovh_config.default_image_name

        public_key = self.ovh_client.get_cached_public_key(vps_ssh_key_id)

        # F1: parse MNGR_VPS_EXTRA_TAGS BEFORE any state-changing call
        # so a typo / reserved key / missing ``=`` fails before we order
        # (and pay for) a VPS. ``parse_extra_tags_env`` enforces OVH's
        # IAM-key regex + the reserved-key list locally, so a 400 from
        # the IAM tag attach loop -- which would otherwise leak a freshly-
        # ordered month of billing -- cannot happen. (DEPLOY_SAFETY_AUDIT-
        # style F1; spec required pre-order parsing.) Both provisioning
        # paths consume the parsed dict: the fresh-order branch attaches
        # it alongside provider/host-id below, and the recycle path passes
        # it to ``try_recycle_cancelled_vps`` (which (over)writes the tags
        # so a VPS recycled across envs reflects the new owner's
        # ``minds_env`` rather than the previous owner's). Parsing up front
        # still matters either way: if recycling falls through to a fresh
        # order, the extra tags have already been validated by that point.
        extra_tags = parse_extra_tags_env(os.environ.get("MNGR_VPS_EXTRA_TAGS", ""))

        with log_span("OVH provisioning for host {} ({})", name, host_id):
            # Reconcile any previous-bake delivery-timeout markers BEFORE
            # the recycle check, so a VPS whose order completed slowly
            # between two bakes is immediately a recycle candidate for
            # this bake (no extra round-trip latency in the failure case).
            self._reconcile_pending_orders()
            recycle_handle = self._maybe_claim_recycled_vps(
                new_host_id=host_id, requested_plan=plan, requested_region=region, extra_tags=extra_tags
            )
            # If `_provision_vps` raises before returning, the outer
            # cleanup in `_create_host_internal` never sees a vps_instance_id
            # and therefore never calls `destroy_instance`, so the recycle
            # lock would leak. Release it here on any failure path; on
            # success, ownership transfers to `_on_host_finalized` which
            # calls `finalize_recycle`.
            recycle_lock_owned = recycle_handle is not None
            # Fresh-order analogue: once `order_and_wait_for_vps` has
            # delivered a VPS, any later failure inside this function
            # leaks that VPS (the outer `_create_host_internal` cleanup
            # is gated on `vps_instance_id is not None` and we never
            # got to `return`). OVH bills monthly with no proration on
            # early termination, so a leaked fresh-order VPS costs a
            # full month. Track the freshly-ordered serviceName here
            # and terminate it in `finally` if we don't reach the
            # successful-exit point below.
            fresh_order_service_name: str | None = None
            try:
                if recycle_handle is None:
                    try:
                        service_name = order_and_wait_for_vps(
                            self.ovh_client,
                            plan_code=plan,
                            datacenter=region,
                            image_name=image_name,
                            pricing_mode=self.ovh_config.pricing_mode.to_wire_value(),
                            duration=self.ovh_config.duration,
                            deliver_timeout_seconds=self.ovh_config.instance_boot_timeout,
                        )
                    except OvhOrderDeliveryTimeoutError as exc:
                        # OVH accepted the order but the VPS didn't deliver
                        # within ``instance_boot_timeout``. Without intervention,
                        # any later delivery becomes an unmanaged orphan
                        # (no ``mngr-provider`` tag => invisible to
                        # ``list_vps_resources_for_provider`` => the next
                        # bake's recycle path can't see it => we leak a
                        # full month of billing). Write a pending-order
                        # marker so :meth:`_reconcile_pending_orders` on
                        # the next ``mngr create`` polls OVH for this
                        # order's VPS and tags it as a recycle candidate
                        # once it surfaces. The bake still fails here --
                        # the marker is the only recovery mechanism, and
                        # it runs out-of-band on the next bake.
                        write_pending_order_marker(
                            self._provider_state_dir(),
                            order_id=exc.order_id,
                            plan_code=plan,
                            region=region,
                        )
                        raise
                    fresh_order_service_name = service_name
                else:
                    service_name = recycle_handle.service_name

                # Tag-immediately on first sight: attach mngr-provider /
                # mngr-host-id as the very first action against the new
                # serviceName. Anything that fails later (rebuild, TOFU,
                # root bootstrap, host-record write) leaves the VPS
                # discoverable via the normal mngr discovery path (which
                # filters on `mngr-provider`), so the operator sees the
                # orphan in `mngr list` and the create-cleanup path can
                # clean it up by service name. The recycle path arrives
                # already tagged -- `try_recycle_cancelled_vps` swapped
                # `mngr-host-id` to the new host id and (over)wrote the
                # extra tags under a cooperative lock -- so we skip the
                # re-tag here to avoid redundant POST /tag calls.
                urn = vps_urn_for(service_name, region_code=iam_region_code_for_endpoint(self.ovh_config.endpoint))
                if recycle_handle is None:
                    # F1: ``extra_tags`` was parsed at the very top of
                    # ``_provision_vps`` so a typo / reserved key has
                    # already failed before we got here. Just merge.
                    # ``MNGR_VPS_EXTRA_TAGS`` mirrors the contract
                    # ``mngr_vps.build_vps_tags`` honors for
                    # Vultr-style callers (e.g. the imbue_cloud pool
                    # bake setting ``minds_env=<name>``).
                    all_tags: dict[str, str] = {
                        MNGR_PROVIDER_TAG_KEY: str(self.name),
                        MNGR_HOST_ID_TAG_KEY: str(host_id),
                    }
                    all_tags.update(extra_tags)
                    attach_tags(
                        self.ovh_client,
                        urn,
                        all_tags,
                    )
                # Invalidate the IAM-listing cache so a concurrent
                # `mngr list` / `mngr ovh list` issued later in this
                # process sees the new VPS. Done for both fresh-order
                # (tags just attached above) and recycle (tags swapped
                # by try_recycle_cancelled_vps) paths.
                self._vps_iam_cache = None

                image_id = resolve_image_id(self.ovh_client, service_name, image_name)
                rebuild_vps_with_public_key(
                    self.ovh_client,
                    service_name=service_name,
                    image_id=image_id,
                    public_ssh_key=public_key,
                    task_timeout_seconds=_OVH_REBUILD_TASK_TIMEOUT_SECONDS,
                )

                wait_for_ssh_after_rebuild(
                    hostname=service_name,
                    port=22,
                    timeout_seconds=self.config.ssh_connect_timeout,
                )

                # OVH installs the rebuild key for the image's default
                # non-root user (e.g. `debian` on `Debian 12 - Docker`),
                # not for root. TOFU + bootstrap happen as that user;
                # the bootstrap sudo-copies the key to /root/.ssh so the
                # rest of the provider (which operates as root via the
                # base VpsProvider) works without per-call sudos.
                vps_private_key_path, _ = self._get_vps_ssh_keypair()
                bootstrap_user = self.ovh_config.bootstrap_ssh_user
                pin_host_key_via_tofu(
                    hostname=service_name,
                    port=22,
                    ssh_user=bootstrap_user,
                    private_key_path=vps_private_key_path,
                    known_hosts_path=self._vps_known_hosts_path(),
                    timeout_seconds=self.config.ssh_connect_timeout,
                )
                bootstrap_root_authorized_keys_via_user(
                    hostname=service_name,
                    port=22,
                    bootstrap_user=bootstrap_user,
                    private_key_path=vps_private_key_path,
                    known_hosts_path=self._vps_known_hosts_path(),
                    timeout_seconds=self.config.ssh_connect_timeout,
                )
                verify_root_ssh(
                    hostname=service_name,
                    port=22,
                    private_key_path=vps_private_key_path,
                    known_hosts_path=self._vps_known_hosts_path(),
                    timeout_seconds=self.config.ssh_connect_timeout,
                )
                # OVH has no cloud-init, so the host-level setup that cloud-init
                # backends (Vultr) get at first boot is applied here over SSH via
                # the single shared source of truth (``apply_host_setup_on_outer``):
                # pinned Docker, optional gVisor runsc (gated by
                # ``install_gvisor_runtime``), sshd tuning, the base packages
                # mngr_vps needs (rsync/inotify-tools/jq), plus the
                # OVH-specific qemu purge that disables the hypervisor's
                # filesystem-freezing automated backups. Runs as the final
                # outer-bootstrap step (on both the fresh-order and recycle
                # paths -- the recycle rebuild reinstalls the qemu agent) before
                # the base VpsProvider takes over. Any failure raises and
                # aborts provisioning, so no half-set-up host is handed back.
                with self._make_outer_for_vps_ip(service_name) as outer:
                    apply_host_setup_on_outer(
                        outer,
                        install_gvisor_runtime=self.config.install_gvisor_runtime,
                        is_qemu_purge_enabled=True,
                    )
                # All post-claim steps succeeded. Ownership of both the
                # recycle lock (recycle path) and the freshly-ordered
                # VPS (fresh-order path) transfers to the caller -- on
                # success `_on_host_finalized` finalizes the recycle,
                # and on later failure `_create_host_internal` will
                # call `destroy_instance` with the now-returned
                # vps_instance_id. Disarm both abort-on-failure
                # branches below.
                recycle_lock_owned = False
                fresh_order_service_name = None
            finally:
                if recycle_lock_owned and recycle_handle is not None:
                    abort_recycle(self.ovh_client, recycle_handle)
                if fresh_order_service_name is not None:
                    self._terminate_orphaned_fresh_order(fresh_order_service_name)

        return VpsInstanceId(service_name), service_name

    def _terminate_orphaned_fresh_order(self, service_name: str) -> None:
        """Best-effort terminate of a freshly-ordered OVH VPS that we are about to leak.

        Called from the ``_provision_vps`` ``finally`` branch when an
        exception fires after ``order_and_wait_for_vps`` succeeded but
        before the caller takes ownership. Wraps the failure in a
        narrow try/except so the cleanup error doesn't mask the
        primary exception that triggered the abort.
        """
        try:
            self.ovh_client.destroy_instance(VpsInstanceId(service_name))
            logger.warning(
                "OVH _provision_vps failed after fresh order delivered {}; requested termination to avoid a leaked month of billing",
                service_name,
            )
        except MngrError as e:
            logger.error(
                "OVH _provision_vps cleanup: failed to terminate freshly-ordered VPS {} ({}); manual cleanup may be needed to avoid a leaked month of billing",
                service_name,
                e,
            )


class OvhProviderBackend(ProviderBackendInterface):
    """Backend for creating OVH classic-VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return OVH_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on OVH classic VPS instances"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return OvhProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "OVH-specific args (consumed by provider, not passed to docker):\n"
            "  --ovh-datacenter=DC   OVH datacenter (e.g. US-EAST-VA, US-WEST-OR)\n"
            "                        (alias: --ovh-region=)\n"
            "  --ovh-plan=PLAN       OVH plan code (default: vps-2025-model1 = VPS-1)\n"
            "  --git-depth=N         Shallow-clone build context to depth N before upload\n"
            "\n"
            "All other build args are passed to 'docker build' on the VPS.\n"
            "Example: -b --ovh-plan=vps-2025-model1 -b --file=Dockerfile -b .\n"
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, OvhProviderConfig):
            raise MngrError(f"Expected OvhProviderConfig, got {type(config).__name__}")
        ovh_client = build_ovh_client(config)
        # An enabled-but-unauthenticated provider is an error, not a silent
        # zero-result listing: if no credentials are resolvable anywhere (config,
        # OVH_* env, ~/.ovh.conf), surface it consistently with the other cloud
        # providers. ProviderNotAuthorizedError is a ProviderUnavailableError, so
        # read paths still treat it as unavailable rather than empty.
        if ovh_client.is_unconfigured:
            raise ProviderNotAuthorizedError(
                name,
                reason="OVH credentials not configured",
                short_remediation="set OVH_* env vars, configure ~/.ovh.conf, or set credentials in [providers.<name>]",
            )
        return OvhProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=ovh_client,
            ovh_client=ovh_client,
            ovh_config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the OVH provider backend."""
    return (OvhProviderBackend, OvhProviderConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr ovh ...`` operator command group."""
    return [ovh_cli_group]
