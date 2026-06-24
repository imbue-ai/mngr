import shutil

import google.auth
from google.auth import exceptions as google_auth_exceptions
from google.auth.credentials import Credentials
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_gcp.errors import GcpCredentialsError
from imbue.mngr_gcp.errors import GcpProjectError
from imbue.mngr_gcp.errors import GcpZoneRegionMismatchError
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_vps.config import OfflineCapableVpsProviderConfig

# OAuth scope granting full access to all Google Cloud Platform APIs. Only
# applied when ``service_account_email`` is set (attaching a service account to
# the launched VM); the ADC used by mngr itself is never scoped here.
DEFAULT_SERVICE_ACCOUNT_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/cloud-platform",)

# Global Debian 12 image family (GCE families are global, unlike per-region AWS
# AMIs), matching the rest of the mngr fleet. Stock GCE Debian images ship no
# cloud-init, so GCP bootstraps via the GCE ``startup-script`` metadata (run by
# the google-guest-agent on every image) -- see ``generate_gce_startup_script``.
DEFAULT_GCE_IMAGE: str = "projects/debian-cloud/global/images/family/debian-12"

# Final fallback zone when neither the provider config nor the active gcloud
# config supplies one. GCE VMs are zonal, so a concrete zone is always required;
# us-west1-a is an arbitrary but stable default (its region, us-west1, is derived
# as the leading ``<region>-<suffix>`` prefix of the zone).
DEFAULT_GCE_ZONE: str = "us-west1-a"

# Cap on the best-effort ``gcloud config get`` probe so a hung gcloud never
# stalls provider construction (which runs on read paths like ``mngr list``).
_GCLOUD_CONFIG_GET_TIMEOUT_SECONDS: float = 10.0


