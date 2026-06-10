"""``mngr gcp ...`` operator commands.

``mngr gcp prepare`` does the one-time privileged setup (network-scoped,
tag-targeted firewall rule creation) so the regular ``mngr create`` path can
run with restricted IAM (instance create/get/list, no
``compute.firewalls.create``). Conventional split: an admin runs prepare once;
developers run create with limited credentials. Mirrors ``mngr aws prepare``.
"""

import click
from google.auth import exceptions as google_auth_exceptions
from loguru import logger

from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.errors import MngrError
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig


def _build_prepare_client(
    project_id: str | None,
    zone: str | None,
    firewall_name: str | None,
    firewall_target_tag: str | None,
    network: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> GcpVpsClient:
    """Construct a ``GcpVpsClient`` for the ``mngr gcp prepare`` command.

    Bridges click options into the client constructor: each option falls back
    to the corresponding ``GcpProviderConfig`` default when not supplied.
    Pulled out of the click callback purely for readability. ``image`` is unused
    by ``ensure_firewall`` but the constructor requires it; a placeholder is
    fine because the prepare path never calls ``create_instance``.
    """
    base = GcpProviderConfig()
    credentials, adc_project = base.get_credentials_and_resolved_project()
    return GcpVpsClient(
        credentials=credentials,
        # Precedence: explicit --project, then configured project_id, then the
        # project ADC resolved (gcloud config / GOOGLE_CLOUD_PROJECT). Coalesce a
        # None ADC project to "" so the constructor's str contract holds; the
        # caller raises a clear error when the resulting project_id is empty.
        project_id=project_id or base.project_id or adc_project or "",
        zone=zone or base.default_zone,
        image="projects/debian-cloud/global/images/family/debian-12",
        network=network or base.network,
        firewall_name=firewall_name or base.firewall_name,
        firewall_target_tag=firewall_target_tag or base.firewall_target_tag,
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        container_ssh_port=base.container_ssh_port,
    )


@click.group(name="gcp")
def gcp_cli_group() -> None:
    """GCP-provider operator commands (one-time setup)."""


@gcp_cli_group.command(name="prepare")
@click.option(
    "--project",
    "project_id",
    default=None,
    help="GCP project ID. Defaults to the provider config's project_id (must be set somewhere).",
)
@click.option(
    "--zone",
    "zone",
    default=None,
    help="GCE zone for the client. Firewall rules are global, but the client is zone-bound; defaults to us-west1-a.",
)
@click.option(
    "--firewall-name",
    "firewall_name",
    default=None,
    help="Firewall rule name to create / reuse. Defaults to 'mngr-gcp-ssh'.",
)
@click.option(
    "--firewall-target-tag",
    "firewall_target_tag",
    default=None,
    help="Network tag the rule targets (every instance is tagged with it). Defaults to 'mngr-ssh'.",
)
@click.option(
    "--network",
    "network",
    default=None,
    help="VPC network the rule applies to. Defaults to 'default'.",
)
@click.option(
    "--allowed-ssh-cidr",
    "allowed_ssh_cidrs",
    multiple=True,
    help=(
        "Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. "
        "Required (fail-closed): with none supplied, prepare refuses to create a wide-open rule."
    ),
)
def prepare(
    project_id: str | None,
    zone: str | None,
    firewall_name: str | None,
    firewall_target_tag: str | None,
    network: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> None:
    """Create (or reuse) the GCP firewall rule for mngr-managed instances.

    Idempotent: re-running is a no-op when the rule already exists. Needs
    compute.firewalls.get + compute.firewalls.create. After this succeeds,
    ``mngr create --provider gcp`` only needs instance create/get/list
    permissions (no firewall-management permissions).
    """
    try:
        client = _build_prepare_client(
            project_id, zone, firewall_name, firewall_target_tag, network, allowed_ssh_cidrs
        )
    except (ValueError, google_auth_exceptions.GoogleAuthError) as e:
        # ``ValueError`` covers the no-ADC case raised by
        # ``GcpProviderConfig.get_credentials_and_resolved_project``;
        # ``GoogleAuthError`` covers other auth-resolution failures. Mirrors the
        # pair caught by ``GcpProviderBackend.build_provider_instance``.
        raise click.ClickException(str(e)) from e
    if not client.project_id:
        raise click.ClickException(
            "No GCP project resolved. Pass --project, run "
            "'mngr config set providers.gcp.project_id <your-project>', set the GOOGLE_CLOUD_PROJECT "
            "environment variable, or run 'gcloud config set project <your-project>' (the active "
            "gcloud project is used automatically when Application Default Credentials are present)."
        )
    try:
        target_tag = client.ensure_firewall()
    except MngrError as e:
        # Fail-closed: ensure_firewall raises when no --allowed-ssh-cidr was
        # supplied. Surface it as a clean CLI error rather than a traceback.
        raise click.ClickException(str(e)) from e
    logger.info(
        "Prepared GCP firewall rule {} (tag {}) in project {}", client.firewall_name, target_tag, client.project_id
    )
    write_human_line(client.firewall_name)
