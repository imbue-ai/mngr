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
from loguru import logger

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_deploy_config

PRODUCTION_ENV_NAME: Final[str] = "production"
STAGING_ENV_NAME: Final[str] = "staging"
DEV_TIER: Final[str] = "dev"
CI_TIER: Final[str] = "ci"


def tier_for_env_name(env_name: str) -> str:
    """Hard-coded env-name -> tier mapping.

    ``production`` -> ``production``; ``staging`` -> ``staging``;
    names starting with ``ci-`` -> ``ci`` (the CI-orchestrator-minted
    ephemeral envs); everything else (the convention is
    ``dev-<user>-<suffix>``) -> ``dev``. Shared by ``minds env``
    (deploy/destroy dispatch) and ``minds pool`` (tier-scoped Vault
    reads for the OVH admin credentials).
    """
    if env_name == PRODUCTION_ENV_NAME:
        return PRODUCTION_ENV_NAME
    if env_name == STAGING_ENV_NAME:
        return STAGING_ENV_NAME
    if env_name.startswith(f"{CI_TIER}-"):
        return CI_TIER
    return DEV_TIER


def modal_profile_for_tier_or_none(tier: str) -> str | None:
    """Return the Modal profile name (``modal_workspace``) for ``tier``, or None.

    Reads ``apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`` and
    pulls the committed ``modal_workspace`` value. We export this as
    ``MODAL_PROFILE`` from ``minds env activate`` so every ``modal``
    CLI shellout (deploy, secret create, environment create, etc.) is
    pinned to the right workspace regardless of what's marked
    ``active = true`` in ``~/.modal.toml``.

    Returns ``None`` when the tier has no deploy.toml on disk (e.g.
    a freshly-checked-out tree before tier config is committed) or
    the committed value is still the literal ``CHANGE_ME`` placeholder.
    Activation proceeds without ``MODAL_PROFILE`` in that case so the
    operator's existing ``modal token set`` setup still works.

    Lives here (rather than in :mod:`imbue.minds.cli.env`) so non-CLI
    callers -- in particular the deployment_tests helpers that need to
    target the right Modal workspace from a pytest subprocess -- can
    derive the profile via the same logic ``minds env activate`` uses
    instead of hardcoding ``"minds-dev"``.
    """
    try:
        deploy_config = load_deploy_config(tier)
    except EnvConfigError as exc:
        logger.warning(
            "Could not load deploy.toml for tier {!r} ({}); MODAL_PROFILE will not be exported. "
            "modal shellouts will fall back to ~/.modal.toml's active profile.",
            tier,
            exc,
        )
        return None
    workspace = str(deploy_config.modal_workspace)
    if not workspace or workspace == "CHANGE_ME":
        return None
    return workspace


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
