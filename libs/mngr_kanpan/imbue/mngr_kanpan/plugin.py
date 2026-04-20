import types
from collections.abc import Sequence
from typing import Any

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_kanpan import hookspecs as kanpan_hookspecs
from imbue.mngr_kanpan.cli import kanpan
from imbue.mngr_kanpan.data_sources.git_info import GitInfoDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSource
from imbue.mngr_kanpan.data_sources.github import GitHubDataSourceConfig
from imbue.mngr_kanpan.data_sources.labels import LabelColumnConfig
from imbue.mngr_kanpan.data_sources.labels import LabelsDataSource
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathsDataSource
from imbue.mngr_kanpan.data_sources.shell import ShellCommandConfig
from imbue.mngr_kanpan.data_sources.shell import ShellCommandDataSource
from imbue.mngr_kanpan.data_types import KanpanPluginConfig

register_plugin_config("kanpan", KanpanPluginConfig)


def _is_source_enabled(config: KanpanPluginConfig, name: str) -> bool:
    """Check if a data source is enabled in the plugin config."""
    source_config = config.data_sources.get(name)
    if source_config is None:
        return True
    return source_config.get("enabled", True)


@hookimpl
def register_hookspecs() -> types.ModuleType | None:
    """Register kanpan-specific hookspecs."""
    return kanpan_hookspecs


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the kanpan command with mngr."""
    return [kanpan]


@hookimpl
def kanpan_data_sources(mngr_ctx: MngrContext) -> Sequence[Any] | None:
    """Register built-in data sources for kanpan board refresh.

    Each source checks its own enabled status from config before being included.
    """
    config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)

    sources: list[Any] = []

    if _is_source_enabled(config, "repo_paths"):
        sources.append(RepoPathsDataSource())

    if _is_source_enabled(config, "git_info"):
        sources.append(GitInfoDataSource())

    if _is_source_enabled(config, "github"):
        github_raw = config.data_sources.get("github") or {}
        sources.append(GitHubDataSource(config=GitHubDataSourceConfig(**github_raw)))

    # Label-backed columns from config
    for field_key, col_config in config.columns.items():
        if not isinstance(col_config, dict):
            continue
        header = col_config.get("header", field_key.upper())
        colors = col_config.get("colors", {})
        label_key = col_config.get("label_key", field_key)
        sources.append(
            LabelsDataSource(
                field_key=field_key,
                config=LabelColumnConfig(header=header, label_key=label_key, colors=colors),
            )
        )

    # Shell command data sources from config
    for field_key, shell_config in config.shell_commands.items():
        sources.append(ShellCommandDataSource(field_key=field_key, config=ShellCommandConfig(**shell_config)))

    return sources
