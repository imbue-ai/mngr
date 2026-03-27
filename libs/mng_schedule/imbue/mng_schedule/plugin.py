from collections.abc import Sequence
from pathlib import Path

import click

from imbue.mng import hookimpl
from imbue.mng.config.data_types import MngContext
from imbue.mng_schedule.cli.commands import schedule


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the schedule command with mng."""
    return [schedule]


@hookimpl
def get_files_for_deploy(
    mng_ctx: MngContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """Register mng-specific config files for scheduled deployments.

    Includes top-level mng config and profile files (settings.toml, user_id),
    but not provider subdirectories -- those are handled by provider plugins
    via their own get_files_for_deploy implementations.

    Also includes project-local settings (.mng/settings.local.toml) when
    include_project_settings is True.
    """
    files: dict[Path, Path | str] = {}

    if include_user_settings:
        user_home = Path.home()

        # ~/.mng/config.toml (top-level mng config with profile ID)
        mng_config = user_home / ".mng" / "config.toml"
        if mng_config.exists():
            files[Path("~/.mng/config.toml")] = mng_config

        # Top-level profile files (settings.toml, user_id) but not provider
        # subdirectories -- those are handled by provider plugins themselves.
        profile_dir = mng_ctx.profile_dir
        if profile_dir.is_dir():
            for file_path in profile_dir.iterdir():
                if file_path.is_file():
                    relative = file_path.relative_to(user_home)
                    files[Path(f"~/{relative}")] = file_path

    if include_project_settings:
        # Include unversioned project-local mng settings.
        # This file is typically gitignored and contains local overrides.
        local_config = repo_root / ".mng" / "settings.local.toml"
        if local_config.is_file():
            relative = local_config.relative_to(repo_root)
            files[Path(str(relative))] = local_config

    return files
