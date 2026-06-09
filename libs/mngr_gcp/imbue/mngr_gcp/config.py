import google.auth
from google.auth import exceptions as google_auth_exceptions
from google.auth.credentials import Credentials
from pydantic import Field

from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig

# OAuth scope granting full access to all Google Cloud Platform APIs. Only
# applied when ``service_account_email`` is set (attaching a service account to
# the launched VM); the ADC used by mngr itself is never scoped here.
DEFAULT_SERVICE_ACCOUNT_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/cloud-platform",)

# Global Debian 12 image family. GCE image families are global (unlike AWS
# AMIs, which are per-region), so a single string suffices -- no per-region
# map. The family always resolves to the latest published image in the family.
DEFAULT_GCE_IMAGE: str = "projects/debian-cloud/global/images/family/debian-12"


class GcpProviderConfig(VpsDockerProviderConfig):
    """Configuration for the GCP Compute Engine VPS Docker provider.

    Credentials are deliberately not stored in this config. Google
    Application Default Credentials (ADC) are used exclusively, resolved via
    ``google.auth.default()`` (``GOOGLE_APPLICATION_CREDENTIALS``, the
    ``gcloud auth application-default login`` file, or an attached service
    account / metadata server). This matches the Modal and AWS provider
    convention and the broader project preference: do not handle credentials
    in mngr configs when an SDK can do it for us.

    ``project_id`` and ``service_account_email`` / ``service_account_scopes``
    are plain, non-secret identifiers -- not credential material.
    """

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("gcp"),
        description="Provider backend (always 'gcp' for this type)",
    )
    project_id: str = Field(
        default="",
        description=(
            "GCP project ID for new instances (required). A plain identifier, not a credential. "
            "Leave the ADC mechanism to supply the actual credentials."
        ),
    )
    default_region: str = Field(
        default="us-west1",
        description="Default GCE region (e.g., 'us-west1'). Used to validate the chosen zone.",
    )
    default_zone: str = Field(
        default="us-west1-a",
        description="Default GCE zone (GCE VMs are zonal, e.g. 'us-west1-a'). Must lie in default_region.",
    )
    default_plan: str = Field(
        default="e2-small",
        description="Default GCE machine type (e.g., 'e2-small' for ~2 vCPU, 2GB RAM).",
    )
    default_image: str = Field(
        default=DEFAULT_GCE_IMAGE,
        description=(
            "Default source image for the boot disk. GCE image families are global, so a single "
            "string (no per-region map) suffices. Defaults to the latest Debian 12 image family."
        ),
    )
    boot_disk_size_gb: int = Field(
        default=30,
        description="Size of the boot persistent disk in GB.",
    )
    boot_disk_type: str = Field(
        default="pd-balanced",
        description="Boot disk type (e.g., 'pd-balanced', 'pd-ssd', 'pd-standard').",
    )
    network: str = Field(
        default="default",
        description="VPC network name for the instance NIC and the auto-created firewall rule.",
    )
    subnetwork: str | None = Field(
        default=None,
        description="Subnetwork name. Required for custom-mode VPCs; None lets GCE pick for auto-mode networks.",
    )
    allowed_ssh_cidrs: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR blocks allowed inbound on tcp/22 and tcp/<container_ssh_port> on the auto-created "
            "firewall rule. Empty by default (fail-closed): without an explicit list, ensure_firewall "
            "raises rather than create a permissive rule. Use e.g. ['203.0.113.4/32'] to allow only "
            "your own IP, or ['0.0.0.0/0'] to expose to the public internet (NOT recommended for production)."
        ),
    )
    firewall_name: str = Field(
        default="mngr-gcp-ssh",
        description="Name of the network-scoped firewall rule auto-created to allow SSH ingress.",
    )
    firewall_target_tag: str = Field(
        default="mngr-ssh",
        description=(
            "Network tag bound to the auto-created firewall rule. Every instance is tagged with it so "
            "the rule targets only mngr-managed VMs (GCE firewalls are network-scoped + tag-targeted, "
            "not per-instance like an EC2 security group)."
        ),
    )
    associate_external_ip: bool = Field(
        default=True,
        description=(
            "Assign an ephemeral external IPv4 address to the instance. Required for the current "
            "mngr-from-developer-laptop SSH access model. For a more secure deployment, set to False "
            "and run mngr from a bastion inside the VPC."
        ),
    )
    service_account_email: str | None = Field(
        default=None,
        description="Optional service account email attached to launched instances. A plain identifier.",
    )
    service_account_scopes: tuple[str, ...] = Field(
        default=DEFAULT_SERVICE_ACCOUNT_SCOPES,
        description="OAuth scopes for the attached service account (only used when service_account_email is set).",
    )

    def get_credentials(self) -> Credentials:
        """Resolve Google Application Default Credentials via ``google.auth.default()``.

        Returns the resolved credentials object so the backend can hand it to
        the ``google-cloud-compute`` clients. mngr never inspects or stores the
        secret material -- the SDK consumes the credentials transparently.

        Raises ``ValueError`` when ADC resolves nothing (no
        ``GOOGLE_APPLICATION_CREDENTIALS``, no ``gcloud auth
        application-default login`` file, no attached service account). The
        backend wraps this in ``ProviderEmptyError`` so read paths (mngr list /
        mngr gc / discovery) skip the GCP provider instead of constructing a
        half-working placeholder.
        """
        try:
            credentials, _resolved_project = google.auth.default()
        except google_auth_exceptions.DefaultCredentialsError as e:
            raise ValueError(
                "GCP Application Default Credentials not configured. Run "
                "'gcloud auth application-default login', set GOOGLE_APPLICATION_CREDENTIALS to a "
                "service-account key file, or run on a GCE/Cloud Run/GKE instance with an attached "
                "service account."
            ) from e
        return credentials

    def get_project_id(self) -> str:
        """Return the configured project ID, raising ``ValueError`` if unset.

        Unlike credentials, the project ID is a required plain identifier with
        no sensible default. Raising here surfaces clearly on
        ``mngr create --provider gcp`` while letting ``mngr list`` skip the
        provider (the backend wraps this in ``ProviderEmptyError``).
        """
        if not self.project_id:
            raise ValueError(
                "No GCP project_id configured. Set [providers.gcp] project_id = '<your-project>' in "
                "the project settings.toml."
            )
        return self.project_id

    def validate_zone_in_region(self) -> None:
        """Raise ``ValueError`` if ``default_zone`` does not lie in ``default_region``.

        GCE zone names are ``<region>-<suffix>`` (e.g. ``us-west1-a`` is in
        ``us-west1``). A mismatched pair (e.g. region ``us-west1`` with zone
        ``us-central1-a``) is almost always a config typo that would otherwise
        surface as a confusing firewall/subnetwork-region error at create time.
        """
        if not self.default_zone.startswith(f"{self.default_region}-"):
            raise ValueError(
                f"GCP default_zone {self.default_zone!r} is not in default_region "
                f"{self.default_region!r} (expected a zone like {self.default_region}-a)."
            )