def get_gcloud_compute_zone(concurrency_group: ConcurrencyGroup) -> str | None:
    """Best-effort read of the active ``gcloud config get compute/zone``.

    Returns ``None`` when the gcloud CLI is absent, errors, times out, or has no
    ``compute/zone`` set. mngr authenticates via Application Default Credentials,
    never the gcloud CLI, so this is purely a convenience: it lets a user's
    ``gcloud config set compute/zone`` flow through as the default VM placement,
    exactly as ``google.auth.default()`` already consults gcloud for the default
    project. It is never required -- an unset or absent gcloud falls back to the
    configured or hardcoded ``DEFAULT_GCE_ZONE`` (see ``resolve_zone_and_region``).

    ``gcloud`` is probed with ``shutil.which`` first so a missing CLI is a clean
    ``None`` rather than a process-spawn error, and the command runs through the
    caller's ``ConcurrencyGroup`` (not ``subprocess``) so the child is tracked and
    torn down with the run.
    """
    if shutil.which("gcloud") is None:
        return None
    try:
        finished = concurrency_group.run_process_to_completion(
            ("gcloud", "config", "get", "compute/zone"),
            timeout=_GCLOUD_CONFIG_GET_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    except ConcurrencyGroupError as e:
        logger.debug("Best-effort 'gcloud config get compute/zone' probe failed; using fallback zone. {}", e)
        return None
    if finished.returncode != 0 or finished.is_timed_out:
        logger.trace(
            "'gcloud config get compute/zone' returned no usable zone "
            "(returncode={}, timed_out={}); using fallback zone.",
            finished.returncode,
            finished.is_timed_out,
        )
        return None
    zone = finished.stdout.strip()
    return zone or None


class GcpProviderConfig(OfflineCapableVpsProviderConfig):
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
    project_id: str | None = Field(
        default=None,
        description=(
            "GCP project ID (a plain identifier, not a credential). When unset, the project is "
            "taken from Application Default Credentials (the active 'gcloud config set project' or the "
            "GOOGLE_CLOUD_PROJECT env var); set it explicitly to pin a specific project."
        ),
    )
    default_region: str | None = Field(
        default=None,
        description=(
            "GCE region (e.g., 'us-west1'). Used only to validate the resolved zone; when unset, "
            "derived from the resolved zone. Set it to catch a mismatched default_zone typo."
        ),
    )
    default_zone: str | None = Field(
        default=None,
        description=(
            "Zone for new instances (GCE VMs are zonal). When unset, taken from the active "
            "'gcloud config get compute/zone'. Must lie in default_region when both are set explicitly."
        ),
    )
    default_machine_type: str = Field(
        default="e2-small",
        description="GCE machine type.",
    )
    default_source_image: str = Field(
        default=DEFAULT_GCE_IMAGE,
        description=(
            "GCE VM boot-disk image (distinct from the base default_image, the Docker container "
            "image run inside the VM)."
        ),
    )
    boot_disk_size_gb: int = Field(
        default=30,
        description="Boot disk size in GB.",
    )
    boot_disk_type: str = Field(
        default="pd-balanced",
        description="Boot disk type.",
    )
    network: str = Field(
        default="default",
        description="VPC network for the instance NIC and firewall rule.",
    )
    subnetwork: str | None = Field(
        default=None,
        description="Optional explicit subnetwork (required for custom-mode VPCs); None lets GCE pick for auto-mode networks.",
    )
    firewall_name: str = Field(
        default="mngr-gcp-ssh",
        description="Name of the network-scoped firewall rule `mngr gcp prepare` creates to allow SSH ingress.",
    )
    firewall_target_tag: str = Field(
        default="mngr-ssh",
        description="Network tag bound to the firewall rule; every instance is tagged with it.",
    )
    associate_external_ip: bool = Field(
        default=True,
        description=(
            "Assign an ephemeral external IPv4 to instances. Required for the current "
            "mngr-from-developer-laptop SSH access model; for a more secure deployment, set to False "
            "and run mngr from a bastion inside the VPC."
        ),
    )
    service_account_email: str | None = Field(
        default=None,
        description=(
            "Optional service account email attached to launched instances. When None, GCE applies "
            "its normal default for an unspecified service account."
        ),
    )
    service_account_scopes: tuple[str, ...] = Field(
        default=DEFAULT_SERVICE_ACCOUNT_SCOPES,
        description="OAuth scopes for the attached service account (only used when service_account_email is set).",
    )
    state_bucket_name: str | None = Field(
        default=None,
        description=(
            "GCS bucket where mngr stores a stopped instance's offline host_dir mirror so it is "
            "readable without starting the instance. When None, named 'mngr-state-<project_id>'. The "
            "bucket is provisioned by `mngr gcp prepare` and only used when "
            "`is_offline_host_dir_enabled` is on; the host + agent records still live in GCE "
            "instance metadata regardless."
        ),
    )
    is_offline_host_dir_enabled: bool = Field(
        default=True,
        description=(
            "When on (default), a stopped instance's host_dir is readable without starting it, so "
            "`mngr event` / `mngr transcript` / `mngr file` work against it. `mngr gcp prepare` sets "
            "up the GCS bucket it needs. Set False to turn it off."
        ),
    )

    def get_credentials_and_resolved_project(self) -> tuple[Credentials, str | None]:
        """Resolve Google Application Default Credentials and the project ADC infers.

        ``google.auth.default()`` returns both the credentials object and the
        project ID it resolves from the ambient environment, in this precedence:
        the ``GOOGLE_CLOUD_PROJECT`` env var, the active ``gcloud config set
        project``, a service-account key's embedded project, then the GCE
        metadata server. mngr never inspects or stores the secret credential
        material -- the SDK consumes it transparently -- but the resolved
        project is handed to ``resolve_project_id`` as the fallback when no
        ``project_id`` is configured explicitly.

        Returning both from a single ``default()`` call lets the backend resolve
        credentials and the fallback project without probing twice.

        Raises ``GcpCredentialsError`` when ADC resolves no credentials (no
        ``GOOGLE_APPLICATION_CREDENTIALS``, no ``gcloud auth
        application-default login`` file, no attached service account). The
        backend wraps this in ``ProviderUnavailableError`` (state *unknown* --
        we never reached GCP, so there may be hosts we cannot see) so read paths
        (mngr list / mngr connect / discovery) surface it to the user instead of
        silently dropping the provider, and ``mngr gc`` skips it rather than
        treating an unreachable provider's hosts as garbage. The resolved
        project may be ``None`` even when credentials succeed (e.g. a bare
        service-account key with no project and no ``GOOGLE_CLOUD_PROJECT``).
        """
        try:
            credentials, resolved_project = google.auth.default()
        except google_auth_exceptions.DefaultCredentialsError as e:
            raise GcpCredentialsError(
                "GCP Application Default Credentials not configured. Run "
                "'gcloud auth application-default login', set GOOGLE_APPLICATION_CREDENTIALS to a "
                "service-account key file, or run on a GCE/Cloud Run/GKE instance with an attached "
                "service account."
            ) from e
        return credentials, resolved_project

    def resolve_project_id(self, adc_fallback_project: str | None) -> str:
        """Return the project to launch instances in, raising ``GcpProjectError`` if none.

        The explicitly configured ``project_id`` always wins. When it is unset,
        fall back to ``adc_fallback_project`` -- the project ADC resolved from
        the ambient environment (see ``get_credentials_and_resolved_project``),
        which is the same default a user gets from ``gcloud config set project``
        or ``GOOGLE_CLOUD_PROJECT``. Raising here surfaces clearly on
        ``mngr create --provider gcp``; on read paths the backend wraps this in
        ``ProviderUnavailableError`` (state unknown -- without a project we
        cannot enumerate the provider's hosts), which is surfaced to the user
        and skipped by ``mngr gc`` rather than silently dropped.

        ``adc_fallback_project`` is injected by the caller (from the single
        ``google.auth.default()`` call shared with credential resolution) so
        this method stays pure and deterministically testable.
        """
        project_id = self.project_id or adc_fallback_project
        if not project_id:
            raise GcpProjectError(
                "No GCP project_id configured and none was resolved from the environment. Run "
                "'mngr config set providers.gcp.project_id <your-project>', set the "
                "GOOGLE_CLOUD_PROJECT environment variable, or run 'gcloud config set project "
                "<your-project>' (the active gcloud project is used automatically when Application "
                "Default Credentials are present)."
            )
        return project_id

    def resolve_zone_and_region(self, gcloud_zone: str | None) -> tuple[str, str]:
        """Resolve the effective GCE zone and region, raising ``GcpZoneRegionMismatchError`` on conflict.

        Zone precedence: the explicitly configured ``default_zone``, then
        ``gcloud_zone`` (the active ``gcloud config get compute/zone``, injected
        by the caller as a best-effort default and ``None`` when the gcloud CLI
        is absent), then ``DEFAULT_GCE_ZONE``. This mirrors how
        ``resolve_project_id`` layers an explicit value over a gcloud/ADC-derived
        fallback. The region is the explicitly configured ``default_region`` when
        set, else derived from the resolved zone.

        GCE zone names are ``<region>-<suffix>`` (e.g. ``us-west1-a`` is in
        ``us-west1``). When ``default_region`` is set explicitly and disagrees
        with the resolved zone (e.g. region ``us-west1`` with zone
        ``us-central1-a``), that is almost always a config typo that would
        otherwise surface as a confusing firewall/subnetwork-region error at
        create time, so it raises here instead.

        ``gcloud_zone`` is injected by the caller (which owns the
        ``ConcurrencyGroup`` used to shell out to gcloud) so this method stays
        pure and deterministically testable -- the same pattern as
        ``resolve_project_id``'s ``adc_fallback_project``.
        """
        zone = self.default_zone or gcloud_zone or DEFAULT_GCE_ZONE
        region = self.default_region or zone.rsplit("-", 1)[0]
        if not zone.startswith(f"{region}-"):
            raise GcpZoneRegionMismatchError(
                f"GCP zone {zone!r} is not in region {region!r} (expected a zone like {region}-a). "
                "Check default_zone / default_region (or the active 'gcloud config get compute/zone')."
            )
        return zone, region

    def resolve_state_bucket_region(self, zone: str) -> str:
        """Return the region the GCS state bucket should live in.

        Explicit ``default_region`` wins; otherwise the region is derived from
        the resolved zone (GCE zones are ``<region>-<suffix>``). Used by both
        the operator CLI (``mngr gcp prepare`` / ``cleanup``) and the runtime
        provider so they pin the bucket to the same region.
        """
        return self.default_region or zone.rsplit("-", 1)[0]

    def resolve_state_bucket_name(self, project_id: str) -> str:
        """Return the effective state-bucket name.

        ``state_bucket_name`` wins when set. Otherwise derive
        ``mngr-state-<project_id>``. GCS bucket names are globally unique but
        scoped per project, and a GCP project id is already DNS-valid lowercase,
        so the derived name is anchored on the project alone (no extra hash is
        needed). Always derivable: unlike AWS, which needs an STS call to learn
        the account id, the project id is known at config-resolution time.
        """
        if self.state_bucket_name:
            return self.state_bucket_name
        return f"mngr-state-{project_id}"

    def build_state_bucket(self, credentials: Credentials, project_id: str, region: str) -> GcsStateBucket:
        """Build a ``GcsStateBucket`` from this config + the resolved credentials / project / region.

        The bucket name is always derivable, so this never returns None -- the
        provider gates on whether the bucket actually *exists* (i.e. whether
        ``mngr gcp prepare`` created it), not on whether a name can be built.
        Mirrors ``AzureProviderConfig.build_state_bucket``.
        """
        return GcsStateBucket(
            credentials=credentials,
            project_id=project_id,
            region=region,
            bucket_name=self.resolve_state_bucket_name(project_id),
        )
