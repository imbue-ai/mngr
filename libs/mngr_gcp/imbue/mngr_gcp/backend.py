import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from functools import cached_property
from typing import Any
from typing import Final

import click
from google.auth import exceptions as google_auth_exceptions
from google.auth.credentials import Credentials
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.ssh_utils import wait_for_expected_host_key
from imbue.mngr_gcp import hookimpl
from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.client import HOST_ID_METADATA_KEY
from imbue.mngr_gcp.client import HOST_NAME_METADATA_KEY
from imbue.mngr_gcp.client import ISOLATION_METADATA_KEY
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.startup_script import generate_gce_startup_script
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import extract_presence_flag
from imbue.mngr_vps.build_args import extract_single_value_arg
from imbue.mngr_vps.build_args import raise_if_unknown_provider_arg
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.host_state_store import HostDirBackend
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance_offline import OfflineCapableVpsProvider
from imbue.mngr_vps.instance_offline import host_name_from_prefixed_value
from imbue.mngr_vps.primitives import VpsInstanceId

GCP_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("gcp")

# GCP has no object-storage state bucket; the offline mirror lives in the
# instance's own GCE *metadata*, which is large and permissive (256 KB per value,
# 512 KB total) -- unlike GCE labels, which lowercase and restrict to
# ``[a-z0-9_-]``, 63 chars. The full ``VpsHostRecord`` JSON lives under
# ``mngr-host-state`` and one JSON value per agent under ``mngr-agent-<agent_id>``,
# so a STOPPED instance (no external IP, SSH unreachable) still surfaces its host
# and agents in discovery and resolves by name. This is the GCP analog of the
# AWS/Azure object-storage state bucket: both back the uniform ``HostStateStore``.
HOST_STATE_METADATA_KEY: Final[str] = "mngr-host-state"
AGENT_METADATA_PREFIX: Final[str] = "mngr-agent-"
# GCE statuses in which the guest OS is down (so the SSH-based sweep can't see the
# host) but the instance still exists and must be surfaced offline from metadata.
# ``STOPPING`` is included so a host doesn't vanish from discovery during the stop
# transition before it reaches the terminal ``TERMINATED`` (GCE's name for a
# stopped -- not deleted -- instance).
_HOST_DOWN_STATES: Final[frozenset[str]] = frozenset({"STOPPING", "TERMINATED"})
# ``mngr-host-name`` metadata holds ``mngr-<host_name>``; strip the prefix to
# recover the host name when labelling a stopped host in discovery.
_HOST_NAME_PREFIX: Final[str] = "mngr-"

# The self-stopping idle watcher (in-container sentinel + host-side systemd
# ``.path``/``.service``) is shared by the base ``OfflineCapableVpsProvider``.
# Identical mechanism to AWS: the GCP oneshot ``.service`` runs ``shutdown -P now``,
# which on GCE lands the instance in ``TERMINATED`` (stopped, disk preserved, no
# compute billing) -- there is no GCE analog to AWS's
# ``InstanceInitiatedShutdownBehavior`` and none is needed (and no IAM/API call).
# Offline host_dir is captured operator-side at ``mngr stop`` and uploaded to a GCS
# state bucket (``GcsStateBucket``), so a stopped instance's host_dir is readable
# without SSH; the host + agent records still live in GCE instance metadata (which
# is large enough for those JSON blobs and always available with no prepare step).


def _gcp_not_authorized_error(
    name: ProviderInstanceName, reason: str, short_remediation: str, short_reason: str | None = None
) -> ProviderNotAuthorizedError:
    """Build a ``ProviderNotAuthorizedError`` with GCP-specific, actionable help text.

    The generic unavailable help text tells the user to "start Docker", which is wrong
    advice for a GCP ADC / project failure -- so we curate the guidance toward resolving
    Application Default Credentials and the project. ``ProviderNotAuthorizedError`` is a
    ``ProviderUnavailableError`` subclass, so read paths still treat the provider as
    unavailable rather than silently empty.
    """
    help_text = (
        "GCP could not be reached. Check, in order:\n"
        "  - credentials: run `gcloud auth application-default login` (or set "
        "GOOGLE_APPLICATION_CREDENTIALS to a service-account key);\n"
        "  - project: set `project_id` in [providers.gcp], or run `gcloud config set project <id>`;\n"
        "  - one-time setup: run `mngr gcp prepare` if you have not yet.\n"
        f"Or disable the provider: mngr config set --scope user providers.{name}.is_enabled false"
    )
    return ProviderNotAuthorizedError(
        name,
        reason=reason,
        short_remediation=short_remediation,
        user_help_text=help_text,
        short_reason=short_reason,
    )


