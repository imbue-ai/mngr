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
from azure.core.exceptions import AzureError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

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
from imbue.mngr_azure import hookimpl
from imbue.mngr_azure.cli import azure_cli_group
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.client import HOST_NAME_TAG_KEY
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_vps_docker.container_setup import HOST_DIR_SUBPATH
from imbue.mngr_vps_docker.container_setup import host_volume_name_for
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import open_host_store
from imbue.mngr_vps_docker.instance import OfflineCapableVpsDockerProvider
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus

AZURE_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("azure")

# Per-agent metadata is mirrored onto the VM as up to three tags per agent, keyed
# ``mngr-agent-<agent_id>-<field>`` (the agent id lives in the key), so a
# *deallocated* VM (SSH-unreachable) still surfaces its agents in discovery and
# resolves by name. ``name``/``type`` are stored raw; ``labels`` as compact JSON.
# Mirrors the AWS EC2-tag layout (Azure tags are similarly permissive).
AGENT_TAG_PREFIX: Final[str] = "mngr-agent-"
_AGENT_TAG_FIELDS: Final[tuple[str, ...]] = ("name", "type", "labels")
# Azure tag values are capped at 256 chars; a field whose value overflows is
# dropped, not failed (realistically only ``labels``).
_MAX_TAG_VALUE_LEN: Final[int] = 256
# ``mngr-host-name`` tag holds ``mngr-<host_name>``; strip the prefix on read.
_HOST_NAME_PREFIX: Final[str] = "mngr-"

# Self-stopping idle watcher (host-side). Like AWS/GCP, the in-container activity
# watcher writes ``IDLE_SENTINEL_FILENAME`` onto the shared volume when idle and a
# host-side systemd ``.path`` unit fires a oneshot ``.service``. Unlike AWS/GCP --
# where a guest poweroff stops the instance and halts billing -- an Azure OS
# shutdown leaves the VM "Stopped (not deallocated)", STILL billing compute. So
# the Azure ``.service`` runs a script that DEALLOCATES the VM via its
# managed-identity IMDS token + the ARM API (the only in-guest way to halt compute
# billing). If the deallocate is refused (no role assignment -- the
# graceful-degradation path) it just logs and exits: an OS poweroff would not halt
# billing on Azure, so falling back to ``shutdown`` would only strand the VM
# unreachable while it keeps billing.
IDLE_WATCHER_UNIT_NAME: Final[str] = "mngr-azure-idle-watcher"
IDLE_SENTINEL_FILENAME: Final[str] = "stop-instance-requested"
# Where the host-side deallocate script is installed on the outer VM.
_DEALLOCATE_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-azure-deallocate.sh"


def _build_sentinel_shutdown_script(sentinel_in_container: str) -> str:
    """Build the in-container ``shutdown.sh`` that signals idle by touching the sentinel.

    Unlike the base ``VpsDockerProvider`` script (``kill -TERM 1``, stops only the
    container), the Azure variant touches a sentinel on the shared volume; a
    host-side systemd path unit observes it and deallocates the whole VM (a
    container cannot deallocate its host).
    """
    return f'#!/bin/bash\ntouch "{sentinel_in_container}"\n'


