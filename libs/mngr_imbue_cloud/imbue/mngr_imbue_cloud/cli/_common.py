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
import tomllib
from pathlib import Path
from typing import Any
from typing import Final
from typing import NoReturn

import click
from loguru import logger
from pydantic import AnyUrl

from imbue.mngr_imbue_cloud.config import CONNECTOR_URL_ENV_VAR
from imbue.mngr_imbue_cloud.config import get_active_profile_dir
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount

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
    """Resolve the connector URL: explicit flag > env var.

    There is no baked-in default; callers must either pass ``--connector-url``
    or set ``MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL`` in the environment.
    minds always passes the env var via the ImbueCloudCli wrapper, so this
    only fires when someone invokes the CLI directly without setting up.
    """
    if override:
        return override.rstrip("/")
    env_value = os.environ.get(CONNECTOR_URL_ENV_VAR)
    if env_value:
        return env_value.rstrip("/")
    fail_with_json(
        f"No connector URL configured: pass --connector-url <url> or set ${CONNECTOR_URL_ENV_VAR}.",
        error_class="UsageError",
        exit_code=2,
    )


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


# Env var name a minds-activated shell uses to flag the pool host DSN for the
# activated env. Mirrors the field written into ``~/.minds-<env>/secrets.toml``
# by ``minds env deploy`` so an operator can also point us at a one-off DSN by
# exporting it directly.
_MINDS_HOST_POOL_DSN_ENV_VAR: Final[str] = "MINDS_HOST_POOL_DSN"
# Env vars the minds bootstrap exports on ``minds env activate`` so we can locate
# the per-env secrets.toml without importing any minds module (these CLIs live in
# mngr_imbue_cloud and are intentionally decoupled from the minds package).
_MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
_MINDS_PREFIX: Final[str] = "minds"


def _read_activated_minds_host_pool_dsn() -> str | None:
    """Return the activated minds env's NEON_HOST_POOL_DSN, or None.

    Walks the same on-disk layout ``minds env deploy`` writes:

        $HOME/.<MINDS_ROOT_NAME>/secrets.toml -> [secrets].NEON_HOST_POOL_DSN

    Returns None when ``MINDS_ROOT_NAME`` is unset, when the env root is
    production (``MINDS_ROOT_NAME=minds``, no per-env secrets.toml), when the file
    doesn't exist, or when the field is missing / empty. All map to "this CLI has
    no opinion -- caller must pass ``--database-url`` or set ``MINDS_HOST_POOL_DSN``."
    """
    root_name = os.environ.get(_MINDS_ROOT_NAME_ENV_VAR)
    if not root_name or root_name == _MINDS_PREFIX:
        return None
    secrets_path = Path.home() / f".{root_name}" / "secrets.toml"
    if not secrets_path.is_file():
        return None
    try:
        raw = tomllib.loads(secrets_path.read_text())
    except OSError as exc:
        logger.warning("Could not read {} for pool DSN resolution: {}", secrets_path, exc)
        return None
    except tomllib.TOMLDecodeError as exc:
        logger.warning(
            "Could not parse {} for pool DSN resolution ({}); pass --database-url explicitly.",
            secrets_path,
            exc,
        )
        return None
    secrets_block = raw.get("secrets")
    if not isinstance(secrets_block, dict):
        return None
    dsn = secrets_block.get("NEON_HOST_POOL_DSN")
    if not isinstance(dsn, str) or not dsn:
        return None
    return dsn


def resolve_pool_database_url(explicit: str | None) -> str:
    """Resolve the pool DSN for an admin pool/server command.

    Precedence (highest first): explicit ``--database-url``, then
    ``$MINDS_HOST_POOL_DSN``, then the activated minds env's ``secrets.toml``
    ``NEON_HOST_POOL_DSN`` (written by ``minds env deploy`` for dev envs), else a
    useful error. ``$DATABASE_URL`` is intentionally NOT consulted (a generic env
    var that might point at an unrelated DB); ``MINDS_HOST_POOL_DSN`` is the
    explicit opt-in for non-activated operators.
    """
    if explicit:
        return explicit
    env_value = os.environ.get(_MINDS_HOST_POOL_DSN_ENV_VAR)
    if env_value:
        return env_value
    activated_dsn = _read_activated_minds_host_pool_dsn()
    if activated_dsn:
        return activated_dsn
    fail_with_json(
        "No pool DSN available. Either pass --database-url explicitly, export "
        f"{_MINDS_HOST_POOL_DSN_ENV_VAR}=<dsn>, or `minds env activate <dev-env>` "
        "first (deploys write the DSN into the per-env secrets.toml).",
        error_class="UsageError",
    )


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
