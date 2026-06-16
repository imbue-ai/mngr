import os
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
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import ProviderResourceInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure import hookimpl
from imbue.mngr_azure.cli import azure_cli_group
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.client import HOST_NAME_TAG_KEY
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.state_bucket import BlobStateBucket
from imbue.mngr_azure.state_bucket import BlobStateBucketError
from imbue.mngr_azure.state_bucket import BlobStateHostIdentity
from imbue.mngr_azure.state_bucket import BlobStateHostIdentityError
from imbue.mngr_azure.state_bucket import host_dir_blob_prefix_for
from imbue.mngr_vps_docker.host_state_store import BucketHostStateStore
from imbue.mngr_vps_docker.host_state_store import HostStateStore
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.instance import AGENT_TAG_FIELDS
from imbue.mngr_vps_docker.instance import AGENT_TAG_PREFIX
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import TagMirrorVpsDockerProvider
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.instance import extract_git_depth
from imbue.mngr_vps_docker.instance import extract_presence_flag
from imbue.mngr_vps_docker.instance import extract_single_value_arg
from imbue.mngr_vps_docker.instance import raise_if_unknown_provider_arg
from imbue.mngr_vps_docker.instance import raise_if_vps_migration_arg
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus

AZURE_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("azure")

# Self-stopping idle watcher (host-side). The shared sentinel script + ``.path``
# unit come from ``OfflineCapableVpsDockerProvider``; Azure differs only in the
# ``.service`` action. Unlike AWS/GCP -- where a guest poweroff stops the instance
# and halts billing -- an Azure OS shutdown leaves the VM "Stopped (not
# deallocated)", STILL billing compute. So the Azure ``.service`` runs a script
# that DEALLOCATES the VM via its managed-identity IMDS token + the ARM API (the
# only in-guest way to halt compute billing). If the deallocate is refused (no
# role assignment -- the graceful-degradation path) it just logs and exits: an OS
# poweroff would not halt billing on Azure, so falling back to ``shutdown`` would
# only strand the VM unreachable while it keeps billing.
# Where the host-side deallocate script is installed on the outer VM.
_DEALLOCATE_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-azure-deallocate.sh"

# Host-side host_dir sync daemon (Component 3 of specs/provider-state-bucket).
# When ``is_host_dir_synced_to_bucket`` is on and a state bucket exists, the
# create path attaches the prepare-provisioned user-assigned managed identity,
# then installs (over SSH on the outer) a systemd oneshot ``.service`` + ``.timer``
# pair: every ``HOST_DIR_SYNC_INTERVAL_SECONDS`` the oneshot runs ``azcopy sync``
# of ``<host_dir_on_outer>`` to the blob container's ``hosts/<id>/host_dir/``
# prefix, authenticating azcopy as the VM's user-assigned identity via MSI (no
# long-lived keys on the box). The same oneshot is triggered once on graceful
# stop (``stop_host``) so the offline copy is current. Offline reads are served
# from the bucket by the operator's credentials via ``get_volume_for_host``.
# Mirrors the AWS ``aws s3 sync`` daemon.
HOST_DIR_SYNC_UNIT_NAME: Final[str] = "mngr-azure-host-dir-sync"
HOST_DIR_SYNC_INTERVAL_SECONDS: Final[int] = 60
# host_dir can contain large transient build artifacts; exclude the obvious ones
# so a periodic full-tree sync stays cheap. azcopy distinguishes file-NAME globs
# (``--exclude-pattern``) from directory PATH prefixes (``--exclude-path``), so the
# file glob and the directory trees go on different flags -- a single
# ``--exclude-pattern`` would only match files literally named ``__pycache__`` /
# ``node_modules``, not their trees. Matches the effective AWS exclude set.
_HOST_DIR_SYNC_EXCLUDE_PATTERNS: Final[tuple[str, ...]] = ("*.tmp",)
_HOST_DIR_SYNC_EXCLUDE_PATHS: Final[tuple[str, ...]] = ("__pycache__", "node_modules")


