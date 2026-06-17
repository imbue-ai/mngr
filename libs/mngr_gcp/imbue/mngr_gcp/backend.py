import os
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.ssh_utils import wait_for_expected_host_key
from imbue.mngr_gcp import hookimpl
from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.client import HOST_NAME_METADATA_KEY
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.startup_script import generate_gce_startup_script
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import extract_git_depth
from imbue.mngr_vps.build_args import extract_presence_flag
from imbue.mngr_vps.build_args import extract_single_value_arg
from imbue.mngr_vps.build_args import raise_if_unknown_provider_arg
from imbue.mngr_vps.build_args import raise_if_vps_migration_arg
from imbue.mngr_vps.instance_offline import KeyValueMirrorVpsProvider
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

# The self-stopping idle watcher (in-container sentinel + host-side systemd
# ``.path``/``.service``) is shared by the base ``OfflineCapableVpsProvider``.
# Identical mechanism to AWS: the GCP oneshot ``.service`` runs ``shutdown -P now``,
# which on GCE lands the instance in ``TERMINATED`` (stopped, disk preserved, no
# compute billing) -- there is no GCE analog to AWS's
# ``InstanceInitiatedShutdownBehavior`` and none is needed (and no IAM/API call).
# GCP does not sync host_dir to an object store, so it inherits the base no-op
# ``_is_host_dir_sync_enabled`` and installs no sync daemon.


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


class GcpProvider(KeyValueMirrorVpsProvider):
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
    # overrides none of those hooks. GCP does not sync host_dir to an object store,
    # so ``_is_host_dir_sync_enabled`` stays the base no-op and nothing is installed.

    # =========================================================================
    # Offline metadata via instance metadata (so STOPPED hosts list + resolve by name)
    # =========================================================================

    def _offline_kv_map(self, instance: Mapping[str, Any]) -> dict[str, str]:
        """Return the instance's GCE metadata dict (the offline mirror) from the normalized list shape."""
        metadata = instance.get("metadata", {})
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    def _host_name_key(self) -> str:
        return HOST_NAME_METADATA_KEY

    def _mirror_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror an agent record into the instance's ``mngr-agent-<id>-*`` GCE metadata.

        GCE metadata (not tags/labels) holds the per-agent mirror so a stopped
        instance still surfaces its agents and resolves for ``mngr start``; the
        shared persist envelope on ``OfflineCapableVpsProvider`` handles the
        authoritative on-volume write.
        """
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No GCE instance found for host {}; cannot persist agent metadata", host_id)
            return
        updates, delete_keys = self._agent_field_items(agent_id, agent_data, instance)
        # One setMetadata round-trip (it is a whole-object read-modify-write) carries
        # both the upserts and the stale deletes, unlike AWS's two tag calls.
        self.gcp_client.set_instance_metadata(VpsInstanceId(instance["id"]), updates, delete_keys)

    def _remove_mirrored_agent_record(self, host_id: HostId, agent_id: str) -> None:
        """Remove the agent's ``mngr-agent-<id>-*`` GCE metadata. Idempotent."""
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
