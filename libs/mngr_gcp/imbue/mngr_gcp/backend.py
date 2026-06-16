import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
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
from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import wait_for_expected_host_key
from imbue.mngr_gcp import hookimpl
from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.client import HOST_NAME_METADATA_KEY
from imbue.mngr_gcp.client import to_gce_label_value
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.startup_script import generate_gce_startup_script
from imbue.mngr_vps.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import IDLE_SENTINEL_FILENAME
from imbue.mngr_vps.instance import OfflineCapableVpsProvider
from imbue.mngr_vps.instance import ParsedVpsBuildOptions
from imbue.mngr_vps.instance import extract_git_depth
from imbue.mngr_vps.instance import extract_presence_flag
from imbue.mngr_vps.instance import extract_single_value_arg
from imbue.mngr_vps.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps.instance import raise_if_vps_migration_arg
from imbue.mngr_vps.primitives import VpsInstanceId

GCP_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("gcp")

# Per-agent metadata is mirrored into instance metadata as up to three items per
# agent, keyed ``mngr-agent-<agent_id>-<field>`` (the agent id lives in the key),
# so a STOPPED instance (no external IP, SSH unreachable) still surfaces its
# agents in discovery and resolves by name. GCE *labels* can't hold this (they
# lowercase and restrict to ``[a-z0-9_-]``, 63 chars), so unlike AWS (EC2 tags)
# the GCP mirror lives in instance *metadata*, whose values are large/permissive.
AGENT_METADATA_PREFIX: Final[str] = "mngr-agent-"
_AGENT_METADATA_FIELDS: Final[tuple[str, ...]] = ("name", "type", "labels")
# GCE statuses in which the guest OS is down (so the SSH-based sweep can't see the
# host) but the instance still exists and its agents must be reconstructed from
# metadata. ``STOPPING`` is included so a host doesn't vanish from discovery
# during the stop transition before it reaches the terminal ``TERMINATED``
# (GCE's name for a stopped -- not deleted -- instance).
_HOST_DOWN_STATES: Final[frozenset[str]] = frozenset({"STOPPING", "TERMINATED"})
# ``mngr-host-name`` metadata holds ``mngr-<host_name>``; strip the prefix to
# recover the host name when reconstructing a stopped host.
_HOST_NAME_PREFIX: Final[str] = "mngr-"

# Self-stopping idle watcher (host-side). Identical mechanism to AWS: the
# in-container activity watcher writes ``IDLE_SENTINEL_FILENAME`` onto the shared
# volume when idle; a host-side systemd ``.path`` unit watches the outer path and
# triggers a oneshot ``.service`` that runs ``shutdown -P now``. On GCE a guest
# poweroff lands the instance in ``TERMINATED`` (stopped, disk preserved, no
# compute billing) by default -- there is no GCE analog to AWS's
# ``InstanceInitiatedShutdownBehavior`` and none is needed (and no IAM/API call).
IDLE_WATCHER_UNIT_NAME: Final[str] = "mngr-gcp-idle-watcher"


def _build_sentinel_shutdown_script(sentinel_in_container: str) -> str:
    """Build the in-container ``shutdown.sh`` that signals idle by touching the sentinel.

    Unlike the base ``VpsProvider`` shutdown script (``kill -TERM 1``, which
    stops only the container), the GCP variant signals idle by touching a sentinel
    file on the shared volume. A host-side systemd path unit observes it and powers
    the whole GCE instance off (a container cannot power off its host).
    """
    return f'#!/bin/bash\ntouch "{sentinel_in_container}"\n'


