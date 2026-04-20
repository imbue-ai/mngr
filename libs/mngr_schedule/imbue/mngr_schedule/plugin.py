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

        # ~/.mngr/config.toml (top-level mngr config with profile ID)
        mngr_config = user_home / ".mngr" / "config.toml"
        if mngr_config.exists():
            files[Path("~/.mngr/config.toml")] = mngr_config

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
    """Anchor the scheduled container to the deployer's Modal environment.

    Scheduled triggers fire inside an ephemeral Modal container that has no
    persistent profile. Without these vars, the nested `mngr` invocation in
    cron_runner would mint a fresh uuid4 user_id via get_or_create_user_id
    and the modal backend would create an orphan `mngr-<uuid>` env on every
    fire (these orphans match no cleanup pattern and accumulate forever).

    Setting both here (rather than relying solely on the baked profile files
    from get_files_for_deploy) keeps the anchor intact even when the deploy
    is run with --exclude-user-settings, and gives the hook last-write
    precedence over --pass-env so an accidental --pass-env MNGR_USER_ID
    can't re-open the leak.
    """
    env_vars["MNGR_PREFIX"] = mngr_ctx.config.prefix
    env_vars["MNGR_USER_ID"] = mngr_ctx.get_profile_user_id()
