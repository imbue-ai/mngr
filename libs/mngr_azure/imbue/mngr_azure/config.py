import hashlib
import json
import os
from pathlib import Path
from typing import Any
from typing import Final

from azure.identity import DefaultAzureCredential
from loguru import logger
from pydantic import Field

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_azure.errors import AzureSubscriptionError
from imbue.mngr_azure.state_bucket import BlobStateBucket
from imbue.mngr_vps.config import PublicIpVpsProviderConfig

# Storage-account names are globally unique, 3-24 chars, lowercase alphanumeric
# only (no hyphens). The derived name is ``mngrst<hash>`` where ``<hash>`` is a
# deterministic short digest of subscription + resource group, truncated to keep
# the whole name within the 24-char cap.
_STATE_ACCOUNT_NAME_PREFIX: Final[str] = "mngrst"
_STATE_ACCOUNT_NAME_MAX_LENGTH: Final[int] = 24
_STATE_ACCOUNT_HASH_LENGTH: Final[int] = _STATE_ACCOUNT_NAME_MAX_LENGTH - len(_STATE_ACCOUNT_NAME_PREFIX)

# Tag written on the resource group by ``mngr azure prepare`` so the inverse
# ``mngr azure cleanup`` can prove the group is mngr-owned before deleting it
# (it must never delete a user's pre-existing resource group).
AZURE_MANAGED_BY_TAG_KEY: Final[str] = "managed-by"
AZURE_MANAGED_BY_TAG_VALUE: Final[str] = "mngr"

# Default marketplace image: Debian 12 (gen2), matching the Debian-12 default of
# the other mngr providers (aws / gcp / ovh / vultr). Debian's Azure image runs
# cloud-init with the Azure datasource, so the shared ``mngr_vps``
# cloud-init flow (Docker install, SSH host-key injection, mngr bootstrap) works
# unchanged. The four-part publisher/offer/sku/version URN is configurable for
# users who want a different distro or a custom image; ``test_release_azure``
# validates that the default still resolves.
DEFAULT_IMAGE_PUBLISHER: Final[str] = "Debian"
DEFAULT_IMAGE_OFFER: Final[str] = "debian-12"
DEFAULT_IMAGE_SKU: Final[str] = "12-gen2"
DEFAULT_IMAGE_VERSION: Final[str] = "latest"


# The az CLI rewrites azureProfile.json in place (open-truncate-write, not an
# atomic rename) on token refresh and on most ``az`` commands, so a read that
# races a write can momentarily see a truncated file that fails to decode/parse.
# Poll for a short, sub-second window -- spaced so the (concurrent) writer can
# finish -- before giving up.
_AZ_PROFILE_READ_TIMEOUT_SECONDS: Final[float] = 0.2
_AZ_PROFILE_READ_RETRY_SECONDS: Final[float] = 0.1


def _read_az_profile_subscriptions(profile_path: Path) -> list[Any] | None:
    """Parse the az profile's ``subscriptions`` list; None on a transient torn read.

    Returning None tells the poll loop to retry (the az CLI rewrites the file
    non-atomically, so a racing read can see a truncated/unparseable file).
    FileNotFoundError propagates: a genuinely absent file is terminal.
    """
    try:
        # UnicodeDecodeError (bad bytes) and json's ValueError (bad JSON) are both
        # ValueError; a non-dict top level makes ``.get`` raise AttributeError.
        return json.loads(profile_path.read_text(encoding="utf-8-sig")).get("subscriptions", [])
    except FileNotFoundError:
        raise
    except (OSError, ValueError, AttributeError):
        return None