def _build_idle_watcher_path_unit(sentinel_on_outer: str) -> str:
    """Build the systemd ``.path`` unit that fires when the idle sentinel appears."""
    return (
        "[Unit]\n"
        "Description=Watch for the mngr idle sentinel and stop this GCE instance when idle\n"
        "[Path]\n"
        f"PathExists={sentinel_on_outer}\n"
        f"Unit={IDLE_WATCHER_UNIT_NAME}.service\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _build_idle_watcher_service_unit(sentinel_on_outer: str) -> str:
    """Build the oneshot systemd ``.service`` that powers the host off when idle.

    Runs ``shutdown -P now``; on GCE a guest poweroff stops the instance
    (``TERMINATED``, disk preserved, no compute billing) with no API call. Removes
    the sentinel BEFORE powering off so the rearmed ``.path`` unit does not
    immediately re-stop a just-resumed instance.
    """
    return (
        "[Unit]\n"
        "Description=Power off this instance when mngr signals the host is idle\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh -c 'rm -f {sentinel_on_outer} && shutdown -P now'\n"
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
        raise ProviderUnavailableError(name, str(e)) from e
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
        match parsed:
            case ParsedGcpBuildOptions(spot=spot, image=image):
                pass
            case _:
                raise MngrError(
                    f"GcpProvider._create_vps_instance expected ParsedGcpBuildOptions, "
                    f"got {type(parsed).__name__}. This indicates the parser hook returned a "
                    "non-GCP shape; _parse_build_args must return ParsedGcpBuildOptions."
                )
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

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return external IPs of GCE instances labeled with this provider's name.

        Credentials are guaranteed to be resolvable here: ``build_provider_instance``
        raises ``ProviderUnavailableError`` when ``config.get_credentials_and_resolved_project()``
        fails, so any GcpProvider that reaches this point has working credentials.
        """
        instances = self._list_instances_cached()
        vps_ips: list[str] = []
        for instance in instances:
            main_ip = instance.get("main_ip", "")
            if main_ip:
                vps_ips.append(main_ip)
        return vps_ips

    # =========================================================================
    # Native GCE stop/start (idle-pause + resume)
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        """Stop the agent container *and* the GCE instance, preserving the boot disk.

        The base ``VpsProvider.stop_host`` only stops the inner Docker
        container, leaving the GCE instance running and billing. This override
        additionally calls ``instances.stop`` so a paused GCP agent costs only disk
        storage; the boot disk (and all on-disk state) survives, so ``start_host``
        can resume it. ``create_snapshot`` is ignored -- native GCE stop preserves
        the whole filesystem. The base container-stop + record-write is reused via
        ``super()`` with ``stop_reason=STOPPED`` so the single write marks the host
        STOPPED before the instance (and its volume) goes unreachable. Mirrors
        ``AwsProvider.stop_host``.
        """
        del create_snapshot
        host_id = host.id if isinstance(host, HostInterface) else host
        host_record = self._find_host_record(host_id)
        if host_record is None or host_record.config is None or host_record.vps_ip is None:
            raise HostNotFoundError(self.name, host_id)
        super().stop_host(
            host, create_snapshot=False, timeout_seconds=timeout_seconds, stop_reason=stop_reason or HostState.STOPPED
        )
        with log_span("Stopping GCE instance"):
            self.gcp_client.stop_instance(host_record.config.vps_instance_id)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Resume a stopped GCP agent: start the GCE instance, then its container.

        A stopped GCE instance is not SSH-reachable and (with an ephemeral external
        IP) has no address, so it is located by its ``mngr-host-id`` label, started,
        and its fresh external IP read back. The instance keeps its SSH host keys
        across a stop/start (they live on the boot disk), so we re-point known_hosts
        at the new IP and rewrite the persisted record's ``vps_ip`` before delegating
        the container start to ``super()``. Mirrors ``AwsProvider.start_host``.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            raise HostNotFoundError(self.name, host_id)
        instance_id = VpsInstanceId(instance["id"])
        with log_span("Starting GCE instance"):
            new_ip = self.gcp_client.start_instance(instance_id)
        # The cached instance list predates the start (stale state / no external IP);
        # drop it so any later discovery sees the running instance + new IP.
        self._instances_cache = None
        # Rebind known_hosts to the new IP from mngr's local host keypairs BEFORE
        # connecting -- the instance kept its host keys across the stop/start (they
        # are on the boot disk), but the IP changed and the record can't be read
        # until we can SSH in. The local keypairs are what was injected at create.
        self._rebind_known_hosts_pre_connect(new_ip)
        with log_span("Waiting for VPS SSH after start"):
            self._wait_for_sshd_on_vps(new_ip, timeout_seconds=self.config.ssh_connect_timeout)
        with self._make_outer_for_vps_ip(new_ip) as outer:
            host_store = self._realizer.open_host_store(outer, host_id)
            record = host_store.read_host_record()
            if record is None or record.config is None:
                raise HostNotFoundError(self.name, host_id)
            self._rebind_known_hosts(record, new_ip)
            # Clear any stale idle sentinel so the freshly-resumed instance isn't
            # immediately re-stopped by the systemd path unit (belt-and-suspenders;
            # the self-stop service also removes it when it fires).
            outer.execute_idempotent_command(f"rm -f {self._idle_sentinel_path_on_outer(host_id)}")
            certified = record.certified_host_data
            updated_data = certified.model_copy_update(
                to_update(certified.field_ref().stop_reason, None),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            updated_record = record.model_copy_update(
                to_update(record.field_ref().vps_ip, new_ip),
                to_update(record.field_ref().certified_host_data, updated_data),
            )
            host_store.write_host_record(updated_record)
        # Drop any cached Host bound to the old IP, then seed the record cache so
        # super().start_host()'s _find_host_record returns the rebound record.
        self._evict_cached_host(host_id)
        self._host_record_cache[host_id] = updated_record
        return super().start_host(host_id, snapshot_id)

    def _instances_matching_host_id(self, host_id: HostId) -> list[dict[str, Any]]:
        """Return every cached instance labeled ``mngr-host-id=<host_id>`` (GCE label-encoded)."""
        wanted = f"mngr-host-id={to_gce_label_value(str(host_id))}"
        return [instance for instance in self._list_instances_cached() if wanted in instance.get("tags", ())]

    def _rebind_known_hosts(self, record: VpsHostRecord, new_ip: str) -> None:
        """Re-point local known_hosts at ``new_ip`` using the instance's preserved host keys.

        GCE stop/start keeps the instance's SSH host keys (on the boot disk), so only
        the IP changes. Drop any stale entries for the old IP, then add the new IP
        with the recorded VPS (port 22) and container host keys.
        """
        old_ip = record.vps_ip
        if old_ip is not None and old_ip != new_ip:
            remove_host_from_known_hosts(self._vps_known_hosts_path(), old_ip, 22)
            remove_host_from_known_hosts(self._container_known_hosts_path(), old_ip, self.config.container_ssh_port)
        if record.ssh_host_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._vps_known_hosts_path(),
                hostname=new_ip,
                port=22,
                public_key=record.ssh_host_public_key,
            )
        if record.container_ssh_host_public_key is not None:
            add_host_to_known_hosts(
                known_hosts_path=self._container_known_hosts_path(),
                hostname=new_ip,
                port=self.config.container_ssh_port,
                public_key=record.container_ssh_host_public_key,
            )

    def _rebind_known_hosts_pre_connect(self, new_ip: str) -> None:
        """Add ``new_ip`` to known_hosts using mngr's local, authoritative host keys.

        Runs on resume *before* any SSH connection (the host record, the other key
        source, can't be read until we can connect). The VPS/container host keypairs
        are generated and held locally by mngr and injected at create time, so the
        public keys here are exactly what the resumed instance presents (its host
        keys persist on the boot disk across a stop/start).
        """
        add_host_to_known_hosts(
            known_hosts_path=self._vps_known_hosts_path(),
            hostname=new_ip,
            port=22,
            public_key=self._get_vps_host_keypair()[1],
        )
        add_host_to_known_hosts(
            known_hosts_path=self._container_known_hosts_path(),
            hostname=new_ip,
            port=self.config.container_ssh_port,
            public_key=self._get_container_host_keypair()[1],
        )

    # =========================================================================
    # Self-stopping idle watcher (in-container sentinel + host-side systemd)
    # =========================================================================

    @property
    def _supports_bare_isolation(self) -> bool:
        # GCE supports stop/start, and the bare idle path powers the instance off
        # (which on GCE stops it), so bare placement is supported.
        return True

    def _create_shutdown_script(self, host: Host) -> None:
        """Write an in-container ``shutdown.sh`` that signals idle via a sentinel file.

        The base writes ``kill -TERM 1`` (stops the container); for GCP an idle
        container should stop the whole *instance* (so a paused agent costs only
        disk), but a container cannot power off its host. Instead the in-container
        watcher touches a sentinel on the shared volume; a host-side systemd path
        unit (installed in ``_on_host_finalized``) observes it and powers the host
        off, which on GCE stops the instance. Mirrors ``AwsProvider._create_shutdown_script``.

        A bare placement has no container -- the agent (the VM's root) powers the
        instance off directly -- so it uses the base shutdown script instead.
        """
        if self._realizer.idle_shutdown_stops_host:
            super()._create_shutdown_script(host)
            return
        sentinel_in_container = str(host.host_dir / "commands" / IDLE_SENTINEL_FILENAME)
        shutdown_script = _build_sentinel_shutdown_script(sentinel_in_container)
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the host-side systemd idle watcher that self-stops this instance.

        Best-effort (the base contract says this MUST NOT raise): any failure just
        means no auto-stop on idle (manual ``mngr stop`` still works). Mirrors
        ``AwsProvider._on_host_finalized``.

        A bare placement self-stops the instance directly (its idle ``shutdown.sh``
        runs ``shutdown -P now`` as the VM's root), so the watcher install is
        skipped for bare.
        """
        if self._realizer.idle_shutdown_stops_host:
            return
        try:
            self._install_idle_watcher(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            logger.warning(
                "GCP idle watcher install failed for host {} ({}); the agent will not "
                "auto-stop on idle, but `mngr stop` still works",
                host_id,
                e,
            )

    def _install_idle_watcher(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the systemd path/service idle watcher on the outer host.

        The watcher powers the host off when the in-container idle sentinel appears;
        on GCE a guest poweroff stops the instance (no API/IAM). Returns early (after
        a WARNING) when the host record is missing.
        """
        record = self._find_host_record(host_id)
        if record is None or record.config is None:
            logger.warning(
                "GCP idle watcher: no host record for {}; skipping watcher install (no auto-stop)",
                host_id,
            )
            return
        sentinel_on_outer = self._idle_sentinel_path_on_outer(host_id)
        with log_span("Installing GCP idle self-stop watcher"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.path"),
                    _build_idle_watcher_path_unit(str(sentinel_on_outer)),
                )
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.service"),
                    _build_idle_watcher_service_unit(str(sentinel_on_outer)),
                )
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {IDLE_WATCHER_UNIT_NAME}.path")
        logger.info("GCP idle self-stop watcher installed for host {}", host_id)

    # =========================================================================
    # Offline metadata via instance metadata (so STOPPED hosts list + resolve by name)
    # =========================================================================

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist an agent's record on the host volume *and* mirror it into instance metadata.

        The base writes the authoritative on-volume record (read by SSH-based
        discovery for *running* hosts); this override additionally mirrors a compact
        record into instance metadata so a *stopped* instance (volume unreadable)
        still surfaces its agents and resolves for ``mngr start``. The on-volume
        write is best-effort (``HostNotFoundError`` when stopped). Mirrors
        ``AwsProvider.persist_agent_data`` but writes metadata, not tags.
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; persisting agent data to GCE metadata only", host_id)
        agent_id = agent_data.get("id")
        if agent_id is None:
            logger.warning(
                "Cannot mirror agent data to GCE metadata without an id (name={!r})", agent_data.get("name")
            )
            return
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No GCE instance found for host {}; cannot persist agent metadata", host_id)
            return
        updates, delete_keys = self._agent_metadata_items(str(agent_id), agent_data, instance)
        # One setMetadata round-trip (it is a whole-object read-modify-write) carries
        # both the upserts and the stale deletes, unlike AWS's two tag calls.
        self.gcp_client.set_instance_metadata(VpsInstanceId(instance["id"]), updates, delete_keys)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove the agent's on-volume record *and* its ``mngr-agent-<id>-*`` metadata."""
        try:
            super().remove_persisted_agent_data(host_id, agent_id)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; removing agent data from GCE metadata only", host_id)
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            return
        keys = [f"{AGENT_METADATA_PREFIX}{agent_id}-{field}" for field in _AGENT_METADATA_FIELDS]
        self.gcp_client.set_instance_metadata(VpsInstanceId(instance["id"]), {}, keys)

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the GCE instance's OS is down (STOPPING or TERMINATED).

        GCE power state rides along for free in the instance listing, so this
        needs no extra call. A stopped GCE instance has no external IP and is
        SSH-unreachable.
        """
        return instance.get("state") in _HOST_DOWN_STATES

    def _agent_metadata_value(self, field: str, agent_data: Mapping[str, object]) -> str | None:
        """Render one agent field as a metadata-value string, or ``None`` if absent/empty.

        ``name``/``type`` raw; ``labels`` as compact JSON (empty labels treated as
        absent). Mirrors ``AwsProvider._agent_field_value``.
        """
        if field == "labels":
            labels = agent_data.get("labels")
            return json.dumps(labels, separators=(",", ":")) if labels else None
        value = agent_data.get(field)
        return None if value is None else str(value)

    def _agent_metadata_items(
        self, agent_id: str, agent_data: Mapping[str, object], instance: Mapping[str, Any]
    ) -> tuple[dict[str, str], list[str]]:
        """Compute the ``mngr-agent-<id>-<field>`` metadata to set, and stale keys to delete.

        ``persist_agent_data`` is an upsert sometimes called with a partial record, so
        a field absent from ``agent_data`` means "unchanged" (left alone, NOT removed
        -- deleting it would clobber the ``name`` offline resolve-by-name depends on).
        A field present but rendering empty (e.g. ``labels={}``) is dropped and its
        existing key deleted. GCE metadata values are large, so unlike AWS there is no
        length cap. Mirrors ``AwsProvider._agent_field_tags``.
        """
        updates: dict[str, str] = {}
        delete_keys: list[str] = []
        existing = set(self._metadata_dict(instance))
        for field in _AGENT_METADATA_FIELDS:
            if field not in agent_data:
                continue
            key = f"{AGENT_METADATA_PREFIX}{agent_id}-{field}"
            value = self._agent_metadata_value(field, agent_data)
            if value is not None:
                updates[key] = value
                continue
            # Present but empty (an explicit removal, e.g. labels={}): drop it and
            # delete any existing key so no stale value lingers.
            if key in existing:
                delete_keys.append(key)
        return updates, delete_keys

    def _persisted_agent_dicts_from_instance(self, instance: Mapping[str, Any]) -> list[dict]:
        """Reassemble agent records from this instance's ``mngr-agent-<id>-<field>`` metadata."""
        by_id: dict[str, dict] = {}
        for key, value in self._metadata_dict(instance).items():
            if not key.startswith(AGENT_METADATA_PREFIX):
                continue
            agent_id, sep, field = key[len(AGENT_METADATA_PREFIX) :].rpartition("-")
            if not sep or field not in _AGENT_METADATA_FIELDS:
                continue
            record = by_id.setdefault(agent_id, {"id": agent_id})
            if field == "labels":
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    logger.warning("Skipping unparseable agent labels metadata {!r}", key)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("Skipping agent labels metadata {!r}: value is not a JSON object", key)
                    continue
                record["labels"] = parsed
            else:
                record[field] = value
        return list(by_id.values())

    def _metadata_dict(self, instance: Mapping[str, Any]) -> dict[str, str]:
        """Return the instance's metadata dict from the normalized list shape."""
        metadata = instance.get("metadata", {})
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    def _label_dict_from_normalized(self, instance: Mapping[str, Any]) -> dict[str, str]:
        """Turn the normalized ``["key=value", ...]`` label list into a dict (split on first ``=``)."""
        labels: dict[str, str] = {}
        for kv in instance.get("tags", ()):
            key, sep, value = kv.partition("=")
            if sep:
                labels[key] = value
        return labels

    def _host_name_from_instance(self, instance: Mapping[str, Any]) -> HostName:
        """Recover the host name from the ``mngr-host-name`` metadata (fallback: host-id label)."""
        name = self._metadata_dict(instance).get(HOST_NAME_METADATA_KEY, "")
        if name.startswith(_HOST_NAME_PREFIX):
            return HostName(name[len(_HOST_NAME_PREFIX) :])
        if name:
            return HostName(name)
        return HostName(self._label_dict_from_normalized(instance).get("mngr-host-id", "unknown"))

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED-state DiscoveredHost from an instance's labels + metadata, or None."""
        host_id_str = self._label_dict_from_normalized(instance).get("mngr-host-id")
        if host_id_str is None:
            return None
        return DiscoveredHost(
            host_id=HostId(host_id_str),
            host_name=self._host_name_from_instance(instance),
            provider_name=self.name,
            host_state=HostState.STOPPED,
        )

    def _offline_host_from_instance(self, host_id: HostId, instance: Mapping[str, Any]) -> OfflineHost:
        """Reconstruct a minimal offline host (STOPPED) for a stopped instance."""
        now = datetime.now(timezone.utc)
        created_at = self._created_at_from_labels(self._label_dict_from_normalized(instance), host_id) or now
        certified = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(self._host_name_from_instance(instance)),
            created_at=created_at,
            updated_at=now,
            stop_reason=HostState.STOPPED.value,
        )
        return self._create_offline_host(VpsHostRecord(certified_host_data=certified))

    def _created_at_from_labels(self, labels: Mapping[str, str], host_id: HostId) -> datetime | None:
        """Parse the ``mngr-created-at`` label (``%Y-%m-%dt%H-%M-%S``, UTC), or None on failure.

        ``create_instance`` writes this label in GCE's restricted charset
        (lowercased ``t`` separator, dashes for time), so reconstruct the UTC
        datetime from that exact format. A malformed/externally-edited value yields
        None (the caller falls back to now()).
        """
        raw = labels.get("mngr-created-at")
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%dt%H-%M-%S").replace(tzinfo=timezone.utc)
        except ValueError as e:
            logger.opt(exception=e).warning(
                "Malformed mngr-created-at label {!r} on host {}; falling back to now()", raw, host_id
            )
            return None


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
