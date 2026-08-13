"""``minds migrate-ssh-keys``: manually run the RSA -> Ed25519 client-key migration.

The desktop client runs the same migration automatically in the background
(:mod:`imbue.minds.desktop_client.ssh_key_migration`); this command exists for
testing and for operators who want to migrate immediately without launching the
app. It enumerates workspaces via ``mngr ls --format json`` and runs one
migration pass synchronously.
"""

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import resolve_effective_mngr_host_dir
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.desktop_client.ssh_key_migration import HostMigrationResult
from imbue.minds.desktop_client.ssh_key_migration import MigrationOutcome
from imbue.minds.desktop_client.ssh_key_migration import SshKeyMigrationError
from imbue.minds.desktop_client.ssh_key_migration import list_migratable_workspaces_from_mngr_ls_json
from imbue.minds.desktop_client.ssh_key_migration import run_ssh_key_migration_pass
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import get_default_mngr_caller


class MigrateSshKeysArguments(FrozenModel):
    """Parsed arguments for the migrate-ssh-keys command."""

    host_id: str | None = Field(description="Restrict the pass to one host, or None for every discovered host")


_LS_TIMEOUT_SECONDS = 120.0


def _describe_result(result: HostMigrationResult) -> str:
    if result.detail:
        return f"{result.host_id}: {result.outcome.value} ({result.detail})"
    return f"{result.host_id}: {result.outcome.value}"


@click.command(name="migrate-ssh-keys")
@click.option(
    "--host-id",
    "host_id",
    default=None,
    help="Only migrate this host-<hex> coordinate (defaults to every discovered host)",
)
def migrate_ssh_keys(host_id: str | None) -> None:
    """Rotate RSA client SSH keys to Ed25519 for per-host-keyed workspaces."""
    arguments = MigrateSshKeysArguments(host_id=host_id)
    root_name = resolve_minds_root_name()
    data_directory = minds_data_dir_for(root_name)
    mngr_host_dir = resolve_effective_mngr_host_dir()
    mngr_caller = get_default_mngr_caller()
    with ConcurrencyGroup(name="minds-migrate-ssh-keys") as concurrency_group:
        mngr_caller.initialize(concurrency_group)
        try:
            ls_result = mngr_caller.call(["ls", "--format", "json"], timeout=_LS_TIMEOUT_SECONDS)
            if ls_result.returncode != 0:
                raise click.ClickException(f"`mngr ls` failed: {ls_result.stderr.strip() or 'unknown error'}")
            try:
                workspaces = list_migratable_workspaces_from_mngr_ls_json(ls_result.stdout)
            except SshKeyMigrationError as e:
                raise click.ClickException(str(e)) from e
            if arguments.host_id is not None:
                workspaces = [workspace for workspace in workspaces if workspace.host_id == arguments.host_id]
                if not workspaces:
                    raise click.ClickException(f"No discovered workspace has host id '{arguments.host_id}'.")
            try:
                results = run_ssh_key_migration_pass(
                    workspaces=workspaces,
                    mngr_host_dir=mngr_host_dir,
                    marker_dir=data_directory / "ssh_key_migrations",
                    mngr_caller=mngr_caller,
                    attempt_count_by_host_id={},
                )
            except (MindError, OSError) as e:
                raise click.ClickException(f"Migration pass failed: {e}") from e
        finally:
            mngr_caller.stop()
    for result in results:
        logger.info("{}", _describe_result(result))
    if not results:
        logger.info("Nothing to do: every discovered host is already examined (or none were found).")
    failed_count = sum(1 for result in results if result.outcome == MigrationOutcome.FAILED)
    if failed_count > 0:
        raise click.ClickException(f"{failed_count} host(s) failed to migrate; see the lines above.")
