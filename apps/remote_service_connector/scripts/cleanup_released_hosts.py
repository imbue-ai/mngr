#!/usr/bin/env python3
"""One-off, broad cleanup of OVH pool hosts via direct OVH calls.

This is the operator runbook companion to the connector's release route +
hourly cleanup cron (see ``imbue/remote_service_connector/app.py``). It reuses
the same pure OVH helpers from ``app.py`` -- there is no ``mngr`` dependency.

It does two things, broadly:

1. **OVH tag-scan.** Lists every OVH VPS tagged ``mngr-provider`` and, for each
   one that is not backing a live pool row (see safety note below), strips
   *all* IAM tags except ``mngr-provider`` (so ``minds_env`` / ``mngr-host-id``
   and anything else go) and cancels the VPS (``deleteAtExpiration=true``).
   That leaves a clean, recyclable host the next pool bake can reuse.
2. **DB sweep.** For each database it is given, deletes ``released`` /
   ``removing`` rows and any row pointing at a VPS it just cleaned.

Safety: by default a VPS that backs an ``available`` or ``leased`` row in any
provided database is left alone (printed as ``protected``). Pass
``--include-active`` to clean those too (only do this when you are certain the
pool is empty).

Credentials:
  * OVH AK/AS/CK come from the environment (``OVH_APPLICATION_KEY`` /
    ``OVH_APPLICATION_SECRET`` / ``OVH_CONSUMER_KEY``, optional
    ``OVH_ENDPOINT``). Source them from Vault, e.g.:
        for k in OVH_APPLICATION_KEY OVH_APPLICATION_SECRET OVH_CONSUMER_KEY; do
            export "$k=$(vault kv get -format=json -mount=secrets minds/dev/ovh/$k \\
                | jq -r .data.data.value)"
        done
  * Databases come from ``--database-url`` (repeatable). Pull each tier's
    host-pool DSN from Vault (``secrets/minds/<tier>/neon/DATABASE_URL`` -> ``value``).

Defaults to a dry-run; pass ``--yes`` to actually mutate OVH / the databases.

Usage:
    uv run python apps/remote_service_connector/scripts/cleanup_released_hosts.py \\
        --database-url "$DEV_DATABASE_URL" \\
        --database-url "$STAGING_DATABASE_URL" \\
        --yes
"""

import os
from typing import Final

import click
import psycopg2
from loguru import logger
from ovh.exceptions import APIError as OvhApiError
from ovh.exceptions import HTTPError as OvhHttpError

from imbue.remote_service_connector.app import HttpOvhOps
from imbue.remote_service_connector.app import OVH_PROVIDER_TAG_KEY
from imbue.remote_service_connector.app import OvhOps
from imbue.remote_service_connector.app import OvhVpsResource
from imbue.remote_service_connector.app import ovh_region_code_for_endpoint
from imbue.remote_service_connector.app import vps_urn_for

_OVH_DEFAULT_ENDPOINT: Final[str] = "ovh-us"
# Pool rows in these statuses still back a live lease and are left alone unless
# ``--include-active`` is passed.
_ACTIVE_STATUSES: Final[frozenset[str]] = frozenset({"available", "leased"})