def read_az_cli_default_subscription() -> str | None:
    """Return the Azure CLI's active (default) subscription id, or None.

    This is the Azure analog of the ``gcloud config set project`` / ADC-resolved
    project that the GCP provider falls back to: after ``az login`` (and
    optionally ``az account set --subscription ...``), the CLI records the active
    subscription in ``$AZURE_CONFIG_DIR/azureProfile.json`` (default
    ``~/.azure/azureProfile.json``) with ``isDefault: true``. Reading that file
    lets ``--provider azure`` work with no config and no env var, the same way
    GCP works off the active gcloud project.

    The file is read (not shelled out to ``az``) so this works without the az CLI
    on PATH. Returns None when the file is absent / unreadable / has no enabled
    default subscription, so callers fall through to the "no subscription" error.
    azureProfile.json is written with a UTF-8 BOM, hence ``utf-8-sig``.

    A truncated/partial read (the az CLI rewriting the file under us) is treated
    as transient and retried; only a *persistently* unreadable file -- or a
    genuinely absent one -- resolves to None. This matters because None here
    surfaces upstream as ``ProviderUnavailableError``, which would drop azure
    agents from ``mngr list`` if a momentary mid-write read were taken as final.
    """
    config_dir = os.environ.get("AZURE_CONFIG_DIR") or str(Path.home() / ".azure")
    profile_path = Path(config_dir) / "azureProfile.json"
    try:
        subscriptions, _, _ = poll_for_value(
            lambda: _read_az_profile_subscriptions(profile_path),
            timeout=_AZ_PROFILE_READ_TIMEOUT_SECONDS,
            poll_interval=_AZ_PROFILE_READ_RETRY_SECONDS,
        )
    except FileNotFoundError:
        # Genuinely absent (azure never set up) -- not a transient mid-write state.
        return None
    if subscriptions is None:
        logger.debug(
            "Could not read az profile {} for default subscription within {}s (torn reads).",
            profile_path,
            _AZ_PROFILE_READ_TIMEOUT_SECONDS,
        )
        return None
    for subscription in subscriptions:
        if subscription.get("isDefault") and subscription.get("state", "Enabled") == "Enabled":
            subscription_id = subscription.get("id")
            if subscription_id:
                return str(subscription_id)
    return None


