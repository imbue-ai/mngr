"""Shared helpers for imbue_cloud CLI subcommands.

Each subcommand needs to find the on-disk session store and to build a
connector client. We deliberately don't bring up the full mngr command
context here -- these commands are plugin-local and don't need to load
plugins, agent types, or providers.
"""

import functools as _functools
import json as _json
import os
import sys
from pathlib import Path
from typing import Any
from typing import NoReturn

import click
from pydantic import AnyUrl

from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.config import CONNECTOR_URL_ENV_VAR
from imbue.mngr_imbue_cloud.config import get_active_profile_dir
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import get_default_connector_url
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore

_DEFAULT_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"


def get_default_host_dir() -> Path:
    """Resolve the active mngr default host dir.

    Honors ``MNGR_HOST_DIR`` (the same env var mngr uses), defaulting to ``~/.mngr``.
    """
    env_value = os.environ.get(_DEFAULT_HOST_DIR_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return Path("~/.mngr").expanduser()


def make_session_store() -> ImbueCloudSessionStore:
    """Build a session store rooted at the current mngr profile.

    Plugin CLI subcommands run outside the full ``MngrContext`` (we don't
    load plugins/agent types/providers for plugin-local commands), so we
    resolve the active profile by reading ``<host_dir>/config.toml``
    directly, mirroring what mngr does internally.
    """
    profile_dir = get_active_profile_dir(get_default_host_dir())
    return ImbueCloudSessionStore(sessions_dir=get_sessions_dir(profile_dir))


def resolve_connector_url(override: str | None) -> str:
    """Resolve the connector URL: explicit flag > env var > baked default."""
    if override:
        return override.rstrip("/")
    env_value = os.environ.get(CONNECTOR_URL_ENV_VAR)
    if env_value:
        return env_value.rstrip("/")
    return get_default_connector_url().rstrip("/")


def make_connector_client(connector_url: str | None) -> ImbueCloudConnectorClient:
    return ImbueCloudConnectorClient(base_url=AnyUrl(resolve_connector_url(connector_url)))


def emit_json(data: Any) -> None:
    """Print a JSON-serialisable object to stdout, followed by a newline."""
    click.echo(_json.dumps(data, indent=2, default=str))


def fail_with_json(message: str, *, exit_code: int = 1, **extra: Any) -> NoReturn:
    """Print a JSON error body to stderr and exit with the given code."""
    body: dict[str, Any] = {"error": message}
    body.update(extra)
    click.echo(_json.dumps(body, indent=2, default=str), err=True)
    sys.exit(exit_code)


def parse_account(value: str) -> ImbueCloudAccount:
    try:
        return ImbueCloudAccount(value)
    except ValueError as exc:
        fail_with_json(f"Invalid account email: {exc}")


def resolve_account_or_active(store: ImbueCloudSessionStore, value: str | None) -> ImbueCloudAccount:
    """Parse ``value`` if present, else fall back to the active account.

    Used by every ``mngr imbue_cloud ...`` sub-command that takes
    ``--account``. ``--account`` can be omitted; we resolve to the
    active account written by ``auth use`` (or implicitly by ``auth
    signin``). Errors with a helpful message when neither path produces
    an account, listing the signed-in candidates if any.
    """
    if value:
        return parse_account(value)
    active = store.get_active_account()
    if active is not None:
        return active
    known = store.list_accounts()
    if known:
        candidate_list = ", ".join(str(account) for account in known)
        fail_with_json(
            "No active account is set; pass --account <email> or run `mngr imbue_cloud "
            f"auth use --account <email>`. Signed-in accounts: {candidate_list}",
            error_class="UsageError",
        )
    fail_with_json(
        "No imbue_cloud accounts are signed in; run `mngr imbue_cloud auth signin --account <email>` first.",
        error_class="UsageError",
    )


def handle_imbue_cloud_errors(func):
    """Decorator that translates ImbueCloudError into structured JSON failures."""

    @_functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ImbueCloudError as exc:
            fail_with_json(str(exc), error_class=type(exc).__name__)

    return wrapper