def _build_ovh_ops() -> OvhOps:
    """Build an OvhOps from environment credentials. Raises if any are missing."""
    missing = [
        name
        for name in ("OVH_APPLICATION_KEY", "OVH_APPLICATION_SECRET", "OVH_CONSUMER_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise click.ClickException(
            f"Missing OVH credentials in environment: {', '.join(missing)}. "
            "Source them from Vault (secrets/minds/<tier>/ovh) before running."
        )
    return HttpOvhOps(
        application_key=os.environ["OVH_APPLICATION_KEY"],
        application_secret=os.environ["OVH_APPLICATION_SECRET"],
        consumer_key=os.environ["OVH_CONSUMER_KEY"],
        endpoint=os.environ.get("OVH_ENDPOINT", _OVH_DEFAULT_ENDPOINT),
    )


def _load_active_status_by_vps(database_urls: tuple[str, ...]) -> dict[str, str]:
    """Map each VPS to its active pool status across every database.

    A VPS is included only if some database has it in an active status
    (``available`` / ``leased``), so a host leased in any DB is protected. This
    backs the runbook's safety check that leaves live-leased VPSes alone.
    """
    # Key on vps_address: it is the OVH service name (the `vps-xxxx.vps.ovh.us`
    # hostname), which is exactly what OvhVpsResource.name carries, so the
    # caller's `active_status_by_vps.get(resource.name)` lookup matches. We do
    # NOT key on vps_instance_id -- that column historically held the mngr
    # host_id (a `host-...` id) instead of the service name, which made this
    # protection silently never match and would have cancelled live hosts.
    status_by_service_name: dict[str, str] = {}
    for database_url in database_urls:
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT vps_address, status FROM pool_hosts")
                for vps_address, status in cur.fetchall():
                    if status in _ACTIVE_STATUSES:
                        status_by_service_name[vps_address] = status
        finally:
            conn.close()
    return status_by_service_name


def _strip_all_non_provider_tags(ovh_ops: OvhOps, resource: OvhVpsResource, region_code: str) -> list[str]:
    """Delete every tag except ``mngr-provider``. Returns the stripped keys."""
    urn = resource.urn or vps_urn_for(resource.name, region_code)
    stripped: list[str] = []
    for tag_key in resource.tags:
        if tag_key == OVH_PROVIDER_TAG_KEY:
            continue
        ovh_ops.delete_tag(urn, tag_key)
        stripped.append(tag_key)
    return stripped


def _delete_matching_db_rows(database_urls: tuple[str, ...], cleaned_service_names: set[str]) -> int:
    """Delete released/removing rows and rows pointing at a cleaned VPS. Returns count."""
    deleted_count = 0
    for database_url in database_urls:
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, vps_address, status FROM pool_hosts")
                rows = cur.fetchall()
                for row_id, vps_address, status in rows:
                    is_stale_status = status in ("released", "removing")
                    # Match on vps_address: it is the OVH service name carried in
                    # cleaned_service_names (resource.name), unlike vps_instance_id.
                    is_cleaned_host = vps_address in cleaned_service_names
                    if is_stale_status or is_cleaned_host:
                        cur.execute("DELETE FROM pool_hosts WHERE id = %s", (str(row_id),))
                        deleted_count += 1
            conn.commit()
        finally:
            conn.close()
    return deleted_count


@click.command()
@click.option(
    "--database-url",
    "database_urls",
    multiple=True,
    type=str,
    help="Host-pool DSN to sweep for stale/cleaned rows (repeatable). Pull from Vault per tier.",
)
@click.option(
    "--include-active",
    is_flag=True,
    default=False,
    help="Also clean VPSes that back an available/leased row (only when the pool is truly empty).",
)
@click.option(
    "--yes",
    "is_confirmed",
    is_flag=True,
    default=False,
    help="Actually mutate OVH and the databases. Without it, this is a dry-run.",
)
def cleanup_released_hosts(database_urls: tuple[str, ...], include_active: bool, is_confirmed: bool) -> None:
    ovh_ops = _build_ovh_ops()
    region_code = ovh_region_code_for_endpoint(os.environ.get("OVH_ENDPOINT", _OVH_DEFAULT_ENDPOINT))

    # Read DB state up front so we can protect VPSes backing live leases.
    active_status_by_vps = _load_active_status_by_vps(database_urls)

    # Find every mngr-provider-tagged VPS in the account.
    resources = [r for r in ovh_ops.list_vps_resources() if OVH_PROVIDER_TAG_KEY in r.tags]
    if not resources:
        logger.info("No mngr-provider-tagged OVH VPSes found.")
        return

    to_clean: list[OvhVpsResource] = []
    protected: list[OvhVpsResource] = []
    for resource in resources:
        active_status = active_status_by_vps.get(resource.name)
        if active_status is not None and not include_active:
            protected.append(resource)
        else:
            to_clean.append(resource)

    logger.info(
        "Found {} mngr-provider VPS(es): {} to clean, {} protected (active).",
        len(resources),
        len(to_clean),
        len(protected),
    )
    for resource in protected:
        logger.info("  PROTECTED {} (active={})", resource.name, active_status_by_vps.get(resource.name))
    for resource in to_clean:
        strippable = sorted(k for k in resource.tags if k != OVH_PROVIDER_TAG_KEY)
        logger.info("  CLEAN     {} (strip tags={}, then cancel)", resource.name, strippable)

    if not is_confirmed:
        logger.info("Dry-run only. Re-run with --yes to apply.")
        return

    cleaned_service_names: set[str] = set()
    failure_count = 0
    for resource in to_clean:
        try:
            _strip_all_non_provider_tags(ovh_ops, resource, region_code)
            ovh_ops.set_delete_at_expiration(resource.name, True)
            cleaned_service_names.add(resource.name)
            logger.info("Cleaned + cancelled OVH VPS {}", resource.name)
        except (OvhApiError, OvhHttpError) as exc:
            logger.warning("Failed to clean OVH VPS {}: {}", resource.name, exc)
            failure_count += 1

    deleted_count = _delete_matching_db_rows(database_urls, cleaned_service_names)
    logger.info(
        "Done. Cleaned {}/{} VPS(es), deleted {} DB row(s).",
        len(cleaned_service_names),
        len(to_clean),
        deleted_count,
    )
    if failure_count > 0:
        raise click.ClickException(f"{failure_count} VPS(es) failed to clean; see warnings above.")


if __name__ == "__main__":
    cleanup_released_hosts()