def _build_host_dir_sync_command(host_dir_on_outer: str, blob_prefix_url: str) -> str:
    """Build the ``azcopy sync ... --delete-destination`` command the oneshot service runs.

    Syncs the per-host ``host_dir`` tree to the ``hosts/<id>/host_dir/`` blob
    prefix, with ``--delete-destination=true`` so a removed file is removed offline
    too (the ``--delete`` analog). Large transient caches are excluded: file-name
    globs via ``--exclude-pattern`` and whole directory trees via ``--exclude-path``
    (azcopy treats the two differently -- a pattern only matches a file's name).
    azcopy authenticates as the VM's *user-assigned* managed identity via MSI; the
    identity is pinned by ``AZCOPY_AUTO_LOGIN_TYPE``/``AZCOPY_MSI_CLIENT_ID`` set in
    the service unit's environment (not on the command line), since the VM also
    carries a system-assigned identity.
    """
    exclude_patterns = ";".join(_HOST_DIR_SYNC_EXCLUDE_PATTERNS)
    exclude_paths = ";".join(_HOST_DIR_SYNC_EXCLUDE_PATHS)
    return (
        f'azcopy sync "{host_dir_on_outer}" "{blob_prefix_url}" '
        f"--recursive --delete-destination=true "
        f'--exclude-pattern "{exclude_patterns}" --exclude-path "{exclude_paths}"'
    )


def _build_host_dir_sync_service_unit(host_dir_on_outer: str, blob_prefix_url: str, identity_client_id: str) -> str:
    """Build the oneshot systemd ``.service`` that pushes host_dir to the bucket once.

    Triggered periodically by the paired ``.timer`` and once on graceful stop.
    ``Type=oneshot`` so a stop-time ``systemctl start`` blocks until the sync
    completes (the offline copy is current before the VM deallocates). The MSI
    login env pins azcopy to the bucket-write user-assigned identity.
    """
    command = _build_host_dir_sync_command(host_dir_on_outer, blob_prefix_url)
    return (
        "[Unit]\n"
        "Description=Sync this host's host_dir to the mngr Azure Blob state bucket for offline reads\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Environment=AZCOPY_AUTO_LOGIN_TYPE=MSI\n"
        f"Environment=AZCOPY_MSI_CLIENT_ID={identity_client_id}\n"
        f"ExecStart=/bin/sh -c '{command}'\n"
    )