def _resolve_credentials_project_and_zone_or_unavailable(
    name: ProviderInstanceName, config: GcpProviderConfig, concurrency_group: ConcurrencyGroup
) -> tuple[Credentials, str, str]:
    """Resolve ADC credentials, project, and the effective GCE zone, raising ``ProviderUnavailableError`` on failure.

    Resolve the zone/region first -- a config-only check, plus a best-effort
    ``gcloud config get compute/zone`` probe (via ``concurrency_group``) when no
    ``default_zone`` is pinned -- then resolve ADC (``google.auth.default()``),
    which yields both the credentials and the project ADC
    infers from the environment (``GOOGLE_CLOUD_PROJECT`` / ``gcloud config set
    project`` / metadata), used by ``resolve_project_id`` as the fallback when no
    explicit ``project_id`` is set. A single ``default()`` call serves both, so we
    never probe twice.

    A failure here means we could not authenticate to GCP (or the configured
    zone/region are self-inconsistent), so the provider's state is *unknown* --
    hence ``ProviderUnavailableError`` (not ``ProviderEmptyError``, which asserts
    "reached and definitively empty"): the shared discovery path surfaces it
    instead of silently dropping the provider, and ``mngr gc`` skips it rather
    than treating its hosts as garbage.
    """
    try:
        gcloud_zone = None if config.default_zone else get_gcloud_compute_zone(concurrency_group)
        zone, _region = config.resolve_zone_and_region(gcloud_zone)
        credentials, adc_project = config.get_credentials_and_resolved_project()
        project_id = config.resolve_project_id(adc_project)
    except (ValueError, google_auth_exceptions.GoogleAuthError) as e:
        raise _gcp_not_authorized_error(
            name,
            str(e),
            "run `gcloud auth application-default login` (and set project_id)",
            short_reason="GCP credentials or project not configured",
        ) from e
    return credentials, project_id, zone


class ParsedGcpBuildOptions(ParsedVpsBuildOptions):
    """``ParsedVpsBuildOptions`` extended with the GCP-only ``--gcp-spot`` / ``--gcp-image`` knobs.

    Returned by ``GcpProvider._parse_build_args`` and consumed by
    ``GcpProvider._create_vps_instance`` so the Spot opt-in and per-host image
    override flow through to ``GcpVpsClient.create_instance`` without touching the
    shared ``VpsClientInterface`` (mirrors ``ParsedAwsBuildOptions``).
    """

    spot: bool = Field(
        default=False,
        description=(
            "Per-host opt-in for GCE Spot capacity, from the presence-only ``--gcp-spot`` build arg. "
            "When True, ``GcpVpsClient.create_instance`` launches the VM with "
            "``scheduling.provisioning_model=SPOT`` (and ``instance_termination_action=DELETE`` so a "
            "preempted Spot VM is deleted, not left stopped)."
        ),
    )
    image: str | None = Field(
        default=None,
        description=(
            "Per-host GCE source-image override, from the ``--gcp-image=`` build arg. When set, "
            "``GcpVpsClient.create_instance`` boots this VM from the given image instead of the "
            "config's ``default_source_image``; when None the configured default is used."
        ),
    )


