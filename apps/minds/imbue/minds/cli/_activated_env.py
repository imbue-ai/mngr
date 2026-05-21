"""Shared activation-check helper for minds CLI subcommands.

``minds env deploy`` / ``destroy`` and ``minds pool {create,list,destroy}``
all refuse to run unless the calling shell has been activated against a
specific minds env (``MINDS_ROOT_NAME`` set + matching the bootstrap
pattern). The check + error-message text is identical across both
subcommands, so it lives here rather than being duplicated.

``minds env deploy`` / ``destroy`` / ``recover`` additionally require
*deploy-mode* activation (``minds env activate --deploy``), which pins
``MODAL_PROFILE`` to the tier's Modal workspace. The deploy-mode gate
also lives here so all three commands share the same refusal text.
"""

import os
import tomllib
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_deploy_config

# Env var the activation exports set to pin every ``modal`` CLI
# shellout to a specific workspace, regardless of which profile is
# marked ``active = true`` in ``~/.modal.toml``. Only exported by
# ``minds env activate --deploy``; plain ``minds env activate`` emits
# ``unset MODAL_PROFILE`` so a previously-deploy-activated shell
# reverts cleanly.
MODAL_PROFILE_ENV_VAR: Final[str] = "MODAL_PROFILE"

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


def _modal_config_path() -> Path:
    """Resolve the path the Modal SDK reads for its config.

    Mirrors Modal SDK's ``modal/config.py`` selection rule
    (``os.environ.get('MODAL_CONFIG_PATH') or os.path.expanduser('~/.modal.toml')``)
    with one deliberate extension: we also run ``expanduser`` on the
    ``MODAL_CONFIG_PATH`` override. The SDK passes the override straight
    to ``open()``, so an operator who sets ``MODAL_CONFIG_PATH=~/foo.toml``
    would crash with ENOENT on the next ``modal …`` shellout -- our
    ``expanduser`` lets us surface that as a clean validation refusal
    (or accept the file if it actually exists at the expanded path)
    instead of a confusing SDK auth failure later.

    Honoring ``MODAL_CONFIG_PATH`` is essential -- if we hardcoded
    ``~/.modal.toml`` while the operator had pointed Modal at a
    different file via the env var, our deploy-mode validation would
    read a different file than the subsequent ``modal …`` shellout
    actually uses, defeating the whole point of pre-validation.
    """
    override = os.environ.get("MODAL_CONFIG_PATH")
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".modal.toml"


def validate_modal_profile_exists_in_modal_toml(workspace: str) -> None:
    """Raise ``ClickException`` if Modal's config file has no profile named ``workspace``.

    Called by ``minds env activate --deploy`` so the operator hits a clean
    error at activation time (with a copy-pasteable ``modal token set``
    hint) instead of a confusing Modal SDK auth failure on the first
    subsequent ``modal …`` shellout.

    Reads the same file the Modal SDK reads: ``$MODAL_CONFIG_PATH`` if
    set, otherwise ``$HOME/.modal.toml`` (see :func:`_modal_config_path`).
    The file is TOML with one section per profile, keyed by the workspace
    name; a matching profile is any section whose key equals ``workspace``
    and whose value is a table.

    Raises a distinct ``ClickException`` for each of three failure modes
    -- file missing, file unparseable, profile section missing -- so the
    operator's error message names the actual cause. All three messages
    include a copy-pasteable ``modal token set --profile <workspace>``
    hint and name the exact config path being checked (so an operator
    with a non-default ``MODAL_CONFIG_PATH`` doesn't get a misleading
    pointer to ``~/.modal.toml``).
    """
    modal_toml = _modal_config_path()
    if not modal_toml.is_file():
        raise click.ClickException(
            f"Modal config file {str(modal_toml)!r} not found, so the Modal profile "
            f"{workspace!r} required for deploy-mode activation cannot exist. Run "
            f"`modal token set --profile {workspace}` (after `uvx modal token new` if you "
            f"have no Modal account on this machine yet) and re-run."
        )
    try:
        data = tomllib.loads(modal_toml.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.ClickException(
            f"Could not read Modal config file {str(modal_toml)!r} ({exc}); cannot verify "
            f"the {workspace!r} profile required for deploy-mode activation."
        ) from exc
    if not isinstance(data.get(workspace), dict):
        raise click.ClickException(
            f"Modal config file {str(modal_toml)!r} has no profile named {workspace!r}, "
            f"which deploy-mode activation of this tier requires. Run "
            f"`modal token set --profile {workspace}` (after `uvx modal token new` if you "
            f"have no Modal account on this machine yet) and re-run."
        )


def require_deploy_mode_activation(*, env_name: str, tier: str) -> None:
    """Raise ``ClickException`` unless the shell is deploy-activated for this tier.

    ``minds env deploy`` / ``destroy`` / ``recover`` all call this so the
    operator cannot accidentally run a deploy with the wrong (or no)
    ``MODAL_PROFILE`` pinned -- which previously caused silent misroutes
    to the wrong Modal workspace.

    Deploy-activated means ``MODAL_PROFILE`` is set in the environment
    and equals the tier's ``modal_workspace`` from ``deploy.toml``.
    Tiers with no committed ``modal_workspace`` (deploy.toml missing or
    the literal ``CHANGE_ME`` placeholder) skip the gate -- there is no
    workspace to pin to in that case.
    """
    expected_workspace = modal_profile_for_tier_or_none(tier)
    if expected_workspace is None:
        return
    current = os.environ.get(MODAL_PROFILE_ENV_VAR, "")
    if current == expected_workspace:
        return
    reactivate_hint = (
        f"Re-activate this shell with deploy-mode: "
        f'`eval "$(uv run minds env activate --deploy {env_name})"` and re-run.'
    )
    if not current:
        raise click.ClickException(
            f"This shell was activated for use only -- minds env deploy/destroy/recover "
            f"require MODAL_PROFILE pinned to {expected_workspace!r}, but it is unset. "
            f"(Activation now distinguishes use-mode from deploy-mode; plain "
            f"`minds env activate <name>` no longer exports MODAL_PROFILE.) "
            f"{reactivate_hint}"
        )
    raise click.ClickException(
        f"MODAL_PROFILE={current!r} does not match this tier's modal_workspace "
        f"{expected_workspace!r}; minds env deploy/destroy/recover refuse to run with a "
        f"mismatched Modal profile, since it would silently misroute the deploy to the "
        f"wrong Modal workspace. {reactivate_hint}"
    )


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