def _build_host_dir_sync_timer_unit(interval_seconds: int) -> str:
    """Build the systemd ``.timer`` that fires the host_dir sync every ``interval_seconds``.

    ``OnBootSec`` gives the host a moment to finish bootstrapping before the first
    sync; ``OnUnitActiveSec`` then repeats at the interval. Mirrors the AWS timer.
    """
    return (
        "[Unit]\n"
        "Description=Periodically sync this host's host_dir to the mngr Azure Blob state bucket\n"
        "[Timer]\n"
        f"OnBootSec={interval_seconds}\n"
        f"OnUnitActiveSec={interval_seconds}\n"
        f"Unit={HOST_DIR_SYNC_UNIT_NAME}.service\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _build_azcopy_install_command() -> str:
    """Build the best-effort azcopy install command (no-op when already present).

    Installs the azcopy v10 binary from Microsoft's static download into
    ``/usr/local/bin`` only when ``azcopy`` is not already on PATH, so a re-run or
    a baked image is a no-op. Uses curl + tar (both present on the Debian image
    after the base cloud-init). Mirrors the AWS awscli best-effort install.
    """
    return (
        "command -v azcopy >/dev/null 2>&1 || ("
        "command -v curl >/dev/null 2>&1 || (apt-get update && apt-get install -y curl) && "
        "tmp=$(mktemp -d) && "
        'curl -fsSL "https://aka.ms/downloadazcopy-v10-linux" -o "$tmp/azcopy.tgz" && '
        'tar -xzf "$tmp/azcopy.tgz" -C "$tmp" && '
        'install -m 0755 "$tmp"/azcopy_linux_*/azcopy /usr/local/bin/azcopy && '
        'rm -rf "$tmp")'
    )


def _build_host_dir_blob_url(account_name: str, container_name: str, host_id: HostId) -> str:
    """Return the ``https://<account>.blob.core.windows.net/<container>/hosts/<id>/host_dir`` sync target."""
    prefix = host_dir_blob_prefix_for(host_id).rstrip("/")
    return f"https://{account_name}.blob.core.windows.net/{container_name}/{prefix}"


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


class AzureProvider(TagMirrorVpsDockerProvider):
    """Azure-specific provider that discovers hosts via the VM list in the resource group."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    azure_client: AzureVpsClient = Field(frozen=True, description="Azure VM API client")
    azure_config: AzureProviderConfig = Field(frozen=True, description="Azure-specific configuration")

    def _host_name_tag_key(self) -> str:
        return HOST_NAME_TAG_KEY

    @cached_property
    def _state_bucket(self) -> BlobStateBucket | None:
        """Return the Blob state bucket when account + container actually exist, else None.

        When present, the bucket is the source of truth for agent records and the
        offline host record (replacing the VM tag mirror); when None (the
        storage account / container do not yet exist because ``mngr azure prepare``
        was never run, or the subscription can't be resolved), mngr falls back to
        the per-agent tag mirror. The existence probe runs at most once per
        provider lifetime (cached). Mirrors ``AwsProvider._state_bucket``.
        """
        return self._resolve_existing_state_bucket()

    def _resolve_existing_state_bucket(self) -> BlobStateBucket | None:
        """Build the configured/derived bucket and return it only if it exists."""
        try:
            subscription_id = self.azure_config.get_subscription_id()
        except ValueError as e:
            logger.debug("Could not resolve subscription for the Blob state bucket; using the VM tag mirror: {}", e)
            return None
        bucket = self.azure_config.build_state_bucket(subscription_id)
        try:
            if not (bucket.account_exists() and bucket.container_exists()):
                logger.debug(
                    "Azure state account/container {}/{} does not exist; using the VM tag mirror "
                    "(run `mngr azure prepare` to create it)",
                    bucket.account_name,
                    bucket.container_name,
                )
                return None
        except BlobStateBucketError as e:
            logger.warning(
                "Could not check Azure state bucket {}; falling back to VM tags: {}", bucket.account_name, e
            )
            return None
        return bucket

    @cached_property
    def _state_store(self) -> HostStateStore:
        """The external host/agent-record mirror: the Blob bucket when present, else the VM tag mirror.

        Selecting one store here lets the persist / remove / list / read paths
        below stop branching on bucket-vs-tags. Offline ``host_dir`` reads are a
        separate, bucket-only feature and stay keyed off ``_state_bucket``. Mirrors
        ``AwsProvider._state_store``.
        """
        bucket = self._state_bucket
        if bucket is not None:
            return BucketHostStateStore(
                bucket=bucket, bucket_error_type=BlobStateBucketError, bucket_label="Azure state bucket"
            )
        return _VmTagHostStateStore(provider=self)

    def _host_identity(self) -> BlobStateHostIdentity | None:
        """Return the bucket-write managed-identity helper (uncached), or None when unresolvable.

        Built fresh each call (cheap; used only at create / rare diagnostics),
        scoped to the same state-account name as ``_state_bucket``. Mirrors
        ``AwsProvider._host_identity``.
        """
        try:
            subscription_id = self.azure_config.get_subscription_id()
        except ValueError:
            return None
        return self.azure_config.build_host_identity(subscription_id)

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
            user_assigned_identity_id=self._host_dir_sync_identity_resource_id(),
        )

    def _host_dir_sync_identity_resource_id(self) -> str | None:
        """Return the prepare-provisioned user-assigned identity resource id to attach at create, or None.

        Returns the bucket-write identity's resource id only when host_dir sync is
        on, a state bucket is present, and the identity was actually provisioned by
        ``mngr azure prepare``. Probing identity existence is best-effort: a failure
        degrades to None (no identity attached -- offline host_dir just won't work)
        rather than blocking create. Attaching the identity requires the create
        credentials to hold the identity's ``.../assign/action``. Mirrors
        ``AwsProvider._host_dir_sync_instance_profile``.
        """
        if not self.azure_config.is_host_dir_synced_to_bucket:
            return None
        if self._state_bucket is None:
            return None
        identity = self._host_identity()
        if identity is None:
            return None
        try:
            if not identity.host_identity_exists():
                logger.warning(
                    "host_dir sync is on but the bucket-write managed identity {} does not exist; launching "
                    "without it (run `mngr azure prepare --use-offline-host-dir yes` to enable offline host_dir)",
                    identity.identity_name,
                )
                return None
        except BlobStateHostIdentityError as e:
            logger.warning(
                "Could not check the bucket-write managed identity {}; launching without it: {}",
                identity.identity_name,
                e,
            )
            return None
        return identity.resource_id()

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
    # Deallocate/start + idle-watcher hooks (for OfflineCapableVpsDockerProvider)
    # =========================================================================

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        """Deallocate the VM -- the only in-guest-free way to halt Azure compute billing.

        NOT a mere OS shutdown, which leaves the VM "Stopped (not deallocated)" and
        STILL billing compute; the OS disk and all state survive for ``start_host``.
        """
        with log_span("Deallocating Azure VM"):
            self.azure_client.deallocate_instance(instance_id)

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        """Start the VM and return its public IP (allocated ``Static``, so unchanged across the pause)."""
        with log_span("Starting Azure VM"):
            return self.azure_client.start_instance(instance_id)

    def _rebind_known_hosts(self, record: VpsDockerHostRecord, new_ip: str) -> None:
        """No-op: Azure's public IP is allocated ``Static``, so it is unchanged across deallocate/start.

        The SSH host keys persist on the OS disk and the address does not move, so
        the resume path needs no known_hosts rebind (unlike AWS/GCP).
        """

    def _rebind_known_hosts_pre_connect(self, new_ip: str) -> None:
        """No-op: see ``_rebind_known_hosts`` -- the static IP needs no pre-connect rebind."""

    def _idle_watcher_service_unit(self, sentinel_on_outer: str) -> str:
        """Idle action: run the self-deallocate script (an Azure OS poweroff would not halt billing)."""
        # The script has the sentinel path baked in at install time (see
        # _write_idle_watcher_aux_files), so the service unit needs no argument.
        del sentinel_on_outer
        return _build_idle_watcher_service_unit()

    def _prepare_idle_self_stop(self, host_id: HostId) -> None:
        """Assign the VM's self-deallocate role so the in-guest idle watcher can deallocate it.

        Best-effort and MUST NOT raise (the base ``_on_host_finalized`` relies on
        that): ``_find_instance_for_host`` raises ``MngrError`` on an ambiguous
        host-id match and ``assign_self_deallocate_role`` surfaces Azure API
        failures as ``VpsApiError``/``AzureError``. It degrades gracefully when the
        operator lacks roleAssignments/write, leaving ``mngr stop`` as the way to
        halt billing.
        """
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

    def _on_host_finalized(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the base idle watcher and, when enabled, the host_dir-to-bucket sync daemon.

        Best-effort per the base contract: a host_dir-sync install failure only costs
        offline host_dir readability, so it is logged rather than failing create_host.
        """
        super()._on_host_finalized(host_id=host_id, vps_ip=vps_ip)
        try:
            self._install_host_dir_sync(host_id=host_id, vps_ip=vps_ip)
        except MngrError as e:
            logger.warning(
                "Azure host_dir sync install failed for host {} ({}); the deallocated host's host_dir "
                "will not be readable offline",
                host_id,
                e,
            )

    def _sync_host_dir_before_pause(self, host_id: HostId, vps_ip: str) -> None:
        """Push host_dir to the bucket one final time before the Azure VM deallocates."""
        self._trigger_final_host_dir_sync(host_id, vps_ip)

    def _install_host_dir_sync(self, *, host_id: HostId, vps_ip: str) -> None:
        """Install the host-side host_dir-to-bucket sync daemon on the outer host.

        Gated on ``is_host_dir_synced_to_bucket`` AND a state bucket being present
        (no bucket -> nothing to sync to) AND the bucket-write managed identity
        actually existing (azcopy authenticates as it via MSI; without it the sync
        would just 403). Installs azcopy (best-effort) and a systemd oneshot
        ``.service`` + ``.timer`` pair that runs ``azcopy sync`` every
        ``HOST_DIR_SYNC_INTERVAL_SECONDS`` using the VM's user-assigned managed
        identity (no long-lived keys). Returns early (no-op) when the feature is
        off, no bucket is configured, or the identity is absent. Mirrors
        ``AwsProvider._install_host_dir_sync``.
        """
        if not self.azure_config.is_host_dir_synced_to_bucket:
            return
        bucket = self._state_bucket
        if bucket is None:
            logger.debug("No Azure state bucket; skipping host_dir sync install for host {}", host_id)
            return
        identity = self._host_identity()
        identity_client_id = identity.get_host_identity_client_id() if identity is not None else None
        if identity_client_id is None:
            logger.warning(
                "host_dir sync is on but the bucket-write managed identity for host {} is absent; skipping "
                "the sync daemon install (run `mngr azure prepare --use-offline-host-dir yes`)",
                host_id,
            )
            return
        host_dir_on_outer = str(self._host_dir_path_on_outer(host_id))
        blob_prefix_url = _build_host_dir_blob_url(bucket.account_name, bucket.container_name, host_id)
        service_unit = _build_host_dir_sync_service_unit(host_dir_on_outer, blob_prefix_url, identity_client_id)
        timer_unit = _build_host_dir_sync_timer_unit(HOST_DIR_SYNC_INTERVAL_SECONDS)
        with log_span("Installing Azure host_dir sync daemon"):
            with self._make_outer_for_vps_ip(vps_ip) as outer:
                outer.execute_idempotent_command(_build_azcopy_install_command(), timeout_seconds=300.0)
                outer.write_text_file(Path(f"/etc/systemd/system/{HOST_DIR_SYNC_UNIT_NAME}.service"), service_unit)
                outer.write_text_file(Path(f"/etc/systemd/system/{HOST_DIR_SYNC_UNIT_NAME}.timer"), timer_unit)
                outer.execute_idempotent_command("systemctl daemon-reload")
                outer.execute_idempotent_command(f"systemctl enable --now {HOST_DIR_SYNC_UNIT_NAME}.timer")
        logger.info("Azure host_dir sync daemon installed for host {} (target {})", host_id, blob_prefix_url)

    def _trigger_final_host_dir_sync(self, host_id: HostId, vps_ip: str) -> None:
        """Run the host_dir sync once (best-effort) so the offline copy is current before deallocate.

        Called from ``stop_host`` while the VM is still reachable. Starts the
        oneshot sync service synchronously (``--wait`` blocks until it finishes).
        Best-effort: any failure is logged at WARNING and swallowed so a sync
        hiccup never blocks the stop -- the offline copy is then simply "as of the
        last periodic sync". Mirrors ``AwsProvider._trigger_final_host_dir_sync``.
        """
        if not self.azure_config.is_host_dir_synced_to_bucket or self._state_bucket is None:
            return
        try:
            with log_span("Triggering final host_dir sync before deallocate"):
                with self._make_outer_for_vps_ip(vps_ip) as outer:
                    outer.execute_idempotent_command(
                        f"systemctl start --wait {HOST_DIR_SYNC_UNIT_NAME}.service", timeout_seconds=300.0
                    )
        except MngrError as e:
            logger.warning(
                "Final host_dir sync before stopping host {} failed; the offline copy will be as of "
                "the last periodic sync: {}",
                host_id,
                e,
            )

    def _write_idle_watcher_aux_files(self, outer: OuterHostInterface, sentinel_on_outer: str) -> None:
        """Install the self-deallocate script the watcher ``.service`` runs (and ensure curl is present).

        The script calls the IMDS + ARM API with curl; ensure curl exists
        (idempotent -- a no-op when already installed) so idle self-deallocate
        doesn't silently degrade.
        """
        outer.execute_idempotent_command(
            "command -v curl >/dev/null 2>&1 || (apt-get update && apt-get install -y curl)"
        )
        outer.write_text_file(Path(_DEALLOCATE_SCRIPT_PATH), _build_self_deallocate_script(sentinel_on_outer))
        outer.execute_idempotent_command(f"chmod +x {_DEALLOCATE_SCRIPT_PATH}")

    # =========================================================================
    # Offline metadata (so DEALLOCATED hosts list + resolve by name): the Blob
    # state bucket when configured, else the VM tag mirror.
    # =========================================================================

    def _persist_agent_to_tags(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        """Mirror an agent record into per-field VM tags (no-bucket fallback)."""
        instance = self._find_instance_for_host(host_id)
        if instance is None:
            logger.warning("No Azure VM found for host {}; cannot persist agent tags", host_id)
            return
        set_tags, delete_keys = self._agent_field_tags(agent_id, agent_data, instance)
        self.azure_client.add_tags(VpsInstanceId(instance["id"]), set_tags)
        if delete_keys:
            self.azure_client.remove_tags(VpsInstanceId(instance["id"]), delete_keys)

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

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline host, reconstructing a deallocated VM's record offline.

        Falls back to the base (SSH/volume-backed) path first; if that can't find
        the host (deallocated and unreachable), reconstruct it from the external
        store: the *full* ``VpsDockerHostRecord`` when the store has it (Blob
        ``host_state.json``), otherwise a minimal record rebuilt from the VM's own
        tags -- which also covers a bucket-mode host created before the bucket
        existed (so its ``host_state.json`` is absent). Mirrors
        ``AwsProvider.to_offline_host``. Calls the SSH-only ``VpsDockerProvider``
        path directly so the ``OfflineCapableVpsDockerProvider`` tag fallback does
        not pre-empt the bucket-aware reconstruction below.
        """
        try:
            return VpsDockerProvider.to_offline_host(self, host_id)
        except HostNotFoundError:
            record = self._state_store.read_host_record(host_id)
            # In bucket mode, fall back to the VM's own tags for a host whose
            # host_state.json is absent (created before the bucket existed). The tag
            # store already reconstructs from tags, so this fallback is bucket-only.
            if record is None and self._state_bucket is not None:
                record = self._host_record_from_instance_tags(host_id)
            if record is None:
                raise
            return self._create_offline_host(record)

    # =========================================================================
    # Offline host_dir volume (reads via the operator's credentials)
    # =========================================================================

    def get_volume_reference_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return a bucket-backed host_dir volume *reference* (cheap, no network probe).

        Used by ``make_readable_offline_host`` during discovery, so it must stay
        cheap: it only builds the scoped ``BlobVolume`` (no Blob call) when host_dir
        sync is on and a state bucket is present. Reads use the operator's
        credentials, so no VM identity is needed here. Returns None when the feature
        is off or no bucket is configured. Mirrors
        ``AwsProvider.get_volume_reference_for_host``.
        """
        if not self.azure_config.is_host_dir_synced_to_bucket:
            return None
        bucket = self._state_bucket
        if bucket is None:
            return None
        host_id = host.id if isinstance(host, HostInterface) else host
        return HostVolume(volume=bucket.volume_for_host(host_id))

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Return the bucket-backed host_dir volume, with a light existence probe.

        Like ``get_volume_reference_for_host`` but additionally confirms the host's
        ``host_dir/`` prefix actually has blobs (a cheap prefix list). When the
        prefix is empty, runs the missing-identity diagnostic (Decision 7) -- a
        clear WARNING pointing at ``mngr azure prepare --use-offline-host-dir yes``
        if the VM has no attached user-assigned identity -- and returns None
        (callers treat None as "not available"). This never raises. Mirrors
        ``AwsProvider.get_volume_for_host``.
        """
        reference = self.get_volume_reference_for_host(host)
        if reference is None:
            return None
        bucket = self._state_bucket
        if bucket is None:
            return None
        host_id = host.id if isinstance(host, HostInterface) else host
        try:
            if not bucket.host_dir_prefix_has_objects(host_id):
                self._warn_if_host_dir_identity_missing(host_id)
                return None
        except BlobStateBucketError as e:
            logger.warning(
                "Could not probe host_dir prefix for host {}; treating volume as unavailable: {}", host_id, e
            )
            return None
        return reference

    def _warn_if_host_dir_identity_missing(self, host_id: HostId) -> None:
        """Warn (non-fatally) when an empty host_dir prefix is explained by a missing managed identity.

        Detects the Decision-7 case directly from cloud state: when the host's VM
        has no attached user-assigned managed identity, the on-box sync daemon
        could never push host_dir, which is why the prefix is empty. Points the user
        at ``mngr azure prepare --use-offline-host-dir yes`` (and recreating the
        host so it picks up the identity). Best-effort: any probe failure is
        swallowed (this is purely advisory). Mirrors
        ``AwsProvider._warn_if_host_dir_identity_missing``.
        """
        try:
            instance = self._find_instance_for_host(host_id)
            if instance is None:
                return
            identity_ids = self.azure_client.get_instance_user_assigned_identity_ids(VpsInstanceId(instance["id"]))
        except (MngrError, AzureError) as e:
            logger.debug(
                "Could not check managed identity for host {} while diagnosing empty host_dir: {}", host_id, e
            )
            return
        if not identity_ids:
            logger.warning(
                "Host {}'s VM has no attached user-assigned managed identity, so its host_dir was never "
                "pushed to the bucket and is not readable offline. Run `mngr azure prepare "
                "--use-offline-host-dir yes`, then recreate the host so it picks up the identity.",
                host_id,
            )


class _VmTagHostStateStore(HostStateStore):
    """Tag-backed host-state mirror: the VM's own tags are the store (no-bucket fallback).

    Compact (256-char per value) and keyed off the live VM, so the host record /
    agent records are reconstructed from the VM's ``mngr-*`` tags. Delegates the
    tag I/O to the owning provider, which resolves VMs from its cached listing.
    """

    provider: AzureProvider

    def persist_host_record(self, record: VpsDockerHostRecord) -> None:
        # The VM's own create/stop tags carry the host record; nothing extra to write.
        pass

    def delete_host_state(self, host_id: HostId) -> None:
        # Destroying the VM drops its tags, so there is no separate state to delete.
        pass

    def persist_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        self.provider._persist_agent_to_tags(host_id, agent_id, agent_data)

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return
        keys = [f"{AGENT_TAG_PREFIX}{agent_id}-{field}" for field in AGENT_TAG_FIELDS]
        self.provider.azure_client.remove_tags(VpsInstanceId(instance["id"]), keys)

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        instance = self.provider._find_instance_for_host(host_id)
        if instance is None:
            return []
        return self.provider._persisted_agent_dicts_from_instance(instance)

    def read_host_record(self, host_id: HostId) -> VpsDockerHostRecord | None:
        return self.provider._host_record_from_instance_tags(host_id)


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
