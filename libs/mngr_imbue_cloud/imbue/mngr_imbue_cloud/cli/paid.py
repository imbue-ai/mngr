"""`mngr imbue_cloud admin paid ...` -- operator-only paid-list management.

Manages the connector's ``paid_domains`` / ``paid_emails`` tables via the
admin CRUD API. Authenticated by the fixed ``MINDS_PAID_ADMIN_KEY`` API key
(NOT a SuperTokens session); domains and emails are managed separately.

A user counts as "paid" when their verified email is matched by an active
(``is_paid = true``) row in either table: an exact full-email match in the
emails list, or an exact domain match (the part after ``@``) in the domains
list. "remove" is a soft delete (sets ``is_paid = false``); "list" shows all
rows with their status unless ``--paid-only`` is passed.
"""

import os
from typing import Callable
from typing import Final

import click
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.cli._common import make_connector_client
from imbue.mngr_imbue_cloud.data_types import PaidListEntry

_PAID_ADMIN_KEY_ENV_VAR: Final[str] = "MINDS_PAID_ADMIN_KEY"


def _resolve_admin_api_key(explicit: str | None) -> SecretStr:
    """Resolve the paid-list admin API key: explicit flag > ``MINDS_PAID_ADMIN_KEY``."""
    if explicit:
        return SecretStr(explicit)
    env_value = os.environ.get(_PAID_ADMIN_KEY_ENV_VAR)
    if env_value:
        return SecretStr(env_value)
    fail_with_json(
        f"No paid-list admin API key: pass --api-key or set ${_PAID_ADMIN_KEY_ENV_VAR}.",
        error_class="UsageError",
        exit_code=2,
    )
    raise AssertionError("unreachable")


def _paid_auth_options(func: Callable[..., None]) -> Callable[..., None]:
    """Attach the shared ``--connector-url`` / ``--api-key`` options to a command."""
    func = click.option(
        "--connector-url",
        default=None,
        help="Connector base URL. Defaults to $MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL.",
    )(func)
    func = click.option(
        "--api-key",
        default=None,
        help=f"Paid-list admin API key. Defaults to ${_PAID_ADMIN_KEY_ENV_VAR}.",
    )(func)
    return func


def _emit_entries(entries: list[PaidListEntry]) -> None:
    emit_json([entry.model_dump() for entry in entries])


@click.group(name="paid")
def paid() -> None:
    """Manage paid domains / emails (requires the MINDS_PAID_ADMIN_KEY API key)."""


@paid.group(name="domain")
def domain() -> None:
    """Add / remove / list paid domains (e.g. ``imbue.com``)."""


@paid.group(name="email")
def email() -> None:
    """Add / remove / list paid individual email addresses."""


@domain.command(name="add")
@click.argument("value")
@_paid_auth_options
@handle_imbue_cloud_errors
def domain_add(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Add (or reactivate) a paid domain."""
    client = make_connector_client(connector_url)
    emit_json(client.add_paid_domain(_resolve_admin_api_key(api_key), value))


@domain.command(name="remove")
@click.argument("value")
@_paid_auth_options
@handle_imbue_cloud_errors
def domain_remove(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Soft-remove a paid domain (sets is_paid=false)."""
    client = make_connector_client(connector_url)
    emit_json(client.remove_paid_domain(_resolve_admin_api_key(api_key), value))


@domain.command(name="list")
@click.option("--paid-only", is_flag=True, default=False, help="Only show currently-active (is_paid) domains.")
@_paid_auth_options
@handle_imbue_cloud_errors
def domain_list(paid_only: bool, connector_url: str | None, api_key: str | None) -> None:
    """List paid domains."""
    client = make_connector_client(connector_url)
    _emit_entries(client.list_paid_domains(_resolve_admin_api_key(api_key), paid_only))


@email.command(name="add")
@click.argument("value")
@_paid_auth_options
@handle_imbue_cloud_errors
def email_add(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Add (or reactivate) a paid email."""
    client = make_connector_client(connector_url)
    emit_json(client.add_paid_email(_resolve_admin_api_key(api_key), value))


@email.command(name="remove")
@click.argument("value")
@_paid_auth_options
@handle_imbue_cloud_errors
def email_remove(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Soft-remove a paid email (sets is_paid=false)."""
    client = make_connector_client(connector_url)
    emit_json(client.remove_paid_email(_resolve_admin_api_key(api_key), value))


@email.command(name="list")
@click.option("--paid-only", is_flag=True, default=False, help="Only show currently-active (is_paid) emails.")
@_paid_auth_options
@handle_imbue_cloud_errors
def email_list(paid_only: bool, connector_url: str | None, api_key: str | None) -> None:
    """List paid emails."""
    client = make_connector_client(connector_url)
    _emit_entries(client.list_paid_emails(_resolve_admin_api_key(api_key), paid_only))
