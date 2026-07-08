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


def get_completion_cache_dir() -> Path:
    """Return the directory used for completion cache files.

    Resolution order:
    1. ``MNGR_COMPLETION_CACHE_DIR`` env var. This is a "special" env var
       (single underscore) like ``MNGR_ROOT_NAME`` / ``MNGR_PREFIX`` /
       ``MNGR_HOST_DIR``: it isn't a parsed ``MngrConfig`` field, because tab
       completion runs in a lightweight pre-reader path that intentionally
       skips full config loading. The double-underscore ``MNGR__*`` form is
       not recognised here.
    2. The mngr host directory (``MNGR_HOST_DIR`` or ``~/.mngr``).

    The directory is created if it does not exist.
    """
    env_dir = os.environ.get("MNGR_COMPLETION_CACHE_DIR")
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
    # Every option name (both ``--long`` and ``-short`` forms). The ``--long``
    # entries are the candidates for ``--`` completion; the whole set lets the
    # positional-argument counter recognise an option so it consumes its value.
    options_by_command: dict[str, list[str]] = {}
    # The subset of options that take no value (flags and ``count`` options like
    # ``-v``/``--verbose``), both forms, so the counter consumes only the option
    # word itself rather than also consuming the following word.
    flag_options_by_command: dict[str, list[str]] = {}
    option_choices: dict[str, list[str]] = {}
    git_branch_options: list[str] = []
    host_name_options: list[str] = []
    plugin_name_options: list[str] = []
    plugin_names: list[str] = []
    # Installable plugin package names (PyPI) for completing `mngr plugin add`,
    # distinct from `plugin_names` (entry-point names of installed plugins).
    catalog_package_names: list[str] = []
    # Currently-installed plugin package names (uv-tool receipt extras) for
    # completing `mngr plugin remove`, which only accepts already-installed
    # packages.
    installed_plugin_package_names: list[str] = []
    config_keys: list[str] = []
    positional_nargs_by_command: dict[str, int | None] = {}
    positional_completions: dict[str, list[list[str]]] = {}
    config_value_choices: dict[str, list[str]] = {}
    # Option names (e.g. "-S", "--setting") whose value is a ``KEY=VALUE`` config
    # override. The completer completes their KEY against config_keys and their
    # VALUE against config_value_choices (the same data behind `mngr config set`).
    setting_option_names: list[str] = []
    # Candidates for the `mngr help` positional arg: every top-level command name
    # plus every registered help topic key.
    help_targets: list[str] = []
