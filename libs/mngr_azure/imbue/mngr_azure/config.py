import json
import os
import time
from pathlib import Path
from typing import Any
from typing import Final

from azure.identity import DefaultAzureCredential
from loguru import logger
from pydantic import Field

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_azure.errors import AzureSubscriptionError
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig

# Tag written on the resource group by ``mngr azure prepare`` so the inverse
# ``mngr azure cleanup`` can prove the group is mngr-owned before deleting it
# (it must never delete a user's pre-existing resource group).
AZURE_MANAGED_BY_TAG_KEY: Final[str] = "managed-by"
AZURE_MANAGED_BY_TAG_VALUE: Final[str] = "mngr"

# Default marketplace image: Ubuntu 24.04 LTS (gen2). Ubuntu runs cloud-init
# with the Azure datasource, so the shared ``mngr_vps_docker`` cloud-init flow
# (Docker install, SSH host-key injection, mngr bootstrap) works unchanged. The
# four-part publisher/offer/sku/version URN is configurable for users who want a
# different distro or a custom image; ``test_release_azure`` validates that the
# default still resolves.
DEFAULT_IMAGE_PUBLISHER: Final[str] = "Canonical"
DEFAULT_IMAGE_OFFER: Final[str] = "ubuntu-24_04-lts"
DEFAULT_IMAGE_SKU: Final[str] = "server"
DEFAULT_IMAGE_VERSION: Final[str] = "latest"


# The az CLI rewrites azureProfile.json in place (open-truncate-write, not an
# atomic rename) on token refresh and on most ``az`` commands, so a read that
# races a write can momentarily see a truncated file that fails to decode/parse.
# Retry a few times -- spaced by a short sleep so the (concurrent) writer can
# finish -- before giving up. The window is sub-second, so this stays small.
_AZ_PROFILE_READ_ATTEMPTS: Final[int] = 3
_AZ_PROFILE_READ_RETRY_SECONDS: Final[float] = 0.05


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
    last_error: Exception | None = None
    for attempt in range(_AZ_PROFILE_READ_ATTEMPTS):
        try:
            # UnicodeDecodeError (bad bytes) and json's ValueError (bad JSON) are
            # both ValueError; a non-dict top level makes ``.get`` raise AttributeError.
            raw = profile_path.read_text(encoding="utf-8-sig")
            subscriptions = json.loads(raw).get("subscriptions", [])
        except FileNotFoundError:
            # Genuinely absent (azure never set up) -- not a transient mid-write
            # state, so don't waste retries waiting for a file that won't appear.
            return None
        except (OSError, ValueError, AttributeError) as e:
            last_error = e
            if attempt < _AZ_PROFILE_READ_ATTEMPTS - 1:
                time.sleep(_AZ_PROFILE_READ_RETRY_SECONDS)
            continue
        for subscription in subscriptions:
            if subscription.get("isDefault") and subscription.get("state", "Enabled") == "Enabled":
                subscription_id = subscription.get("id")
                if subscription_id:
                    return str(subscription_id)
        return None
    logger.debug(
        "Could not read az profile {} for default subscription after {} attempts: {}",
        profile_path,
        _AZ_PROFILE_READ_ATTEMPTS,
        last_error,
    )
    return None


class AzureProviderConfig(VpsDockerProviderConfig):
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
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/<container_ssh_port> on the NSG created by "
            "`mngr azure prepare`. Empty by default (fail-closed): without an explicit list, prepare "
            "raises rather than create a permissive rule. Use e.g. ['203.0.113.4/32'] to allow only "
            "your own IP, or ['0.0.0.0/0'] to expose to the public internet (NOT recommended for production)."
        ),
    )
    associate_public_ip: bool = Field(
        default=True,
        description=(
            "Assign a public IPv4 address to the VM. Required for the current "
            "mngr-from-developer-laptop SSH access model. For a more secure deployment, set to False "
            "and run mngr from a bastion inside the vnet."
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
            "No Azure subscription resolved. Run `az login` (and optionally "
            "`az account set --subscription <id>`) so the active subscription is used automatically, "
            "or run 'mngr config set providers.azure.subscription_id <your-subscription-id>', "
            "or set the AZURE_SUBSCRIPTION_ID environment variable."
        )
