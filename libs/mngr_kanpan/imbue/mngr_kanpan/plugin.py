from collections.abc import Sequence
from typing import Any

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_kanpan.cli import kanpan
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathsDataSource
from imbue.mngr_kanpan.data_sources.shell import ShellCommandConfig
from imbue.mngr_kanpan.data_sources.shell import ShellCommandDataSource
from imbue.mngr_kanpan.data_types import DataSourceConfig
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import ShellCommandSourceConfig

register_plugin_config("kanpan", KanpanPluginConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the kanpan command with mngr."""
    return [kanpan]


@hookimpl
def kanpan_data_sources(mngr_ctx: MngrContext) -> Sequence[Any] | None:
    """Register built-in data sources for kanpan board refresh."""
    config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)

    sources: list[Any] = [
        RepoPathsDataSource(),
        GitInfoDataSource(),
    ]

    # GitHub data source with optional config toggles
    github_config_raw = config.data_sources.get("github")
    if isinstance(github_config_raw, DataSourceConfig):
        github_ds_config = GitHubDataSourceConfig(
            pr=github_config_raw.pr,
            ci=github_config_raw.ci,
            create_pr_url=github_config_raw.create_pr_url,
            conflicts=github_config_raw.conflicts,
            unresolved=github_config_raw.unresolved,
        )
    elif isinstance(github_config_raw, dict):
        github_ds_config = GitHubDataSourceConfig(**github_config_raw)
    else:
        github_ds_config = GitHubDataSourceConfig()
    sources.append(GitHubDataSource(config=github_ds_config))

    # Shell command data sources from config
    for field_key, shell_config in config.shell_commands.items():
        if isinstance(shell_config, ShellCommandSourceConfig):
            sc = ShellCommandConfig(
                name=shell_config.name,
                header=shell_config.header,
                command=shell_config.command,
            )
        elif isinstance(shell_config, dict):
            sc = ShellCommandConfig(**shell_config)
        else:
            continue
        sources.append(ShellCommandDataSource(field_key=field_key, config=sc))

    return sources
