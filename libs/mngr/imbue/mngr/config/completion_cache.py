"""Shared types and constants for the tab completion cache.

This module is deliberately lightweight (stdlib + local stdlib-only deps) so
it can be imported by both the cache writer (completion_writer.py, heavy
imports) and the cache reader (cli/complete.py, no heavy imports).
"""

import os
from pathlib import Path
from typing import Final
from typing import NamedTuple

from imbue.mngr.config.host_dir import read_default_host_dir

COMPLETION_CACHE_FILENAME: Final[str] = ".command_completions.json"


def get_completion_cache_dir(config_value: Path | None = None) -> Path:
    """Return the directory used for completion cache files.

    Resolution order:
    1. ``config_value`` (i.e. ``MngrConfig.completion_cache_dir``) when supplied
       and non-None. Honoring the parsed config field is what makes
       ``completion_cache_dir = "/foo"`` in ``settings.toml`` take effect.
    2. ``MNGR__COMPLETION_CACHE_DIR`` env var (lightweight pre-reader path used
       by tab-completion callers that run before the full config is loaded).
    3. The mngr host directory (``MNGR_HOST_DIR`` or ``~/.mngr``).

    The directory is created if it does not exist.
    """
    if config_value is not None:
        cache_dir = Path(config_value)
    else:
        env_dir = os.environ.get("MNGR__COMPLETION_CACHE_DIR")
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
    git_branch_options: list[str] = []
    host_name_options: list[str] = []
    plugin_name_options: list[str] = []
    plugin_names: list[str] = []
    config_keys: list[str] = []
    positional_nargs_by_command: dict[str, int | None] = {}
    positional_completions: dict[str, list[list[str]]] = {}
    config_value_choices: dict[str, list[str]] = {}