class AzureProviderConfig(PublicIpVpsProviderConfig):
    """Configuration for the Azure Virtual Machines VPS Docker provider.

    Credentials are deliberately not stored in this config. Azure's
    ``DefaultAzureCredential`` is used exclusively: it transparently resolves
    the developer's ``az login`` session locally and a service principal
    (``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_SECRET`` env
    vars) in CI. This matches the Modal / AWS / GCP provider convention and the
    broader project preference: do not handle credentials in mngr configs when
    an SDK can do it for us.

    ``subscription_id`` is a plain, non-secret identifier -- not credential
    material -- and is the only required field.
    """

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("azure"),
        description="Provider backend (always 'azure' for this type)",
    )
    subscription_id: str = Field(
        default="",
        description=(
            "Azure subscription ID for new resources. A plain identifier, not a credential. "
            "Optional: when unset, falls back to the AZURE_SUBSCRIPTION_ID env var, then to the "
            "Azure CLI's active subscription (`az account show`), so `--provider azure` works with "
            "no config after `az login` -- the same way GCP uses the active gcloud project."
        ),
    )
    default_region: str = Field(
        default="westus",
        description="Default Azure region / location (e.g. 'westus').",
    )
    resource_group: str = Field(
        default="mngr",
        description=(
            "Name of the mngr-owned resource group that holds all infrastructure (vnet, subnet, "
            "NSG) and the per-host VMs. Created by `mngr azure prepare` and tagged managed-by=mngr."
        ),
    )
    vnet_name: str = Field(default="mngr-vnet", description="Name of the virtual network created by prepare.")
    subnet_name: str = Field(default="mngr-subnet", description="Name of the subnet created by prepare.")
    nsg_name: str = Field(
        default="mngr-nsg",
        description="Name of the network security group created by prepare and attached to the subnet.",
    )
    vnet_address_prefix: str = Field(default="10.0.0.0/16", description="CIDR address space of the vnet.")
    subnet_address_prefix: str = Field(default="10.0.0.0/24", description="CIDR address range of the subnet.")
    default_vm_size: str = Field(
        default="Standard_B2s",
        description=(
            "Default Azure VM size (e.g. 'Standard_B2s' for 2 vCPU / 4GB). B-series is the burstable "
            "family most likely to have nonzero vCPU quota on a fresh pay-as-you-go subscription. "
            "Surfaced to users as the `--azure-vm-size=` build arg."
        ),
    )
    image_publisher: str = Field(default=DEFAULT_IMAGE_PUBLISHER, description="Marketplace image publisher.")
    image_offer: str = Field(default=DEFAULT_IMAGE_OFFER, description="Marketplace image offer.")
    image_sku: str = Field(default=DEFAULT_IMAGE_SKU, description="Marketplace image SKU.")
    image_version: str = Field(default=DEFAULT_IMAGE_VERSION, description="Marketplace image version ('latest' ok).")
    admin_username: str = Field(
        default="azureuser",
        description=(
            "Admin user the injected SSH public key is attached to at VM create. Cloud-init also "
            "forwards the key into root's authorized_keys, so mngr's root SSH works regardless."
        ),
    )
    os_disk_size_gb: int = Field(default=30, description="Size of the OS managed disk in GB.")
    os_disk_type: str = Field(
        default="StandardSSD_LRS",
        description="OS managed-disk storage account type (e.g. 'StandardSSD_LRS', 'Premium_LRS', 'Standard_LRS').",
    )
    state_storage_account_name: str | None = Field(
        default=None,
        description=(
            "Azure Storage account where mngr stores a deallocated VM's state so it is readable "
            "without starting the VM. When None, named 'mngrst<hash>' (3-24 lowercase alnum). The "
            "storage account is required infrastructure (run `mngr azure prepare`); there is no tag fallback."
        ),
    )
    is_offline_host_dir_enabled: bool = Field(
        default=True,
        description=(
            "When on (default), a deallocated VM's host_dir is readable without starting it, so "
            "`mngr event` / `mngr transcript` / `mngr file` work against it. `mngr azure prepare` sets "
            "up the access it needs. Set False to turn it off."
        ),
    )

    def get_credential(self) -> Any:
        """Return a ``DefaultAzureCredential`` for the management clients.

        Typed ``Any`` because ``azure.core.credentials.TokenCredential`` is a
        Protocol (not an isinstance-able concrete class), which pydantic's
        ``arbitrary_types_allowed`` validation cannot accept as a field type.
        The returned credential is consumed transparently by the SDK; mngr never
        inspects or stores the secret material.

        Construction never raises (``DefaultAzureCredential`` authenticates
        lazily on first token request), so unlike AWS/GCP there is no cheap
        no-credentials check here -- the gating is on ``subscription_id``
        presence (see ``get_subscription_id``), and an unauthenticated
        environment surfaces as an API error on the first real call.
        """
        return DefaultAzureCredential()

    def get_subscription_id(self) -> str:
        """Return the subscription ID, raising ``AzureSubscriptionError`` if unresolvable.

        Priority: the configured ``subscription_id`` > the ``AZURE_SUBSCRIPTION_ID``
        env var > the Azure CLI's active subscription (from ``azureProfile.json``;
        see ``read_az_cli_default_subscription``). The az-CLI fallback mirrors the
        GCP provider using the active gcloud project, so ``--provider azure`` works
        with no config after ``az login``. Raising here surfaces clearly on
        ``mngr create --provider azure`` while letting ``mngr list`` warn-and-skip
        the provider (the backend wraps this in ``ProviderUnavailableError``: Azure
        was unreachable, so its state -- and any agents on it -- is unknown).
        """
        if self.subscription_id:
            return self.subscription_id
        env_subscription = os.environ.get("AZURE_SUBSCRIPTION_ID")
        if env_subscription:
            return env_subscription
        az_default_subscription = read_az_cli_default_subscription()
        if az_default_subscription:
            return az_default_subscription
        raise AzureSubscriptionError(
            "No Azure subscription resolved. Set one of:\n"
            "  - run `az login` (optionally `az account set --subscription <id>`) to use the active subscription;\n"
            "  - `mngr config set providers.azure.subscription_id <id>`;\n"
            "  - the AZURE_SUBSCRIPTION_ID environment variable."
        )

    def resolve_state_storage_account_name(self, subscription_id: str) -> str:
        """Return the effective state-storage-account name.

        ``state_storage_account_name`` wins when set. Otherwise derive
        ``mngrst<hash>`` from a deterministic short digest of the subscription +
        resource group (lowercase alphanumeric, within the 24-char Azure cap).
        Storage-account names are globally unique, so the derivation is anchored on
        the (subscription, resource-group) scope -- the same scope the bucket is
        shared across.
        """
        if self.state_storage_account_name:
            return self.state_storage_account_name
        digest = hashlib.sha256(f"{subscription_id}/{self.resource_group}".encode("utf-8")).hexdigest()
        return f"{_STATE_ACCOUNT_NAME_PREFIX}{digest[:_STATE_ACCOUNT_HASH_LENGTH]}"

    def build_state_bucket(self, subscription_id: str) -> BlobStateBucket:
        """Build a ``BlobStateBucket`` from this config + the resolved subscription.

        The account name is always derivable (unlike AWS, which needs an STS call
        to learn the account id), so this never returns None -- the provider gates
        on whether the account+container actually *exist* (i.e. whether
        ``mngr azure prepare`` created them), not on whether a name can be built.
        """
        return BlobStateBucket(
            credential=self.get_credential(),
            subscription_id=subscription_id,
            resource_group=self.resource_group,
            region=self.default_region,
            account_name=self.resolve_state_storage_account_name(subscription_id),
        )
