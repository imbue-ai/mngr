"""``mngr ovh ...`` CLI subcommands.

Operator-grade inspection of the OVH account so you can sanity-check what
``mngr create`` and the recycle path see. ``list`` reads its defaults from the
user's ``[providers.<name>]`` settings.toml block (selected with ``--provider``)
so it talks to the same endpoint / account / credentials the runtime ``mngr
create --provider <name>`` path uses; credentials still fall back to env /
``~/.ovh.conf`` when the block leaves them unset.
"""

from collections.abc import Mapping
from typing import Any
from typing import Final

import click
from click_option_group import optgroup
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.iam_tags import list_vps_resources
from imbue.mngr_vps.cli_helpers import resolve_provider_config
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.primitives import VpsInstanceId

_MAX_PARALLEL_VPS_FETCHES: Final[int] = 16


class _OvhListCliOptions(CommonCliOptions):
    """Option shape for ``mngr ovh list`` (select a provider block + the --all toggle)."""

    provider: str
    show_all: bool


def _resolve_provider_config(mngr_ctx: MngrContext, provider_name: str) -> OvhProviderConfig:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    ``list`` must inspect the same OVH endpoint / account / credentials the
    runtime ``mngr create --provider <provider_name>`` path uses, so it reads the
    user's resolved config rather than ``OvhProviderConfig()`` class defaults
    (which would talk to the default endpoint / subsidiary regardless of what the
    user pinned). Credentials still fall back to env / ``~/.ovh.conf`` via
    ``build_ovh_client`` when the block leaves them unset. Thin wrapper over the
    shared ``resolve_provider_config`` (see it for the {configured / wrong-backend
    / missing} contract).
    """
    return resolve_provider_config(
        mngr_ctx,
        provider_name,
        config_cls=OvhProviderConfig,
        default_factory=OvhProviderConfig,
        cloud_label="an OVH backend",
        override_hint="Point --provider at an OVH-backed block to inspect it.",
    )


@click.group(name="ovh")
def ovh() -> None:
    """OVH-provider operator commands (inspection, debugging)."""


@ovh.command(name="list")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="ovh",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(endpoint, credentials, ovh_subsidiary, project_id). When the block does not "
        "exist, OvhProviderConfig class defaults are used as the fallback; credentials "
        "still fall back to env / ~/.ovh.conf when unset."
    ),
)
@optgroup.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help=(
        "List every VPS the account owns, not just those tagged for mngr. "
        "By default, only VPSes tagged with `mngr-provider` are shown."
    ),
)
@add_common_options
@click.pass_context
def list_command(ctx: click.Context, **_kwargs: Any) -> None:
    """List OVH VPSes visible to this account, with mngr-relevant details.

    Columns:
      SERVICENAME, PLAN, DATACENTER, STATE, EXPIRATION,
      CANCEL?, MNGR-PROVIDER, MNGR-HOST-ID, RECYCLING-BY.
    """
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="ovh list",
        command_class=_OvhListCliOptions,
    )
    show_all = opts.show_all
    config = _resolve_provider_config(mngr_ctx, opts.provider)
    client = build_ovh_client(config)
    if client.is_unconfigured:
        raise click.ClickException(
            "OVH credentials not configured. Set OVH_APPLICATION_KEY/SECRET/CONSUMER_KEY "
            "(or OAuth2 client_id/client_secret) or populate ~/.ovh.conf."
        )

    service_names = _safely_list_instances(client)
    if not service_names:
        click.echo("(no OVH VPSes found for this account)")
        return

    tag_map = _build_tag_map_by_service_name(client)
    rows = _collect_rows_in_parallel(client, service_names, tag_map)
    if not show_all:
        rows = [r for r in rows if r["mngr_provider"]]
        if not rows:
            click.echo("(no mngr-tagged OVH VPSes for this account; pass --all to see untagged ones)")
            return

    _print_rows(rows)


def _safely_list_instances(client: OvhVpsClient) -> list[str]:
    try:
        return client.list_instances()
    except VpsApiError as e:
        raise click.ClickException(f"OVH /vps listing failed: {e}") from e


def _build_tag_map_by_service_name(client: OvhVpsClient) -> dict[str, Mapping[str, str]]:
    """One IAM-resource call gives us tags for every VPS in the account at once."""
    try:
        resources = list_vps_resources(client)
    except MngrError as e:
        logger.warning("OVH IAM tag listing failed; rendering rows without tag info: {}", e)
        return {}
    return {r.name: r.tags for r in resources}


def _collect_rows_in_parallel(
    client: OvhVpsClient,
    service_names: list[str],
    tag_map: dict[str, Mapping[str, str]],
) -> list[dict[str, str]]:
    """Fan out per-VPS detail fetches with a small worker pool."""
    cg = ConcurrencyGroup(name="mngr-ovh-list")
    rows: list[dict[str, str]] = []
    with (
        cg,
        ConcurrencyGroupExecutor(
            parent_cg=cg,
            name="mngr-ovh-list-fetch",
            max_workers=min(_MAX_PARALLEL_VPS_FETCHES, max(1, len(service_names))),
        ) as executor,
    ):
        futures = [executor.submit(_row_for, client, name, tag_map.get(name, {})) for name in service_names]
        for future in futures:
            try:
                rows.append(future.result())
            except MngrError as e:
                logger.warning("Failed to fetch row for one VPS: {}", e)
    rows.sort(key=lambda r: r["service_name"])
    return rows


def _row_for(client: OvhVpsClient, service_name: str, tags: Mapping[str, str]) -> dict[str, str]:
    vps = _safe_get_instance(client, service_name)
    info = _safe_get_service_info(client, service_name)
    model = (vps.get("model") or {}) if vps else {}
    renew = (info.get("renew") or {}) if info else {}
    return {
        "service_name": service_name,
        "plan": str(model.get("name", "")),
        "datacenter": str(vps.get("zone", "") if vps else ""),
        "state": str(vps.get("state", "?") if vps else "?"),
        "expiration": str(info.get("expiration", "?") if info else "?"),
        "cancel": "yes" if renew.get("deleteAtExpiration") else "no",
        "mngr_provider": str(tags.get(MNGR_PROVIDER_TAG_KEY, "")),
        "mngr_host_id": str(tags.get(MNGR_HOST_ID_TAG_KEY, "")),
        "recycling_by": str(tags.get(MNGR_RECYCLING_LOCK_TAG_KEY, "")),
    }


def _safe_get_instance(client: OvhVpsClient, service_name: str) -> dict[str, Any] | None:
    try:
        return client.get_instance(VpsInstanceId(service_name))
    except VpsApiError as e:
        logger.debug("Failed to fetch /vps/{}: {}", service_name, e)
        return None


def _safe_get_service_info(client: OvhVpsClient, service_name: str) -> dict[str, Any] | None:
    try:
        return client.get_service_info(service_name)
    except VpsApiError as e:
        logger.debug("Failed to fetch /vps/{}/serviceInfos: {}", service_name, e)
        return None


def _print_rows(rows: list[dict[str, str]]) -> None:
    """Render rows as a left-aligned text table on stdout."""
    headers = {
        "service_name": "SERVICENAME",
        "plan": "PLAN",
        "datacenter": "DATACENTER",
        "state": "STATE",
        "expiration": "EXPIRATION",
        "cancel": "CANCEL?",
        "mngr_provider": "MNGR-PROVIDER",
        "mngr_host_id": "MNGR-HOST-ID",
        "recycling_by": "RECYCLING-BY",
    }
    widths = {key: len(label) for key, label in headers.items()}
    for row in rows:
        for key in headers:
            widths[key] = max(widths[key], len(row[key]))
    header_line = "  ".join(headers[key].ljust(widths[key]) for key in headers)
    click.echo(header_line)
    for row in rows:
        click.echo("  ".join(row[key].ljust(widths[key]) for key in headers))
