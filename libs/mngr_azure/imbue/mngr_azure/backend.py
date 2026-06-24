import os
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from typing import Any
from typing import Final

import click
from azure.core.exceptions import AzureError
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import ProviderResourceInfo
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure import hookimpl
from imbue.mngr_azure.cli import azure_cli_group
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.client import HOST_NAME_TAG_KEY
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.state_bucket import BlobStateBucket
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
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.systemd import render_systemd_unit

AZURE_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("azure")


# The self-stopping idle watcher (in-container sentinel + host-side systemd
# ``.path``/``.service``) is shared by the base ``OfflineCapableVpsProvider``.
# Unlike AWS/GCP -- where a guest poweroff stops the instance and halts billing --
# an Azure OS shutdown leaves the VM "Stopped (not deallocated)", STILL billing
# compute. So the Azure ``.service`` runs a script that DEALLOCATES the VM via its
# managed-identity IMDS token + the ARM API (the only in-guest way to halt compute
# billing). If the deallocate is refused (no role assignment -- the
# graceful-degradation path) it just logs and exits: an OS poweroff would not halt
# billing on Azure, so falling back to ``shutdown`` would only strand the VM
# unreachable while it keeps billing.
# Where the host-side deallocate script is installed on the outer VM.
_DEALLOCATE_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-azure-deallocate.sh"


def _build_idle_watcher_service_unit() -> str:
    """Build the oneshot systemd ``.service`` that runs the self-deallocate script when idle."""
    return render_systemd_unit(
        {
            "Unit": [("Description", "Deallocate this Azure VM when mngr signals the host is idle")],
            "Service": [("Type", "oneshot"), ("ExecStart", _DEALLOCATE_SCRIPT_PATH)],
        }
    )


