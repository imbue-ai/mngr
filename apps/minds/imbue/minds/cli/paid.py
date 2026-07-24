"""``minds paid {add,remove,list}`` -- env-aware wrapper around ``mngr imbue_cloud admin paid email``.

From the activated minds env this resolves the two things the underlying admin command
needs, so the operator never hand-passes them:

* the connector base URL, from the activated tier's ``client.toml`` (the path the env
  activation exports as ``MINDS_CLIENT_CONFIG_PATH``); and
* the admin API key (``MINDS_ADMIN_KEY``, with the deprecated ``MINDS_PAID_ADMIN_KEY``
  spelling still accepted while Vault entries migrate), from the activated tier's
  ``<vault_path_prefix>/supertokens`` Vault entry -- the same value the connector loads
  as a Modal Secret, injected into the subprocess env (never onto the command line).

Transport is a subprocess to ``mngr imbue_cloud admin paid email ...`` to match the rest
of the minds env CLI's mngr invocations.
"""

import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.cli._activated_env import require_activated_env_name
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.mngr.cli.output_helpers import write_human_line

# Env var the admin paid command reads the key from, and the field it lives under in
# the tier's supertokens Vault entry (the connector folds it into the same secret).
# The deprecated MINDS_PAID_ADMIN_KEY spelling is still read (and injected) as a
# fallback while Vault entries and installed mngr versions migrate.
_ADMIN_KEY_ENV_VAR: Final[str] = "MINDS_ADMIN_KEY"
_ADMIN_KEY_VAULT_FIELD: Final[str] = "MINDS_ADMIN_KEY"
_LEGACY_ADMIN_KEY_ENV_VAR: Final[str] = "MINDS_PAID_ADMIN_KEY"
_LEGACY_ADMIN_KEY_VAULT_FIELD: Final[str] = "MINDS_PAID_ADMIN_KEY"
_CLIENT_CONFIG_PATH_ENV_VAR: Final[str] = "MINDS_CLIENT_CONFIG_PATH"
_PAID_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0


def build_admin_paid_email_args(verb_args: Sequence[str], *, connector_url: str) -> list[str]:
    """Compose the ``mngr imbue_cloud admin paid email`` argv (sans the admin key, which goes via env).

    ``verb_args`` is the subcommand + its operands, e.g. ``["add", "a@b.com"]`` or
    ``["list", "--paid-only"]``. Split out so the wiring is unit-testable.
    """
    return ["imbue_cloud", "admin", "paid", "email", *verb_args, "--connector-url", connector_url]


def _resolve_connector_url() -> str:
    """Read the activated env's connector URL from its client.toml (set by env activation)."""
    config_path = os.environ.get(_CLIENT_CONFIG_PATH_ENV_VAR)
    if not config_path:
        raise click.ClickException(
            f'${_CLIENT_CONFIG_PATH_ENV_VAR} is not set; run `eval "$(uv run minds env activate <name>)"` first.'
        )
    return str(load_client_config(Path(config_path)).connector_url)


def admin_key_from_supertokens_secret(secret: Mapping[str, str], vault_prefix: str) -> str:
    """Pick the admin API key out of a tier's ``<vault_prefix>/supertokens`` secret.

    Prefers the ``MINDS_ADMIN_KEY`` field, falling back (with a warning) to the
    deprecated ``MINDS_PAID_ADMIN_KEY`` spelling while Vault entries migrate.
    ``vault_prefix`` is only used in the warning / error messages. Split out of
    :func:`_resolve_admin_key` so the fallback contract is unit-testable
    without a Vault read.
    """
    admin_key = secret.get(_ADMIN_KEY_VAULT_FIELD, "")
    if admin_key:
        return admin_key
    legacy_admin_key = secret.get(_LEGACY_ADMIN_KEY_VAULT_FIELD, "")
    if legacy_admin_key:
        logger.warning(
            "Admin API key found under deprecated Vault field {}; rename it to {} in {}/supertokens",
            _LEGACY_ADMIN_KEY_VAULT_FIELD,
            _ADMIN_KEY_VAULT_FIELD,
            vault_prefix,
        )
        return legacy_admin_key
    raise click.ClickException(
        f"Vault entry {vault_prefix}/supertokens is missing {_ADMIN_KEY_VAULT_FIELD!r}; "
        "the admin API is not enabled for this tier (add the key and redeploy)."
    )


def _resolve_admin_key(env_name: str) -> str:
    """Read the activated tier's admin API key from ``<vault_prefix>/supertokens``.

    Prefers the ``MINDS_ADMIN_KEY`` field, falling back (with a warning) to the
    deprecated ``MINDS_PAID_ADMIN_KEY`` spelling while Vault entries migrate.
    """
    tier = tier_for_env_name(env_name)
    vault_prefix = str(load_deploy_config(tier).vault_path_prefix).rstrip("/")
    secret = read_vault_kv(VaultPath(f"{vault_prefix}/supertokens"))
    return admin_key_from_supertokens_secret(secret, vault_prefix)


def _run_admin_paid_email(verb_args: Sequence[str]) -> None:
    """Resolve connector URL + admin key for the activated env, then run the admin paid command."""
    env_name = require_activated_env_name()
    connector_url = _resolve_connector_url()
    try:
        admin_key = _resolve_admin_key(env_name)
    except VaultReadError as exc:
        raise click.ClickException(f"Could not read the admin API key from Vault for env '{env_name}': {exc}") from exc
    args = build_admin_paid_email_args(verb_args, connector_url=connector_url)
    full_command = ["mngr", *args]
    logger.info("Running: {}", " ".join(full_command))
    cg = ConcurrencyGroup(name="minds-paid")
    with cg:
        # Inject the key under both spellings so an older installed mngr (which
        # only reads the deprecated name) keeps working during the migration.
        result = cg.run_process_to_completion(
            command=full_command,
            timeout=_PAID_COMMAND_TIMEOUT_SECONDS,
            is_checked_after=False,
            env={**os.environ, _ADMIN_KEY_ENV_VAR: admin_key, _LEGACY_ADMIN_KEY_ENV_VAR: admin_key},
        )
    if result.stdout.strip():
        write_human_line(result.stdout.rstrip())
    if result.returncode != 0:
        raise click.ClickException(
            f"mngr imbue_cloud admin paid email failed (exit {result.returncode}): {result.stderr.strip()}"
        )


@click.group(name="paid")
def paid() -> None:
    """Manage paid users for the activated minds env (wraps ``mngr imbue_cloud admin paid email``)."""


@paid.command(name="add")
@click.argument("email")
def paid_add(email: str) -> None:
    """Add (or reactivate) a paid user email."""
    _run_admin_paid_email(["add", email])


@paid.command(name="remove")
@click.argument("email")
def paid_remove(email: str) -> None:
    """Soft-remove a paid user email (sets is_paid=false)."""
    _run_admin_paid_email(["remove", email])


@paid.command(name="list")
@click.option("--paid-only", is_flag=True, default=False, help="Only show currently-active (is_paid) emails.")
def paid_list(paid_only: bool) -> None:
    """List paid user emails."""
    _run_admin_paid_email(["list", "--paid-only"] if paid_only else ["list"])
