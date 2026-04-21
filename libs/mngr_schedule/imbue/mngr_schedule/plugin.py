import os
from collections.abc import Sequence
from pathlib import Path

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.cli.commands import schedule


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the schedule command with mngr."""
    return [schedule]


@hookimpl
def get_files_for_deploy(
    mngr_ctx: MngrContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """Register mngr-specific config files for scheduled deployments.

    Includes top-level mngr config and profile files (settings.toml, user_id),
    but not provider subdirectories -- those are handled by provider plugins
    via their own get_files_for_deploy implementations.

    Also includes project-local settings (.mngr/settings.local.toml) when
    include_project_settings is True.
    """
    files: dict[Path, Path | str] = {}

    if include_user_settings:
        user_home = Path.home()

        # Top-level mngr config (contains the active profile_id). Read from the
        # deployer's actual host_dir, not the hardcoded ~/.mngr -- a deployer with
        # MNGR_ROOT_NAME=minds or similar has their config at ~/.minds/config.toml,
        # and staging from the wrong path silently drops it, which forces the cron
        # container to mint a fresh profile at fire time.
        deployer_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
        mngr_config = deployer_host_dir / "config.toml"
        if mngr_config.is_file():
            # relative_to raises ValueError if host_dir is outside $HOME; that's a
            # misconfiguration worth surfacing since the profile files branch below
            # assumes the same (any path not under $HOME would also fail there).
            relative = mngr_config.relative_to(user_home)
            files[Path(f"~/{relative}")] = mngr_config

        # Top-level profile files (settings.toml, user_id) but not provider
        # subdirectories -- those are handled by provider plugins themselves.
        profile_dir = mngr_ctx.profile_dir
        if profile_dir.is_dir():
            for file_path in profile_dir.iterdir():
                if file_path.is_file():
                    relative = file_path.relative_to(user_home)
                    files[Path(f"~/{relative}")] = file_path

    if include_project_settings:
        # Include unversioned project-local mngr settings.
        # This file is typically gitignored and contains local overrides.
        local_config = repo_root / ".mngr" / "settings.local.toml"
        if local_config.is_file():
            relative = local_config.relative_to(repo_root)
            files[Path(str(relative))] = local_config

    return files


@hookimpl
def modify_env_vars_for_deploy(
    mngr_ctx: MngrContext,
    env_vars: dict[str, str],
) -> None:
    """Propagate the deployer's MNGR_ROOT_NAME into the scheduled container.

    The cron_runner image bakes the deployer's config.toml, profile settings,
    and user_id under `~/.<deployer_root_name>/...` (via get_files_for_deploy).
    If the container's mngr runs with no MNGR_ROOT_NAME, it defaults to "mngr"
    and looks at ~/.mngr/config.toml -- missing the baked files when the
    deployer's root_name is non-default (minds, mngr-changelog-schedule, etc).
    The miss forces get_or_create_profile_dir + get_or_create_user_id down the
    "fresh install" branch: mint a uuid4 profile + uuid4 user_id, which causes
    the Modal backend to create a new orphan `mngr-<uuid>` environment on every
    cron fire.

    Propagating MNGR_ROOT_NAME keeps the container's lookup path aligned with
    where files were actually staged, so the deployer's full config (profile
    settings.toml, user_id, everything else) is used and the nested mngr calls
    land in the deployer's actual Modal env (derived prefix = `{root_name}-`)
    rather than a default `mngr-` env.

    setdefault so an explicit --pass-env / --env-file value still wins -- a
    caller can still redirect a scheduled trigger to a different root_name if
    they really mean to.
    """
    root_name = os.environ.get("MNGR_ROOT_NAME")
    if root_name is not None:
        env_vars.setdefault("MNGR_ROOT_NAME", root_name)