def _build_self_deallocate_script(sentinel_to_remove: str | None) -> str:
    """Build the host-side self-deallocate script that halts this VM's compute billing.

    Fetches the VM's managed-identity token from IMDS (no az CLI needed -- plain
    curl), reads this VM's ARM resource id from IMDS, then POSTs the ARM
    ``deallocate`` action (it returns 202 before the guest is torn down).
    ``curl -f`` makes a 403 (no role assignment -- the graceful-degradation
    config) exit non-zero; the script then just logs and exits non-zero. It
    deliberately does NOT poweroff on failure: an Azure OS shutdown does not halt
    compute billing, so a fallback ``shutdown`` would only strand the VM
    unreachable while it keeps billing.

    ``sentinel_to_remove`` is the idle sentinel the script deletes before
    deallocating, or ``None`` when there is nothing to remove. The container path
    passes the sentinel (a resumed VM must not immediately re-trigger; the ``.path``
    unit re-fires this deallocate next time the watcher re-creates it); the bare
    path runs this directly as the agent's ``shutdown.sh`` -- it has no sentinel, so
    it passes ``None`` and the removal line is omitted.
    """
    token_url = (
        "http://169.254.169.254/metadata/identity/oauth2/token"
        "?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F"
    )
    resource_id_url = "http://169.254.169.254/metadata/instance/compute/resourceId?api-version=2021-02-01&format=text"
    remove_sentinel_line = f'rm -f "{sentinel_to_remove}"\n' if sentinel_to_remove is not None else ""
    return (
        "#!/bin/sh\n"
        "# Installed by mngr (AzureProvider) -- deallocate this VM when idle.\n"
        "set -u\n"
        f"{remove_sentinel_line}"
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


# OAuth scope used to eagerly validate Azure credentials (the Azure Resource Manager
# default scope). Requesting a token for it forces DefaultAzureCredential to authenticate.
_AZURE_MANAGEMENT_SCOPE: Final[str] = "https://management.azure.com/.default"


def _resolve_and_validate_azure_credential(config: AzureProviderConfig) -> Any:
    """Build the Azure credential and force authentication by requesting a management-scope token.

    Raises ``AzureError`` if the credential cannot authenticate.
    """
    credential = config.get_credential()
    credential.get_token(_AZURE_MANAGEMENT_SCOPE)
    return credential


def _azure_not_authorized_error(
    name: ProviderInstanceName, reason: str, short_remediation: str, short_reason: str | None = None
) -> ProviderNotAuthorizedError:
    """Build a ``ProviderNotAuthorizedError`` with Azure-specific, actionable help text.

    The generic unavailable help text tells the user to "start Docker", which is wrong
    advice for a cloud auth/subscription failure. Azure's causes are a missing
    subscription, an unusable credential, or skipped one-time setup -- so we curate the
    guidance accordingly. ``ProviderNotAuthorizedError`` is a ``ProviderUnavailableError``
    subclass, so read paths still treat the provider as unavailable rather than empty.
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
    return ProviderNotAuthorizedError(
        name,
        reason=reason,
        short_remediation=short_remediation,
        user_help_text=help_text,
        short_reason=short_reason,
    )


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


class AzureProvider(OfflineCapableVpsProvider):
    """Azure-specific provider that discovers hosts via the VM list in the resource group."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    azure_client: AzureVpsClient = Field(frozen=True, description="Azure VM API client")
    azure_config: AzureProviderConfig = Field(frozen=True, description="Azure-specific configuration")

    @cached_property
    def _state_bucket(self) -> BlobStateBucket | None:
        """Return the Blob state bucket when account + container actually exist, else None.

        The bucket is the sole source of truth for agent records and the offline
        host record. None means the account/container do not exist yet (``mngr
        azure prepare`` was never run) or the subscription can't be resolved;
        ``_state_store`` then raises an actionable error. A storage error while
        probing existence propagates rather than masquerading as "absent". The
        existence probe runs at most once per provider lifetime (cached). Mirrors
        ``AwsProvider._state_bucket``.
        """
        return self._resolve_existing_state_bucket()

    def _resolve_existing_state_bucket(self) -> BlobStateBucket | None:
        """Build the configured/derived bucket and return it only if it exists.

        Returns None only when the bucket genuinely does not exist (or the
        subscription is unresolvable). An ``account_exists`` / ``container_exists``
        storage error propagates -- the bucket is required, so an inability to
        check is an operational failure, not a silent "no bucket".
        """
        try:
            subscription_id = self.azure_config.get_subscription_id()
        except ValueError as e:
            logger.debug("Could not resolve subscription for the Blob state bucket: {}", e)
            return None
        bucket = self.azure_config.build_state_bucket(subscription_id)
        if not (bucket.account_exists() and bucket.container_exists()):
            logger.debug(
                "Azure state account/container {}/{} does not exist; offline host state is unavailable "
                "(run `mngr azure prepare` to create it)",
                bucket.account_name,
                bucket.container_name,
            )
            return None
        return bucket

    @cached_property
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror: the Blob bucket, or raise when it is absent.

        Delegates to the shared ``_select_bucket_store``, supplying only the resolved
        Blob bucket, its label, and the ``mngr azure prepare`` remediation command.
        The bucket is required: when it does not exist, the helper raises an
        actionable error pointing at ``mngr azure prepare``. Offline ``host_dir``
        reads are a separate, bucket-only feature keyed off ``_state_bucket``.
        """
        return self._select_bucket_store(
            self._state_bucket, store_label="Azure state bucket", prepare_command="mngr azure prepare"
        )

    @cached_property
    def _host_dir_backend(self) -> HostDirBackend:
        """Select the offline host_dir backend once: bucket-backed when enabled + present, else no-op.

        Delegates to the shared ``_select_bucket_host_dir_backend``, supplying the
        resolved Blob bucket and the config's ``is_offline_host_dir_enabled`` flag.
        """
        return self._select_bucket_host_dir_backend(
            self._state_bucket, enabled=self.azure_config.is_offline_host_dir_enabled
        )

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List Azure VMs tagged with this provider's name."""
        return self.azure_client.list_instances(provider_tag=str(self.name))

    def gc_provider_resources(self, dry_run: bool) -> list[ProviderResourceInfo]:
        """Reclaim NIC/public-IP orphans left by failed VM creates (Azure-specific).

        Azure provisions a per-VM public IP + NIC before the VM and reserves them
        for 180s after a capacity-failed create, so they cannot be cleaned up
        synchronously on the failure path. They are reaped here at GC time instead
        of on the next create. Age-gated and best-effort -- see
        ``AzureVpsClient.reclaim_orphaned_network_resources``.
        """
        return self.azure_client.reclaim_orphaned_network_resources(provider_name=self.name, dry_run=dry_run)

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
        spot = self._require_parsed(parsed, ParsedAzureBuildOptions).spot
        # Offline host_dir is operator-driven (captured at `mngr stop`), so no
        # user-assigned identity is attached here; the VM still gets a
        # system-assigned identity (for the idle self-deallocate role).
        return self.azure_client.create_instance(
            label=label,
            region=parsed.region,
            plan=parsed.plan,
            user_data=user_data,
            ssh_key_ids=ssh_key_ids,
            tags=tags,
            spot=spot,
        )

    # The shared ``OfflineCapableVpsProvider._list_provider_vps_hostnames``
    # (cached listing -> non-empty main_ip) covers Azure: a *deallocated* VM keeps
    # its Static IP, so it is still listed and then fails fast over the bounded SSH
    # connect timeout before being reconstructed offline -- see that base method.

    # =========================================================================
    # Deallocate/start (idle-pause + resume) -- the base OfflineCapableVpsProvider
    # owns the orchestration; here we supply the Azure-specific cloud-API hooks
    # plus the static-IP rebind no-ops.
    # =========================================================================

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        with log_span("Deallocating Azure VM"):
            self.azure_client.deallocate_instance(instance_id)

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        with log_span("Starting Azure VM"):
            return self.azure_client.start_instance(instance_id)

    def _rebind_known_hosts(self, record: VpsHostRecord, new_ip: str) -> None:
        """No-op: Azure's Static public IP is unchanged across deallocate/start, so the
        create-time known_hosts entries stay valid -- no rebind is needed."""

    def _rebind_known_hosts_pre_connect(self, host_id: HostId, new_ip: str) -> None:
        """No-op: Azure's Static IP means the known_hosts entry is unchanged across a
        deallocate/start, so no pre-connect rebind is needed."""

    # =========================================================================
    # Self-stopping idle watcher (sentinel + host-side systemd deallocate)
    # =========================================================================

    @property
    def _supports_bare_isolation(self) -> bool:
        # Azure VMs support deallocate/start, and the bare idle path self-deallocates
        # directly (the agent runs the same ARM deallocate the container watcher uses),
        # so bare placement is supported.
        return True

    def _provider_instance_kind(self) -> str:
        return "Azure VM"

    def _write_bare_idle_shutdown_script(self, host: Host) -> None:
        """BARE Azure override: write the ARM self-deallocate script as ``shutdown.sh``.

        A bare placement is the VM's root and has no container. An OS
        ``shutdown -P now`` would not halt Azure compute billing, so the bare path
        must deallocate via ARM like the container watcher does (the role assignment
        in ``_post_finalize_steps`` still applies). There is no sentinel on the bare
        path, so the script is built with ``None``.
        """
        self._write_shutdown_script(host, _build_self_deallocate_script(sentinel_to_remove=None))

    def _idle_watcher_service_unit(self) -> str:
        """Azure override: the oneshot ``.service`` runs the installed ARM self-deallocate script.

        The sentinel removal lives in the deallocate script itself (written by
        ``_prepare_idle_watcher_outer``), since an Azure OS poweroff would not halt
        billing.
        """
        return _build_idle_watcher_service_unit()

    def _prepare_idle_watcher_outer(self, outer: OuterHostInterface, sentinel_on_outer: str) -> None:
        """Azure override: install curl and write the self-deallocate script before the units.

        The self-deallocate script calls the IMDS + ARM API with curl; ensure curl
        is present (idempotent) so idle self-deallocate doesn't silently degrade.
        The script removes ``sentinel_on_outer`` first, then deallocates. The
        ``.path``/``.service`` units the base writes after this point fire this
        script via its installed path.
        """
        outer.execute_idempotent_command(
            "command -v curl >/dev/null 2>&1 || (apt-get update && apt-get install -y curl)"
        )
        outer.write_text_file(
            Path(_DEALLOCATE_SCRIPT_PATH), _build_self_deallocate_script(sentinel_to_remove=sentinel_on_outer)
        )
        outer.execute_idempotent_command(f"chmod +x {_DEALLOCATE_SCRIPT_PATH}")

    def _post_finalize_steps(self, *, host_id: HostId, vps_ip: str) -> list[tuple[str, Callable[[], None]]]:
        """Prepend the Azure self-deallocate role assignment to the shared post-finalize steps.

        A bare placement runs the self-deallocate ARM call directly from its idle
        ``shutdown.sh`` (it is the VM's root), and a container placement's host-side
        watcher does the same -- both need the role assignment, so it runs for both
        shapes. The role assignment degrades gracefully when the operator lacks
        roleAssignments/write (see ``AzureVpsClient.assign_self_deallocate_role``).
        Best-effort like the rest: a failure is logged and the other steps still run.
        """
        del vps_ip
        return [
            (
                "idle self-deallocate is disabled for this host, but `mngr stop` still works",
                lambda: self._assign_self_deallocate_role(host_id),
            )
        ]

    def _assign_self_deallocate_role(self, host_id: HostId) -> None:
        """Assign the ARM self-deallocate role to this host's VM (best-effort body for the step).

        ``assign_self_deallocate_role`` surfaces Azure API failures as ``VpsApiError``
        (a ``MngrError``), but a raw ``AzureError`` could still escape the SDK; wrap
        it so the base post-finalize loop -- which catches only ``MngrError`` -- keeps
        the no-raise contract.
        """
        try:
            instance = self._find_instance_for_host(host_id)
            if instance is not None:
                self.azure_client.assign_self_deallocate_role(str(instance["id"]))
        except AzureError as e:
            raise MngrError(f"Azure self-deallocate role assignment failed: {e}") from e

    # =========================================================================
    # Offline discovery (so DEALLOCATED hosts list + resolve by name from the bucket)
    # =========================================================================

    def _host_name_tag_key(self) -> str:
        # The host name is mirrored into the Azure ``mngr-host-name`` tag (as
        # ``mngr-<host_name>``); the shared ``_offline_discovered_host_from_instance``
        # reads it through here.
        return HOST_NAME_TAG_KEY

    def _remirror_host_name(self, host_record: VpsHostRecord, name: HostName) -> None:
        """Re-stamp the ``mngr-host-name`` VM tag (read by offline discovery) after a rename.

        Merges into the VM's existing tags (does not replace them); the value matches
        create's ``label`` (``mngr-<host_name>``).
        """
        if host_record.config is None:
            return
        self.azure_client.set_instance_tags(
            host_record.config.vps_instance_id, {self._host_name_tag_key(): f"mngr-{name}"}
        )

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
            # rewriting azureProfile.json under us). That is ProviderNotAuthorizedError
            # (a ProviderUnavailableError), NOT ProviderEmptyError: read paths (mngr
            # list) must surface it rather than silently dropping the provider and its
            # agents. Host-creation paths surface this same error to the user.
            raise _azure_not_authorized_error(
                name,
                str(e),
                "set subscription_id, AZURE_SUBSCRIPTION_ID, or run `az account set --subscription <id>`",
                short_reason="Azure subscription not resolved",
            ) from e

        # DefaultAzureCredential constructs lazily and never validates, so eagerly
        # request a management-scope token here to surface an unauthenticated
        # environment *now* (instead of as a confusing API error on the first
        # discovery call), matching how AWS/GCP resolve credentials at construction.
        try:
            credential = _resolve_and_validate_azure_credential(config)
        except AzureError as e:
            # A credential we couldn't obtain/validate leaves Azure's state unknown, so
            # this is unauthorized (surfaced), not empty (silently skipped).
            raise _azure_not_authorized_error(
                name,
                "Azure credentials not available",
                "run `az login` (or set AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET)",
            ) from e

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
