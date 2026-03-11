"""Shared types and constants for the tab completion cache.

This module is deliberately lightweight (stdlib + local stdlib-only deps) so
it can be imported by both the cache writer (completion_writer.py, heavy
imports) and the cache reader (cli/complete.py, no heavy imports).
"""

import os
from pathlib import Path
from typing import Final
from typing import NamedTuple

from imbue.mng.config.host_dir import read_default_host_dir

COMPLETION_CACHE_FILENAME: Final[str] = ".command_completions.json"


def get_completion_cache_dir() -> Path:
    """Return the directory used for completion cache files.

    Uses MNG_COMPLETION_CACHE_DIR if set, otherwise the mng host directory
    (MNG_HOST_DIR or ~/.mng). The directory is created if it does not exist.
    """
    env_dir = os.environ.get("MNG_COMPLETION_CACHE_DIR")
    if env_dir:
        cache_dir = Path(env_dir)
    else:
        cache_dir = read_default_host_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class CompletionCacheData(NamedTuple):
    """Schema for the tab completion JSON cache file."""

    commands: list[str] = []
    aliases: dict[str, str] = {}
    subcommand_by_command: dict[str, list[str]] = {}
    options_by_command: dict[str, list[str]] = {}
    flag_options_by_command: dict[str, list[str]] = {}
    option_choices: dict[str, list[str]] = {}
    agent_name_arguments: list[str] = []
    git_branch_options: list[str] = []
    host_name_options: list[str] = []
    host_name_arguments: list[str] = []
    plugin_name_options: list[str] = []
    plugin_names: list[str] = []
    plugin_name_arguments: list[str] = []
    config_key_arguments: list[str] = []
    config_keys: list[str] = []