class GcpProvider(OfflineCapableVpsProvider):
    """GCP-specific provider that discovers hosts via the GCE instances.list API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    gcp_client: GcpVpsClient = Field(frozen=True, description="GCE API client")
    gcp_config: GcpProviderConfig = Field(frozen=True, description="GCP-specific configuration")

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List GCE instances labeled with this provider's name."""
        return self.gcp_client.list_instances(provider_tag=str(self.name))

    def _validate_provider_args_for_create(self) -> None:
        """Pre-create hook: announce an inferred project, enforce the pytest safety net, require the firewall.

        Called by ``create_host`` before the first provider write, so every
        check here fails cleanly with no leaked resources.

        1. When ``project_id`` was not pinned in the config (so it was inferred
           from ADC), log which project we are about to create billable
           instances in. This fires only at create time -- not on every
           ``mngr list`` discovery pass -- so a stray ``gcloud config`` default
           is never used silently.

        2. Mirror the AWS guard (``mngr_aws.backend.AwsProvider``): when
           ``PYTEST_CURRENT_TEST`` is set, the test harness is responsible for
           the safety net that prevents leaked cost if pytest itself is killed.
           For GCP that net is ``scheduling.max_run_duration`` +
           ``instance_termination_action=DELETE`` (both rely on
           ``auto_shutdown_seconds`` being set). If it isn't, fail closed.

        3. Require the SSH firewall rule (created once via ``mngr gcp prepare``)
           to already exist. Checking it read-only here -- before create_host
           uploads the SSH key or creates the instance -- means a first-time
           user who hasn't run ``prepare`` gets the clean "run mngr gcp prepare"
           message immediately, instead of it surfacing mid-create under a
           "Host creation failed, attempting cleanup..." line. With an empty
           ``allowed_ssh_cidrs`` (no ingress requested) ``resolve_firewall``
           short-circuits and this check is a no-op: no rule is expected, so the
           instance launches intentionally unreachable.
        """
        if not self.gcp_config.project_id:
            logger.info(
                "No GCP project_id configured; creating instances in project {!r} resolved from "
                "Application Default Credentials (gcloud config / GOOGLE_CLOUD_PROJECT). Run "
                "'mngr config set providers.gcp.project_id <your-project>' to pin it explicitly.",
                self.gcp_client.project_id,
            )
        if "PYTEST_CURRENT_TEST" in os.environ:
            seconds = self._get_effective_auto_shutdown_seconds()
            if not (seconds and seconds > 0):
                raise MngrError(
                    "Refusing to create GCE instance during pytest without "
                    "auto_shutdown_seconds set on the GCP provider config. "
                    "Set [providers.<instance>] auto_shutdown_seconds = <N> in "
                    "the project settings.toml so the instance launches with "
                    "scheduling.max_run_duration + instance_termination_action=DELETE "
                    "and self-deletes even if pytest is killed."
                )
        # Read-only firewall pre-flight. ``resolve_firewall`` raises a MngrError
        # pointing at ``mngr gcp prepare`` when the rule is missing. The hot
        # ``create_instance`` path resolves it again to get the target tag; this
        # extra GET is cheap and is what lets the failure happen early and clean.
        self.gcp_client.resolve_firewall()

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedGcpBuildOptions:
        """Parse GCP-prefixed build args.

        Accepts ``--gcp-zone=ZONE`` (GCE VMs are zonal, so the placement knob is
        a zone, not a region; it must equal the provider's bound zone), the
        machine type via ``--gcp-machine-type=TYPE``, a per-host boot-disk image
        override via ``--gcp-image=IMAGE`` (defaults to the config's
        ``default_source_image``), ``--gcp-spot`` (presence-only, opts the host
        onto GCE Spot capacity), and the shared ``--git-depth=N``. Composed from
        the shared low-level helpers (rather
        than the ``parse_vps_build_args`` convenience, which hardcodes a
        ``--<prefix>-region=`` flag) so the flag is named ``--gcp-zone`` to
        match GCE's zonal model. The parsed value populates
        ``ParsedVpsBuildOptions.region``, which the base threads to
        ``create_instance(region=...)`` -- the GCP client interprets that as the
        zone.
        """
        args = list(build_args or ())
        zone, args = extract_single_value_arg(args, "--gcp-zone=")
        machine_type, args = extract_single_value_arg(args, "--gcp-machine-type=")
        image, args = extract_single_value_arg(args, "--gcp-image=")
        spot, args = extract_presence_flag(args, "--gcp-spot")
        git_depth, args = extract_git_depth(args)
        valid_args = ("--gcp-zone=", "--gcp-machine-type=", "--gcp-image=", "--gcp-spot", "--git-depth=")
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            raise_if_unknown_provider_arg(arg, "gcp", valid_args)
            docker_build_args.append(arg)
        return ParsedGcpBuildOptions(
            region=zone or self.gcp_client.zone,
            plan=machine_type or self.gcp_config.default_machine_type,
            spot=spot,
            image=image,
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
        )

    def _create_vps_instance(
        self,
        parsed: ParsedVpsBuildOptions,
        label: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        """GCP override: thread the per-host ``--gcp-spot`` / ``--gcp-image`` knobs into ``GcpVpsClient.create_instance``.

        Calls through ``self.gcp_client`` (the concrete typed GCP client) rather
        than the shared ``self.vps_client`` interface so the GCP-only ``spot`` and
        ``image`` kwargs are statically visible, mirroring
        ``AwsProvider._create_vps_instance``.
        """
        gcp_parsed = self._require_parsed(parsed, ParsedGcpBuildOptions)
        spot = gcp_parsed.spot
        image = gcp_parsed.image
        return self.gcp_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
            spot=spot,
            image=image,
        )

    def _generate_bootstrap_payload(
        self,
        *,
        host_private_key: str,
        host_public_key: str,
        authorized_user_public_key: str,
    ) -> str:
        """GCP override: render a GCE ``startup-script`` instead of cloud-init ``user-data``.

        Stock GCE images (Debian especially) ship no cloud-init; the
        google-guest-agent runs ``startup-script`` on every image instead.
        ``GcpVpsClient.create_instance`` stores it under that metadata key.
        """
        return generate_gce_startup_script(
            host_private_key=host_private_key,
            host_public_key=host_public_key,
            install_gvisor_runtime=self.config.install_gvisor_runtime,
            auto_shutdown_seconds=self._get_effective_auto_shutdown_seconds(),
            authorized_user_public_key=authorized_user_public_key,
        )

    def _wait_for_expected_host_key(self, vps_ip: str, expected_host_public_key: str, timeout_seconds: float) -> None:
        """GCP override: wait until the VM serves our host key before strict-checking.

        The startup-script installs the host key after sshd has already booted with
        a random key, so poll the live key until it matches and the mismatch window
        is closed.
        """
        wait_for_expected_host_key(
            hostname=vps_ip,
            port=22,
            expected_host_public_key=expected_host_public_key,
            timeout_seconds=timeout_seconds,
        )

    # The shared ``OfflineCapableVpsProvider._list_provider_vps_hostnames``
    # (cached listing -> non-empty main_ip) covers GCP unchanged: a stopped GCE
    # instance loses its external IP and is excluded by the non-empty IP check.

    # =========================================================================
    # Native GCE stop/start (idle-pause + resume) -- the base
    # OfflineCapableVpsProvider owns the orchestration; here we supply only the
    # GCE-specific cloud-API hooks.
    # =========================================================================

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        with log_span("Stopping GCE instance"):
            self.gcp_client.stop_instance(instance_id)

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        with log_span("Starting GCE instance"):
            return self.gcp_client.start_instance(instance_id)

    # =========================================================================
    # Self-stopping idle watcher (in-container sentinel + host-side systemd)
    # =========================================================================

    @property
    def _supports_bare_isolation(self) -> bool:
        # GCE supports stop/start, and the bare idle path powers the instance off
        # (which on GCE stops it), so bare placement is supported.
        return True

    def _provider_instance_kind(self) -> str:
        return "GCE instance"

    # The base ``OfflineCapableVpsProvider`` owns the idle-watcher install and the
    # shutdown-script write. GCP's ``.service`` body is the default
    # ``shutdown -P now`` (a GCE guest poweroff stops the instance), so GCP
    # overrides none of those hooks. Offline ``host_dir`` is captured operator-side
    # at ``mngr stop`` to the GCS state bucket; ``_host_dir_backend`` (selected
    # below) is bucket-backed when the bucket exists and the feature is enabled,
    # and the no-op backend otherwise.

    def _instances_matching_host_id(self, host_id: HostId) -> list[dict[str, Any]]:
        """Match on the host id stored verbatim in instance metadata (GCE labels cannot hold a raw mngr host id)."""
        wanted = str(host_id)
        return [
            instance
            for instance in self._list_instances_cached()
            if self._metadata_dict(instance).get(HOST_ID_METADATA_KEY) == wanted
        ]

    # =========================================================================
    # Offline discovery + the metadata-backed state store (so STOPPED hosts list
    # and resolve by name without SSH, uniformly with the AWS/Azure buckets)
    # =========================================================================

    @cached_property
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror, backed by GCE instance metadata.

        Unlike AWS/Azure, GCP keeps host + agent *records* in GCE instance metadata
        (see ``_GceMetadataHostStateStore``): the records are small JSON blobs that
        fit comfortably in metadata, which is always available with no ``prepare``
        step. The GCS state bucket is used only for the offline ``host_dir`` mirror
        (where the size limit *would* matter), via the bucket-backed
        ``_host_dir_backend`` below.
        """
        return _GceMetadataHostStateStore(provider=self)

    @cached_property
    def _state_bucket(self) -> GcsStateBucket | None:
        """Return the GCS state bucket when it actually exists, else None.

        The bucket holds the offline ``host_dir`` mirror written by ``mngr stop``.
        None means the bucket does not exist yet (``mngr gcp prepare`` was never
        run) and ``_host_dir_backend`` falls back to the no-op backend. A storage
        error while probing existence propagates rather than masquerading as
        "absent". The existence probe runs at most once per provider lifetime
        (cached). Mirrors ``AwsProvider._state_bucket`` / ``AzureProvider._state_bucket``.
        """
        return self._resolve_existing_state_bucket()

    def _resolve_existing_state_bucket(self) -> GcsStateBucket | None:
        """Build the configured/derived bucket and return it only if it exists.

        Returns None only when the bucket genuinely does not exist. A
        ``bucket_exists`` storage error propagates -- the offline host_dir feature
        is opt-in (config flag), but when on the inability to check is an
        operational failure, not a silent "no bucket".
        """
        bucket = self.gcp_config.build_state_bucket(
            credentials=self.gcp_client.credentials,
            project_id=self.gcp_client.project_id,
            region=self._gcs_bucket_region(),
        )
        if not bucket.bucket_exists():
            logger.debug(
                "GCS state bucket {} does not exist; offline host_dir is unavailable "
                "(run `mngr gcp prepare` to create it)",
                bucket.bucket_name,
            )
            return None
        return bucket

    def _gcs_bucket_region(self) -> str:
        """The region to anchor the GCS state bucket on.

        GCS buckets are region-scoped (multi-region is possible but unnecessary
        here). The bucket lives in the same region as the GCE instances writing to
        it, so reads/writes from the operator's mngr CLI hit the closest endpoint.
        Falls back to the zone's region when ``default_region`` is unset.
        """
        return self.gcp_config.resolve_state_bucket_region(self.gcp_client.zone)

    @cached_property
    def _host_dir_backend(self) -> HostDirBackend:
        """Select the offline host_dir backend once: bucket-backed when enabled + present, else no-op.

        Delegates to the shared ``_select_bucket_host_dir_backend``, supplying the
        resolved GCS bucket and the config's ``is_offline_host_dir_enabled`` flag.
        Mirrors ``AwsProvider._host_dir_backend`` / ``AzureProvider._host_dir_backend``.
        """
        return self._select_bucket_host_dir_backend(
            self._state_bucket, enabled=self.gcp_config.is_offline_host_dir_enabled
        )

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the GCE instance's OS is down (STOPPING or TERMINATED).

        GCE power state rides along for free in the instance listing, so this
        needs no extra call. A stopped GCE instance has no external IP and is
        SSH-unreachable.
        """
        return instance.get("state") in _HOST_DOWN_STATES

    def _metadata_dict(self, instance: Mapping[str, Any]) -> dict[str, str]:
        """Return the instance's metadata dict from the normalized list shape."""
        metadata = instance.get("metadata", {})
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    def _isolation_marker_for_instance(self, instance: Mapping[str, Any]) -> str | None:
        """Read the ``mngr-isolation`` placement marker from GCE instance metadata (no SSH), or None.

        GCP stores mngr identity in metadata (GCE labels are too restricted), so
        the marker is read from metadata here rather than the normalized tag list.
        """
        return self._metadata_dict(instance).get(ISOLATION_METADATA_KEY)

    def _host_name_from_instance(self, instance: Mapping[str, Any]) -> HostName:
        """Recover the host name from the ``mngr-host-name`` metadata (fallback: host-id metadata)."""
        metadata = self._metadata_dict(instance)
        return host_name_from_prefixed_value(
            metadata.get(HOST_NAME_METADATA_KEY, ""), metadata.get(HOST_ID_METADATA_KEY, "")
        )

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED-state DiscoveredHost from an instance's metadata, or None.

        Reads only the cheap identity metadata stamped at create (host id +
        host name), never the metadata state record -- the full record is read from
        the state store on demand.
        """
        host_id_str = self._metadata_dict(instance).get(HOST_ID_METADATA_KEY)
        if host_id_str is None:
            return None
        return DiscoveredHost(
            host_id=HostId(host_id_str),
            host_name=self._host_name_from_instance(instance),
            provider_name=self.name,
            host_state=HostState.STOPPED,
        )

    def _remirror_host_name(self, host_record: VpsHostRecord, name: HostName) -> None:
        """Re-stamp the ``mngr-host-name`` identity metadata (read by offline discovery) after a rename.

        Matches create's value (``f"{_HOST_NAME_PREFIX}{name}"``), which
        ``_host_name_from_instance`` strips back off, so a renamed-then-stopped
        host lists under its new name.
        """
        if host_record.config is None:
            return
        self.gcp_client.set_instance_metadata(
            host_record.config.vps_instance_id, {HOST_NAME_METADATA_KEY: f"{_HOST_NAME_PREFIX}{name}"}
        )


class _GceMetadataHostStateStore(HostStateStore):
    """Instance-metadata-backed mirror: full host + agent records live in GCE metadata.

    The GCP analog of the AWS/Azure ``BucketHostStateStore``. GCP has no
    object-storage state bucket; instead the full ``VpsHostRecord`` JSON (key
    ``mngr-host-state``) and one JSON value per agent (key ``mngr-agent-<id>``)
    live in the instance's own GCE metadata, which is large and permissive enough
    (256 KB per value, 512 KB total) to hold them -- unlike GCE labels. Exposing it
    behind the same ``HostStateStore`` interface makes the offline read/write paths
    uniform across all three providers.

    Keyed by ``host_id``; the instance is resolved from the provider's cached
    listing. All methods are best-effort and idempotent: a write resolves the live
    instance and merges via ``set_instance_metadata`` (a whole-object
    read-modify-write guarded by a fingerprint), and a read serves from the cached
    listing's metadata with no extra GET.
    """

    provider: "GcpProvider"

    def persist_host_record(self, record: VpsHostRecord) -> None:
        host_id = HostId(record.certified_host_data.host_id)
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No GCE instance found for host {}; cannot mirror host record to metadata", host_id)
            return
        self.provider.gcp_client.set_instance_metadata(
            VpsInstanceId(instance["id"]), {HOST_STATE_METADATA_KEY: record.model_dump_json()}
        )

    def delete_host_state(self, host_id: HostId) -> None:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return
        agent_keys = [key for key in self.provider._metadata_dict(instance) if key.startswith(AGENT_METADATA_PREFIX)]
        self.provider.gcp_client.set_instance_metadata(
            VpsInstanceId(instance["id"]), {}, [HOST_STATE_METADATA_KEY, *agent_keys]
        )

    def persist_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No GCE instance found for host {}; cannot mirror agent metadata", host_id)
            return
        self.provider.gcp_client.set_instance_metadata(
            VpsInstanceId(instance["id"]), {f"{AGENT_METADATA_PREFIX}{agent_id}": json.dumps(dict(agent_data))}
        )

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return
        self.provider.gcp_client.set_instance_metadata(
            VpsInstanceId(instance["id"]), {}, [f"{AGENT_METADATA_PREFIX}{agent_id}"]
        )

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return []
        records: list[dict] = []
        for key, value in self.provider._metadata_dict(instance).items():
            if not key.startswith(AGENT_METADATA_PREFIX):
                continue
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as e:
                logger.warning("Skipping unparseable agent metadata {!r}: {}", key, e)
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                logger.warning("Skipping agent metadata {!r}: value is not a JSON object", key)
        return records

    def read_host_record(self, host_id: HostId) -> VpsHostRecord | None:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return None
        record_json = self.provider._metadata_dict(instance).get(HOST_STATE_METADATA_KEY)
        if record_json is None:
            return None
        try:
            return VpsHostRecord.model_validate_json(record_json)
        except ValueError as e:
            # A malformed record raises rather than returning None (matching
            # BucketHostStateStore.read_host_record): silently dropping it would
            # make an otherwise-known stopped host vanish from listings.
            raise MngrError(f"Malformed host record in GCE metadata for {host_id}: {e}") from e


class GcpProviderBackend(ProviderBackendInterface):
    """Backend for creating GCP Compute Engine VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return GCP_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on GCP Compute Engine VMs"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return GcpProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "GCE-specific args (consumed by provider, not passed to docker):\n"
            "  --gcp-zone=ZONE          GCE zone, e.g. us-west1-a (GCE VMs are zonal; must equal\n"
            "                           the provider's configured zone; defaults to the config's\n"
            "                           default_zone, the active gcloud compute/zone, or us-west1-a)\n"
            "  --gcp-machine-type=TYPE  GCE machine type (default: e2-small)\n"
            "  --gcp-image=IMAGE        GCE boot-disk source image for this host, overriding the\n"
            "                           config's default_source_image (a full image / family URL)\n"
            "  --gcp-spot               Run on GCE Spot capacity (presence-only flag; preemptible).\n"
            "  --git-depth=N            Shallow-clone build context to depth N before upload\n"
            "\n"
            "When --gcp-image is omitted the VM image is taken from the provider config\n"
            "(default_source_image).\n"
            "\n"
            "All other build args are passed to 'docker build' on the GCE instance.\n"
            "Example: -b --gcp-machine-type=e2-medium -b --file=Dockerfile -b .\n"
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
        if not isinstance(config, GcpProviderConfig):
            raise MngrError(f"Expected GcpProviderConfig, got {type(config).__name__}")

        # Resolve credentials + project. On failure this raises
        # ProviderUnavailableError (state unknown), which the shared discovery
        # path surfaces to the user on read paths (mngr list / connect / gc) and
        # which mngr create surfaces directly -- no custom warning or create-path
        # bootstrap hook is needed.
        credentials, project_id, zone = _resolve_credentials_project_and_zone_or_unavailable(
            name, config, mngr_ctx.concurrency_group
        )

        gcp_client = GcpVpsClient(
            credentials=credentials,
            project_id=project_id,
            zone=zone,
            # GCE VM source image -- distinct from config.default_image (inherited),
            # which is the Docker *container* image run inside the VM.
            image=config.default_source_image,
            machine_type=config.default_machine_type,
            boot_disk_size_gb=config.boot_disk_size_gb,
            boot_disk_type=config.boot_disk_type,
            network=config.network,
            subnetwork=config.subnetwork,
            allowed_ssh_cidrs=config.allowed_ssh_cidrs,
            firewall_name=config.firewall_name,
            firewall_target_tag=config.firewall_target_tag,
            associate_external_ip=config.associate_external_ip,
            service_account_email=config.service_account_email,
            service_account_scopes=config.service_account_scopes,
            auto_shutdown_seconds=config.auto_shutdown_seconds,
            container_ssh_port=config.container_ssh_port,
        )

        return GcpProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=gcp_client,
            gcp_client=gcp_client,
            gcp_config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the GCP provider backend."""
    return (GcpProviderBackend, GcpProviderConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr gcp ...`` operator command group."""
    return [gcp_cli_group]