def _build_idle_watcher_path_unit(sentinel_on_outer: str) -> str:
    """Build the systemd ``.path`` unit that fires when the idle sentinel appears."""
    return (
        "[Unit]\n"
        "Description=Watch for the mngr idle sentinel and deallocate this Azure VM when idle\n"
        "[Path]\n"
        f"PathExists={sentinel_on_outer}\n"
        f"Unit={IDLE_WATCHER_UNIT_NAME}.service\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _build_idle_watcher_service_unit() -> str:
    """Build the oneshot systemd ``.service`` that runs the self-deallocate script when idle."""
    return (
        "[Unit]\n"
        "Description=Deallocate this Azure VM when mngr signals the host is idle\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_DEALLOCATE_SCRIPT_PATH}\n"
    )


def _build_self_deallocate_script(sentinel_on_outer: str) -> str:
    """Build the host-side self-deallocate script the idle ``.service`` runs.

    Halts Azure compute billing from inside the guest: fetch the VM's
    managed-identity token from IMDS (no az CLI needed -- plain curl), read this
    VM's ARM resource id from IMDS, then POST the ARM ``deallocate`` action (it
    returns 202 before the guest is torn down). ``curl -f`` makes a 403 (no role
    assignment -- the graceful-degradation config) exit non-zero; the script then
    just logs and exits non-zero. It deliberately does NOT poweroff on failure: an
    Azure OS shutdown does not halt compute billing, so a fallback ``shutdown``
    would only strand the VM unreachable while it keeps billing. The sentinel is
    removed first so a resumed VM does not immediately re-trigger (and so the
    ``.path`` unit re-fires this deallocate next time the watcher re-creates it).
    """
    token_url = (
        "http://169.254.169.254/metadata/identity/oauth2/token"
        "?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F"
    )
    resource_id_url = "http://169.254.169.254/metadata/instance/compute/resourceId?api-version=2021-02-01&format=text"
    return (
        "#!/bin/sh\n"
        "# Installed by mngr (AzureProvider) -- deallocate this VM when idle.\n"
        "set -u\n"
        f'rm -f "{sentinel_on_outer}"\n'
        f'token=$(curl -s -H "Metadata:true" "{token_url}" | grep -o \'"access_token":"[^"]*"\' | cut -d\'"\' -f4)\n'
        f'rid=$(curl -s -H "Metadata:true" "{resource_id_url}")\n'
        'if [ -n "$token" ] && [ -n "$rid" ] && curl -fsS -X POST '
        '-H "Authorization: Bearer $token" -H "Content-Length: 0" '
        '"https://management.azure.com${rid}/deallocate?api-version=2024-07-01"; then\n'
        "    exit 0\n"
        "fi\n"
        # The deallocate failed (no managed-identity token, no role assignment, or
        # ARM unreachable). Log to the journal and exit non-zero. We deliberately do
        # NOT poweroff: an Azure OS shutdown does not halt compute billing, so it
        # would only make the VM unreachable while it keeps billing -- strictly worse
        # than leaving it running and resumable. The sentinel was already removed, so
        # the .path unit re-fires this deallocate when the idle watcher re-creates it
        # on a later cycle (recovering from a transient ARM outage on its own).
        'echo "mngr: self-deallocate refused (missing managed-identity token/role or ARM '
        "unreachable). VM left running and STILL BILLING compute -- grant the deallocate "
        'role or run mngr stop. Will retry on the next idle cycle." >&2\n'
        "exit 1\n"
    )


def _azure_unavailable_error(name: ProviderInstanceName, reason: str) -> ProviderUnavailableError:
    """Build a ``ProviderUnavailableError`` with Azure-specific, actionable help text.

    The generic ``ProviderUnavailableError`` help text tells the user to "start
    Docker", which is wrong advice for a cloud auth/subscription failure. Azure's
    "unavailable" causes are a missing subscription, an unusable credential, or
    skipped one-time setup -- so we curate the guidance accordingly.
    """
    help_text = (
        "Azure could not be reached. Check, in order:\n"
        "  - subscription: set AZURE_SUBSCRIPTION_ID, set `subscription_id` in [providers.azure], "
        "or run `az account set --subscription <id>`;\n"
        "  - credentials: run `az login` (or set AZURE_CLIENT_ID / AZURE_TENANT_ID / "
        "AZURE_CLIENT_SECRET for a service principal);\n"
        "  - one-time setup: run `mngr azure prepare` if you have not yet.\n"
        f"Or disable the provider: mngr config set --scope user providers.{name}.is_enabled false"
    )
    return ProviderUnavailableError(name, reason, user_help_text=help_text)


class ParsedAzureBuildOptions(ParsedVpsBuildOptions):
    """``ParsedVpsBuildOptions`` extended with the Azure-only spot knob.

    Returned by ``AzureProvider._parse_build_args`` and consumed by
    ``AzureProvider._create_vps_instance`` so the ``--azure-spot`` opt-in flows
    through to ``AzureVpsClient.create_instance`` without touching the shared
    ``VpsClientInterface``.
    """

    spot: bool = Field(
        default=False,
        description=(
            "Per-host opt-in for Azure Spot capacity, from the presence-only ``--azure-spot`` build "
            "arg. When True, ``AzureVpsClient.create_instance`` sets priority=Spot, "
            "eviction_policy=Delete, max_price=-1. Azure may reclaim spot VMs on capacity pressure; "
            "the host is deleted, not stopped, on eviction. Opt-in only -- safe for ephemeral / "
            "experimental agents, risky for long-lived ones."
        ),
    )


class AzureProvider(OfflineCapableVpsDockerProvider):
    """Azure-specific provider that discovers hosts via the VM list in the resource group."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    azure_client: AzureVpsClient = Field(frozen=True, description="Azure VM API client")
    azure_config: AzureProviderConfig = Field(frozen=True, description="Azure-specific configuration")

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List Azure VMs tagged with this provider's name."""
        return self.azure_client.list_instances(provider_tag=str(self.name))

    def _validate_provider_args_for_create(self) -> None:
        """Pre-create hook: enforce the pytest safety net, then require the prepared subnet.

        Called by ``create_host`` before the first provider write, so every check
        here fails cleanly with no leaked resources.

        1. Mirror the AWS guard: when ``PYTEST_CURRENT_TEST`` is set, the test
           harness is responsible for configuring a safety net so a killed pytest
           run cannot leak a billing VM. On Azure that net is two-layered --
           cloud-init ``shutdown -P +N`` (from ``auto_shutdown_seconds``) powers
           off the agent, and the conftest session-end orphan scanner
           force-deletes any leaked VM tagged ``mngr-pytest-launched`` older than
           the TTL (derived from the same ``auto_shutdown_seconds``). If it is
           unset, fail closed here rather than silently leak a VM. NB: unlike
           AWS/GCP, an OS ``shutdown -P`` on Azure leaves the VM "Stopped (not
           deallocated)", which still bills for compute -- so on Azure the *cost*
           guarantee comes from the orphan scanner, not from auto_shutdown. The
           guard is still required so the TTL the scanner derives is well-defined.

        2. Require the prepared subnet (created once via ``mngr azure prepare``)
           to already exist. Checking it read-only here -- before ``create_host``
           uploads the SSH key or creates the VM -- means a first-time user who
           hasn't run ``prepare`` gets the clean "run mngr azure prepare" message
           immediately, instead of it surfacing mid-create under a "Host creation
           failed, attempting cleanup..." line. The hot ``create_instance`` path
           resolves the subnet again to build the NIC; this extra GET is cheap
           and is what lets the failure happen early and clean. Mirrors the GCP
           firewall pre-flight. (Note the subnet exists after ``prepare`` even
           when ``allowed_ssh_cidrs`` is empty -- prepare still creates the
           NSG/subnet, just with no SSH allow rule -- so this is not skipped in
           the no-ingress case.)
        """
        if "PYTEST_CURRENT_TEST" in os.environ:
            seconds = self._get_effective_auto_shutdown_seconds()
            if not (seconds and seconds > 0):
                raise MngrError(
                    "Refusing to create an Azure VM during pytest without auto_shutdown_seconds set on "
                    "the Azure provider config. Set [providers.<instance>] auto_shutdown_seconds = <N> "
                    "in the project settings.toml so the session-end orphan scanner has a well-defined "
                    "TTL (and cloud-init schedules 'shutdown -P +N')."
                )
        # Read-only subnet pre-flight. ``resolve_subnet_id`` raises a MngrError
        # pointing at ``mngr azure prepare`` when the subnet is missing.
        self.azure_client.resolve_subnet_id()

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedAzureBuildOptions:
        """Parse Azure-prefixed build args.

        Accepts ``--azure-region=REGION``, ``--azure-vm-size=SIZE``,
        ``--azure-spot`` (presence-only), and the shared ``--git-depth=N``.
        Composed from the shared low-level helpers rather than the convenience
        ``parse_vps_build_args`` because Azure has a knob (spot) beyond region +
        plan.
        """
        args = list(build_args or ())
        region, args = extract_single_value_arg(args, "--azure-region=")
        vm_size, args = extract_single_value_arg(args, "--azure-vm-size=")
        spot, args = extract_presence_flag(args, "--azure-spot")
        git_depth, args = extract_git_depth(args)
        valid_args = (
            "--azure-region=",
            "--azure-vm-size=",
            "--azure-spot",
            "--git-depth=",
        )
        docker_build_args: list[str] = []
        for arg in args:
            raise_if_vps_migration_arg(arg)
            raise_if_unknown_provider_arg(arg, "azure", valid_args)
            docker_build_args.append(arg)
        return ParsedAzureBuildOptions(
            region=region or self.azure_config.default_region,
            plan=vm_size or self.azure_config.default_vm_size,
            spot=spot,
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
        """Azure override: thread the per-host ``spot`` opt-in into ``AzureVpsClient.create_instance``.

        Calls through ``self.azure_client`` (the concrete typed client) rather
        than the shared ``self.vps_client`` interface so the Azure-only ``spot``
        kwarg is statically visible.
        """
        match parsed:
            case ParsedAzureBuildOptions(spot=spot):
                pass
            case _:
                raise MngrError(
                    f"AzureProvider._create_vps_instance expected ParsedAzureBuildOptions, "
                    f"got {type(parsed).__name__}. This indicates the parser hook returned a "
                    "non-Azure shape; _parse_build_args must return ParsedAzureBuildOptions."
                )
        return self.azure_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
            spot=spot,
        )

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return public IPs of Azure VMs tagged with this provider's name.

        Credentials are guaranteed resolvable here: ``build_provider_instance``
        raises ``ProviderUnavailableError`` when ``config.get_subscription_id()``
        fails, so any AzureProvider that reaches this point has a subscription.

        Note: Azure allocates the public IP ``Static``, so a *deallocated* VM keeps
        its IP (unlike a stopped GCE/EC2 instance, which loses its ephemeral IP and
        is thus naturally excluded by the ``if main_ip`` check below). We
        deliberately do NOT special-case deallocated VMs out here: the shared
        discovery probe applies a bounded SSH connect timeout (pyinfra's
        ``CONNECT_TIMEOUT``, 10s), so an unreachable VM fails fast and is surfaced
        offline -- the same path a crashed-but-still-"running" host takes. The
        deallocated host is then reconstructed from tags in
        ``discover_hosts_and_agents``. This keeps discovery uniform (no
        power-state-specific branch) at the cost of one bounded timeout when a
        paused VM is present.
        """
        instances = self._list_instances_cached()
        vps_ips: list[str] = []
        for instance in instances:
            main_ip = instance.get("main_ip", "")
            if main_ip:
                vps_ips.append(main_ip)
        return vps_ips

    # =========================================================================
    # Deallocate/start (idle-pause + resume)
    # =========================================================================

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
        stop_reason: HostState | None = None,
    ) -> None:
        """Stop the agent container *and* deallocate the Azure VM, halting compute billing.

        The base ``VpsDockerProvider.stop_host`` only stops the inner Docker
        container, leaving the VM allocated and billing. This override additionally
        *deallocates* the VM (NOT a mere OS shutdown, which on Azure leaves the VM
        "Stopped (not deallocated)" still billing compute), so a paused Azure agent
        costs only OS-disk storage; the disk and all state survive for
        ``start_host``. ``create_snapshot`` is ignored. Mirrors
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
        with log_span("Deallocating Azure VM"):
            self.azure_client.deallocate_instance(host_record.config.vps_instance_id)

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        """Resume a deallocated Azure agent: start the VM, then its container.

        A deallocated VM is located by its ``mngr-host-id`` tag (it is SSH-
        unreachable). Azure allocates the public IP ``Static``, so the IP is
        PRESERVED across deallocate/start and the SSH host keys persist on the OS
        disk -- so, unlike AWS/GCP, no known_hosts rebind is needed. We just start
        the VM, clear the idle sentinel and ``stop_reason``, and delegate the
        container start to ``super()``.
        """
        host_id = host.id if isinstance(host, HostInterface) else host
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            raise HostNotFoundError(self.name, host_id)
        instance_id = VpsInstanceId(instance["id"])
        with log_span("Starting Azure VM"):
            vps_ip = self.azure_client.start_instance(instance_id)
        # The cached instance list predates the start (stale power state); drop it.
        self._instances_cache = None
        with log_span("Waiting for VPS SSH after start"):
            self._wait_for_sshd_on_vps(vps_ip, timeout_seconds=self.config.ssh_connect_timeout)
        with self._make_outer_for_vps_ip(vps_ip) as outer:
            host_store = open_host_store(outer, host_volume_name_for(host_id))
            record = host_store.read_host_record()
            if record is None or record.config is None:
                raise HostNotFoundError(self.name, host_id)
            # Clear any stale idle sentinel so the freshly-resumed VM isn't
            # immediately re-deallocated by the systemd path unit (belt-and-
            # suspenders; the self-deallocate script also removes it when it fires).
            outer.execute_idempotent_command(f"rm -f {self._idle_sentinel_path_on_outer(host_id)}")
            certified = record.certified_host_data
            updated_data = certified.model_copy_update(
                to_update(certified.field_ref().stop_reason, None),
                to_update(certified.field_ref().updated_at, datetime.now(timezone.utc)),
            )
            # vps_ip is unchanged (static IP), but write it back for robustness.
            updated_record = record.model_copy_update(
                to_update(record.field_ref().vps_ip, vps_ip),
                to_update(record.field_ref().certified_host_data, updated_data),
            )
            host_store.write_host_record(updated_record)
        self._evict_cached_host(host_id)
        self._host_record_cache[host_id] = updated_record
        return super().start_host(host_id, snapshot_id)

    def _find_instance_for_host(self, host_id: HostId) -> dict[str, Any] | None:
        """Locate this host's VM by its ``mngr-host-id`` tag (works while deallocated).

        Reads only the VM list tags (no SSH), so it resolves a deallocated VM.
        Refuses (raises) when more than one VM carries the same ``mngr-host-id``.
        Mirrors ``AwsProvider._find_instance_for_host``.
        """
        matches = self._instances_matching_host_id(host_id)
        if not matches:
            self._instances_cache = None
            matches = self._instances_matching_host_id(host_id)
        if len(matches) > 1:
            ids = sorted(str(m.get("id")) for m in matches)
            raise MngrError(
                f"Azure provider {self.name!r}: {len(matches)} VMs are tagged "
                f"mngr-host-id={host_id} ({', '.join(ids)}); refusing to act on an ambiguous match. "
                "Resolve the duplicate tags (or delete the stray VM) and retry."
            )
        return matches[0] if matches else None

    def _instances_matching_host_id(self, host_id: HostId) -> list[dict[str, Any]]:
        """Return every cached VM tagged ``mngr-host-id=<host_id>``."""
        wanted = f"mngr-host-id={host_id}"
        return [instance for instance in self._list_instances_cached() if wanted in instance.get("tags", ())]

    # =========================================================================
    # Self-stopping idle watcher (sentinel + host-side systemd deallocate)
    # =========================================================================

    def _create_shutdown_script(self, host: Host) -> None:
        """Write an in-container ``shutdown.sh`` that signals idle via a sentinel file.

        For Azure an idle container should *deallocate* the whole VM (so a paused
        agent costs only disk), but a container cannot deallocate its host. The
        in-container watcher touches a sentinel; a host-side systemd path unit
        (installed in ``_on_host_finalized``) observes it and runs the
        self-deallocate script. Mirrors ``AwsProvider._create_shutdown_script``.
        """
        sentinel_in_container = str(host.host_dir / "commands" / IDLE_SENTINEL_FILENAME)
        shutdown_script = _build_sentinel_shutdown_script(sentinel_in_container)
        commands_dir = host.host_dir / "commands"
        host.execute_idempotent_command(f"mkdir -p {commands_dir}")
        host.write_file(commands_dir / "shutdown.sh", shutdown_script.encode())
        host.execute_idempotent_command(f"chmod +x {commands_dir / 'shutdown.sh'}")

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Assign the self-deallocate role and install the host-side idle watcher.

        Best-effort (the base contract says this MUST NOT raise): a failure just
        means no idle self-deallocate (manual ``mngr stop`` still works). The role
        assignment degrades gracefully when the operator lacks
        roleAssignments/write (see ``AzureVpsClient.assign_self_deallocate_role``).
        """
        # The base contract says this hook MUST NOT raise, so guard the whole
        # role-assignment path: _find_instance_for_host raises MngrError on an
        # ambiguous host-id match, and assign_self_deallocate_role surfaces Azure
        # API failures as VpsApiError (a MngrError, not an AzureError).
        try:
            instance = self._find_instance_for_host(host_id)
            if instance is not None:
                self.azure_client.assign_self_deallocate_role(str(instance["id"]))
        except (MngrError, AzureError) as e:
            logger.warning(
                "Could not assign the self-deallocate role for host {} ({}); idle self-deallocate "
                "is disabled for this host, but `mngr stop` still works",
                host_id,
                e,
            )
        try:
            self._install_idle_watcher(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            logger.warning(
                "Azure idle watcher install failed for host {} ({}); the agent will not "
                "auto-stop on idle, but `mngr stop` still works",
                host_id,
                e,
            )

    def _install_idle_watcher(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the self-deallocate script + systemd path/service idle watcher on the outer host."""
        record = self._find_host_record(host_id)
        if record is None or record.config is None:
            logger.warning(
                "Azure idle watcher: no host record for {}; skipping watcher install (no auto-stop)",
                host_id,
            )
            return
        sentinel_on_outer = self._idle_sentinel_path_on_outer(host_id)
        with log_span("Installing Azure idle self-deallocate watcher"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                # The self-deallocate script calls the IMDS + ARM API with curl;
                # ensure it's present (idempotent -- a no-op when already installed)
                # so idle self-deallocate doesn't silently degrade to a poweroff.
                outer.execute_idempotent_command(
                    "command -v curl >/dev/null 2>&1 || (apt-get update && apt-get install -y curl)"
                )
                outer.write_text_file(
                    Path(_DEALLOCATE_SCRIPT_PATH), _build_self_deallocate_script(str(sentinel_on_outer))
                )
                outer.execute_idempotent_command(f"chmod +x {_DEALLOCATE_SCRIPT_PATH}")
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.path"),
                    _build_idle_watcher_path_unit(str(sentinel_on_outer)),
                )
                outer.write_text_file(
                    Path(f"/etc/systemd/system/{IDLE_WATCHER_UNIT_NAME}.service"),
                    _build_idle_watcher_service_unit(),
                )
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {IDLE_WATCHER_UNIT_NAME}.path")
        logger.info("Azure idle self-deallocate watcher installed for host {}", host_id)

    def _idle_sentinel_path_on_outer(self, host_id: HostId) -> Path:
        """Outer-filesystem path of the in-container idle sentinel for this host."""
        return (
            self.config.btrfs_mount_path
            / host_id.get_uuid().hex
            / HOST_DIR_SUBPATH
            / "commands"
            / IDLE_SENTINEL_FILENAME
        )

    # =========================================================================
    # Offline metadata via VM tags (so DEALLOCATED hosts list + resolve by name)
    # =========================================================================

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist an agent's record on the host volume *and* mirror it into VM tags.

        Mirrors ``AwsProvider.persist_agent_data``: the base writes the
        authoritative on-volume record (read by SSH-based discovery for *running*
        hosts -- best-effort, raises ``HostNotFoundError`` when deallocated), and
        this override mirrors a compact record into VM tags so a *deallocated* VM
        still surfaces its agents and resolves for ``mngr start``.
        """
        try:
            super().persist_agent_data(host_id, agent_data)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; persisting agent data to Azure tags only", host_id)
        agent_id = agent_data.get("id")
        if agent_id is None:
            logger.warning("Cannot mirror agent data to Azure tags without an id (name={!r})", agent_data.get("name"))
            return
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No Azure VM found for host {}; cannot persist agent tags", host_id)
            return
        set_tags, delete_keys = self._agent_field_tags(str(agent_id), agent_data, instance)
        self.azure_client.add_tags(VpsInstanceId(instance["id"]), set_tags)
        if delete_keys:
            self.azure_client.remove_tags(VpsInstanceId(instance["id"]), delete_keys)

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove the agent's on-volume record *and* its ``mngr-agent-<id>-*`` tags."""
        try:
            super().remove_persisted_agent_data(host_id, agent_id)
        except HostNotFoundError:
            logger.debug("Host {} unreachable; removing agent data from Azure tags only", host_id)
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            return
        keys = [f"{AGENT_TAG_PREFIX}{agent_id}-{field}" for field in _AGENT_TAG_FIELDS]
        self.azure_client.remove_tags(VpsInstanceId(instance["id"]), keys)

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        """Whether the VM is halted (stopped/deallocated, and their in-flight transitions).

        Azure's VM list cannot carry power state (``expand=instanceView`` is
        rejected on a resource-group list), so -- unlike AWS/GCP, which get state
        for free in the listing -- this confirms the halt with a per-VM
        ``get_instance_status`` call. The base discovery loop calls this only for
        mngr VMs the SSH sweep did NOT surface (and after the dedup filter), so a
        healthy ``mngr list`` makes zero extra calls, and a not-online VM that is
        still ``running`` (transient SSH failure, mid-boot, firewall) is not
        misreported as STOPPED.
        """
        return self.azure_client.get_instance_status(VpsInstanceId(instance["id"])) == VpsInstanceStatus.HALTED

    def _agent_field_value(self, field: str, agent_data: Mapping[str, object]) -> str | None:
        """Render one agent field as a tag-value string, or ``None`` if absent/empty.

        ``name``/``type`` raw; ``labels`` as compact JSON (empty labels treated as
        absent). Mirrors ``AwsProvider._agent_field_value``.
        """
        if field == "labels":
            labels = agent_data.get("labels")
            return json.dumps(labels, separators=(",", ":")) if labels else None
        value = agent_data.get(field)
        return None if value is None else str(value)

    def _agent_field_tags(
        self, agent_id: str, agent_data: Mapping[str, object], instance: Mapping[str, Any]
    ) -> tuple[dict[str, str], list[str]]:
        """Compute the ``mngr-agent-<id>-<field>`` tags to set, and stale ones to delete.

        ``persist_agent_data`` is an upsert sometimes called with a partial record,
        so a field absent from ``agent_data`` is left alone (NOT removed -- deleting
        it would clobber the ``name`` offline resolve-by-name depends on). A field
        present but empty (e.g. ``labels={}``) or over the 256-char Azure tag limit
        (realistically only ``labels``) is dropped and its existing tag deleted.
        Mirrors ``AwsProvider._agent_field_tags``.
        """
        set_tags: dict[str, str] = {}
        delete_keys: list[str] = []
        existing = set(self._tag_dict_from_normalized(instance))
        for field in _AGENT_TAG_FIELDS:
            if field not in agent_data:
                continue
            key = f"{AGENT_TAG_PREFIX}{agent_id}-{field}"
            value = self._agent_field_value(field, agent_data)
            if value is not None and len(value) <= _MAX_TAG_VALUE_LEN:
                set_tags[key] = value
                continue
            if value is not None:
                logger.warning(
                    "Agent {} {} ({} chars) exceeds the {}-char Azure tag limit; omitted from the "
                    "stopped-host tag mirror",
                    agent_data.get("name", agent_id),
                    field,
                    len(value),
                    _MAX_TAG_VALUE_LEN,
                )
            if key in existing:
                delete_keys.append(key)
        return set_tags, delete_keys

    def _persisted_agent_dicts_from_instance(self, instance: Mapping[str, Any]) -> list[dict]:
        """Reassemble agent records from this VM's ``mngr-agent-<id>-<field>`` tags.

        Mirrors ``AwsProvider._persisted_agent_dicts_from_instance`` (ids may
        contain dashes, so split on the final ``-``).
        """
        by_id: dict[str, dict] = {}
        for key, value in self._tag_dict_from_normalized(instance).items():
            if not key.startswith(AGENT_TAG_PREFIX):
                continue
            agent_id, sep, field = key[len(AGENT_TAG_PREFIX) :].rpartition("-")
            if not sep or field not in _AGENT_TAG_FIELDS:
                continue
            record = by_id.setdefault(agent_id, {"id": agent_id})
            if field == "labels":
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    logger.warning("Skipping unparseable agent labels tag {!r}", key)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("Skipping agent labels tag {!r}: value is not a JSON object", key)
                    continue
                record["labels"] = parsed
            else:
                record[field] = value
        return list(by_id.values())

    def _tag_dict_from_normalized(self, instance: Mapping[str, Any]) -> dict[str, str]:
        """Turn the normalized ``["key=value", ...]`` tag list into a dict (split on first ``=``)."""
        tags: dict[str, str] = {}
        for kv in instance.get("tags", ()):
            key, sep, value = kv.partition("=")
            if sep:
                tags[key] = value
        return tags

    def _host_name_from_tags(self, tags: Mapping[str, str]) -> HostName:
        """Recover the host name from the ``mngr-host-name=mngr-<name>`` tag (fallback: host-id)."""
        name_tag = tags.get(HOST_NAME_TAG_KEY, "")
        if name_tag.startswith(_HOST_NAME_PREFIX):
            return HostName(name_tag[len(_HOST_NAME_PREFIX) :])
        if name_tag:
            return HostName(name_tag)
        return HostName(tags.get("mngr-host-id", "unknown"))

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        """Build a STOPPED-state DiscoveredHost from a VM's tags, or None if not a mngr host."""
        tags = self._tag_dict_from_normalized(instance)
        host_id_str = tags.get("mngr-host-id")
        if host_id_str is None:
            return None
        return DiscoveredHost(
            host_id=HostId(host_id_str),
            host_name=self._host_name_from_tags(tags),
            provider_name=self.name,
            host_state=HostState.STOPPED,
        )

    def _offline_host_from_instance(self, host_id: HostId, instance: Mapping[str, Any]) -> OfflineHost:
        """Reconstruct a minimal offline host (STOPPED) for a deallocated VM from its tags."""
        tags = self._tag_dict_from_normalized(instance)
        now = datetime.now(timezone.utc)
        created_at = now
        created_at_raw = tags.get("mngr-created-at")
        if created_at_raw:
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except ValueError as e:
                logger.opt(exception=e).warning(
                    "Malformed mngr-created-at tag {!r} on host {}; falling back to now()",
                    created_at_raw,
                    host_id,
                )
        certified = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(self._host_name_from_tags(tags)),
            created_at=created_at,
            updated_at=now,
            stop_reason=HostState.STOPPED.value,
        )
        return self._create_offline_host(VpsDockerHostRecord(certified_host_data=certified))


