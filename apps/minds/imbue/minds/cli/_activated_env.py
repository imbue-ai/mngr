"""Shared activation-check helper for minds CLI subcommands.

``minds env deploy`` / ``destroy`` and ``minds pool {create,list,destroy}``
all refuse to run unless the calling shell has been activated against a
specific minds env (``MINDS_ROOT_NAME`` set + matching the bootstrap
pattern). The check + error-message text is identical across both
subcommands, so it lives here rather than being duplicated.
"""

import os
from typing import Final

import click

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env

PRODUCTION_ENV_NAME: Final[str] = "production"
STAGING_ENV_NAME: Final[str] = "staging"
DEV_TIER: Final[str] = "dev"


def tier_for_env_name(env_name: str) -> str:
    """Hard-coded env-name -> tier mapping.

    ``production`` -> ``production``; ``staging`` -> ``staging``;
    everything else (the convention is ``<user>-<suffix>``) -> ``dev``.
    Shared by ``minds env`` (deploy/destroy dispatch) and ``minds pool``
    (tier-scoped Vault reads for the OVH admin credentials).
    """
    if env_name == PRODUCTION_ENV_NAME:
        return PRODUCTION_ENV_NAME
    if env_name == STAGING_ENV_NAME:
        return STAGING_ENV_NAME
    return DEV_TIER


def require_activated_env_name() -> str:
    """Return the activated env name or raise ``ClickException``.

    Used by ``minds env deploy`` / ``destroy`` and ``minds pool ...`` to
    refuse when no env has been activated. Mirrors the bootstrap's
    :func:`is_minds_root_name_set_to_active_env` check.
    """
    if not is_minds_root_name_set_to_active_env():
        raise click.ClickException(
            "No minds env is activated in this shell. Run "
            '`eval "$(uv run minds env activate <name>)"` first '
            "(e.g. `dev-<your-user>` for your personal dev env, or "
            "`staging` / `production`)."
        )
    try:
        return env_name_from_root_name(os.environ[MINDS_ROOT_NAME_ENV_VAR])
    except BootstrapError as exc:
        # Should be unreachable -- ``is_minds_root_name_set_to_active_env``
        # already validated the value matches the pattern. Guarded
        # anyway so a future drift between the two doesn't surface as
        # a confusing AttributeError.
        raise click.ClickException(str(exc)) from exc
