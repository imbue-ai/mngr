"""``mngr gcp ...`` operator commands.

``mngr gcp prepare`` does the one-time privileged setup (network-scoped,
tag-targeted firewall rule creation) so the regular ``mngr create`` path can
run with restricted IAM (instance create/get/list, no
``compute.firewalls.create``). Conventional split: an admin runs prepare once;
developers run create with limited credentials. Mirrors ``mngr aws prepare``.

``mngr gcp cleanup`` is the inverse of prepare: it deletes the mngr-managed
firewall rule so a project returns to its pre-prepare state. It refuses while
any mngr-managed instance still exists in the project, so it cannot strand a
running agent's SSH access. Mirrors ``mngr aws cleanup``.
"""

import click
from google.auth import exceptions as google_auth_exceptions
from loguru import logger

from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.errors import MngrError
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig


def _build_operator_client(
    project_id: str | None,
    zone: str | None,
    firewall_name: str | None,
    firewall_target_tag: str | None,
    network: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> GcpVpsClient:
    """Construct a ``GcpVpsClient`` for the ``mngr gcp`` operator commands.

    Bridges click options into the client constructor: each option falls back
    to the corresponding ``GcpProviderConfig`` default when not supplied.
    Pulled out of the click callbacks purely for readability. ``image`` is
    unused by the firewall ops but the constructor requires it; a placeholder is
    fine because the operator commands never call ``create_instance``.
    ``prepare`` passes the effective ingress CIDRs; ``cleanup`` passes ``()``
    (it only deletes, never authorizes ingress, so the value is unused there).
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


def _perform_cleanup(client: GcpVpsClient) -> str | None:
    """Core of ``mngr gcp cleanup``: refuse if instances exist, else delete the rule.

    Returns the deleted firewall rule name, or ``None`` when there was nothing to
    delete. Raises ``click.ClickException`` when any mngr-managed instance still
    exists in the project, so cleanup never strands a running agent's SSH access.
    Split from the click callback so the refuse/delete decision is unit-testable
    against a stubbed client, without the click runtime or real credentials.
    """
    instances = client.list_mngr_managed_instances()
    if instances:
        summary = ", ".join(f"{i['id']} ({i['state']} in {i['zone']})" for i in instances)
        raise click.ClickException(
            f"Refusing to clean up project {client.project_id}: {len(instances)} mngr-managed "
            f"instance(s) still exist: {summary}. Destroy them first with `mngr destroy <agent>` "
            "(or delete them), then re-run `mngr gcp cleanup`."
        )
    return client.delete_firewall()


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
        client = _build_operator_client(
            project_id, zone, firewall_name, firewall_target_tag, network, allowed_ssh_cidrs
        )
    except (ValueError, google_auth_exceptions.GoogleAuthError) as e:
        # ``ValueError`` covers the no-ADC / no-project cases raised by
        # ``GcpProviderConfig`` (``GcpCredentialsError`` / ``GcpProjectError``,
        # both ``ValueError`` subclasses); ``GoogleAuthError`` covers other
        # auth-resolution failures. Mirrors the pair caught by
        # ``GcpProviderBackend.build_provider_instance``.
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


@gcp_cli_group.command(name="cleanup")
@click.option(
    "--project",
    "project_id",
    default=None,
    help="GCP project ID. Defaults to the provider config's project_id (or the gcloud/ADC default).",
)
@click.option(
    "--firewall-name",
    "firewall_name",
    default=None,
    help="Firewall rule name to delete. Defaults to 'mngr-gcp-ssh'.",
)
@click.option(
    "--network",
    "network",
    default=None,
    help="VPC network the rule applies to (part of its identity). Defaults to 'default'.",
)
def cleanup(
    project_id: str | None,
    firewall_name: str | None,
    network: str | None,
) -> None:
    """Undo `mngr gcp prepare`: delete the mngr-managed firewall rule.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed instance still exists anywhere in the project -- destroy
    those first with `mngr destroy <agent>` so a running agent's SSH access is
    never stranded. With no instances present, deletes the auto-created
    `mngr-gcp-ssh` firewall rule. Idempotent: a no-op (exit 0) when the rule is
    already gone.

    Needs compute.instances.list (aggregated) + compute.firewalls.get +
    compute.firewalls.delete. Does not touch per-host keys -- those are created
    and deleted by the create/destroy lifecycle, not by `prepare`.
    """
    try:
        # firewall_target_tag and allowed_ssh_cidrs are irrelevant to delete;
        # the shared builder takes them, so pass the defaults / an empty tuple.
        client = _build_operator_client(project_id, None, firewall_name, None, network, ())
    except (ValueError, google_auth_exceptions.GoogleAuthError) as e:
        # Same credential / environment errors as the prepare path.
        raise click.ClickException(str(e)) from e
    if not client.project_id:
        raise click.ClickException(
            "No GCP project resolved. Pass --project, run "
            "'mngr config set providers.gcp.project_id <your-project>', set the GOOGLE_CLOUD_PROJECT "
            "environment variable, or run 'gcloud config set project <your-project>' (the active "
            "gcloud project is used automatically when Application Default Credentials are present)."
        )
    deleted_firewall = _perform_cleanup(client)
    if deleted_firewall is None:
        write_human_line(
            f"Nothing to clean up: no firewall rule {client.firewall_name!r} in project {client.project_id}."
        )
    else:
        logger.info("Cleaned up GCP firewall rule {} in project {}", deleted_firewall, client.project_id)
        write_human_line(deleted_firewall)