class AzureProviderBackend(ProviderBackendInterface):
    """Backend for creating Azure VM VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return AZURE_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on Azure Virtual Machines"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return AzureProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "Azure-specific args (consumed by provider, not passed to docker):\n"
            "  --azure-region=REGION       Azure region / location (default: westus)\n"
            "  --azure-vm-size=SIZE        Azure VM size (default: Standard_B2s)\n"
            "  --azure-spot                Run on Azure Spot capacity (presence-only flag).\n"
            "                              Azure may reclaim on capacity pressure; the host is\n"
            "                              deleted, not stopped, on eviction. Opt-in only.\n"
            "  --git-depth=N               Shallow-clone build context to depth N before upload\n"
            "\n"
            "All other build args are passed to 'docker build' on the VM.\n"
            "Example: -b --azure-vm-size=Standard_D2s_v5 -b --file=Dockerfile -b .\n"
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
        if not isinstance(config, AzureProviderConfig):
            raise MngrError(f"Expected AzureProviderConfig, got {type(config).__name__}")

        try:
            subscription_id = config.get_subscription_id()
        except ValueError as e:
            # A missing/unresolvable subscription means Azure was never reached:
            # the state is *unknown* (agents may well exist on a configured
            # subscription we transiently couldn't read -- e.g. the az CLI
            # rewriting azureProfile.json under us). That is ProviderUnavailableError,
            # NOT ProviderEmptyError: read paths (mngr list) must surface a warning
            # rather than silently dropping the provider and its agents from the
            # listing. Host-creation paths surface this same error to the user.
            raise _azure_unavailable_error(name, str(e)) from e

        try:
            credential = config.get_credential()
        except AzureError as e:
            # Same rationale: a credential we couldn't obtain leaves Azure's state
            # unknown, so this is unavailable (warned), not empty (silently skipped).
            raise _azure_unavailable_error(name, str(e)) from e

        azure_client = AzureVpsClient(
            credential=credential,
            subscription_id=subscription_id,
            region=config.default_region,
            resource_group=config.resource_group,
            vnet_name=config.vnet_name,
            subnet_name=config.subnet_name,
            nsg_name=config.nsg_name,
            vnet_address_prefix=config.vnet_address_prefix,
            subnet_address_prefix=config.subnet_address_prefix,
            vm_size=config.default_vm_size,
            image_publisher=config.image_publisher,
            image_offer=config.image_offer,
            image_sku=config.image_sku,
            image_version=config.image_version,
            admin_username=config.admin_username,
            os_disk_size_gb=config.os_disk_size_gb,
            os_disk_type=config.os_disk_type,
            allowed_ssh_cidrs=config.allowed_ssh_cidrs,
            associate_public_ip=config.associate_public_ip,
            container_ssh_port=config.container_ssh_port,
        )

        return AzureProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=azure_client,
            azure_client=azure_client,
            azure_config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the Azure provider backend."""
    return (AzureProviderBackend, AzureProviderConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr azure ...`` operator command group."""
    return [azure_cli_group]
