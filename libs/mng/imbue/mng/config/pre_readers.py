import os
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.consts import PROFILES_DIRNAME
from imbue.mng.config.consts import ROOT_CONFIG_FILENAME
from imbue.mng.config.host_dir import read_default_host_dir
from imbue.mng.utils.git_utils import find_git_worktree_root

# =============================================================================
# Config File Discovery and Loading
# =============================================================================


def try_load_toml(path: Path | None) -> dict[str, Any] | None:
    """Load and parse a TOML file, returning None if path is None, missing, or malformed."""
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as e:
        logger.trace("Skipped malformed config file: {} ({})", path, e)
        return None


def find_profile_dir_lightweight(base_dir: Path) -> Path | None:
    """Read-only profile directory lookup (never creates dirs/files).

    Returns the profile directory if it can be determined from existing files,
    or None otherwise.
    """
    root_config = try_load_toml(base_dir / ROOT_CONFIG_FILENAME)
    if root_config is None:
        return None
    profile_id = root_config.get("profile")
    if not profile_id:
        return None
    profile_dir = base_dir / PROFILES_DIRNAME / profile_id
    if profile_dir.exists() and profile_dir.is_dir():
        return profile_dir
    return None


def get_user_config_path(profile_dir: Path) -> Path:
    """Get the user config path based on profile directory."""
    return profile_dir / "settings.toml"


def get_project_config_name(root_name: str) -> Path:
    """Get the project config relative path based on root name."""
    return Path(f".{root_name}") / "settings.toml"


def get_local_config_name(root_name: str) -> Path:
    """Get the local config relative path based on root name."""
    return Path(f".{root_name}") / "settings.local.toml"


def _find_project_root(cg: ConcurrencyGroup, start: Path | None = None) -> Path | None:
    """Find the project root by looking for git worktree root."""
    return find_git_worktree_root(start, cg)


def load_project_config(context_dir: Path | None, root_name: str, cg: ConcurrencyGroup) -> dict[str, Any] | None:
    """Find and load the project config file, returning None if not found or malformed."""
    root = context_dir or _find_project_root(cg=cg)
    if root is None:
        return None
    return try_load_toml(root / get_project_config_name(root_name))


def load_local_config(context_dir: Path | None, root_name: str, cg: ConcurrencyGroup) -> dict[str, Any] | None:
    """Find and load the local config file, returning None if not found or malformed."""
    root = context_dir or _find_project_root(cg=cg)
    if root is None:
        return None
    return try_load_toml(root / get_local_config_name(root_name))


# =============================================================================
# Lightweight config pre-readers
# =============================================================================
#
# These functions read specific values from config files before the full
# config is loaded.  They run early in startup (CLI parse time or plugin
# manager creation) so they intentionally avoid plugin hooks, full config
# validation, and anything that needs a PluginManager.
#
# Note: logging is not yet configured when these run (setup_logging needs
# OutputOptions and MngContext, which aren't available until after config
# loading). Trace-level logs will only be visible with loguru's default
# stderr sink if someone explicitly lowers the level.
#
# _resolve_config_files returns the raw config dicts in precedence order
# (user, project, local). Each pre-reader iterates these and merges the
# results, so later layers naturally override earlier ones.


def _resolve_config_files(
    context_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return parsed config dicts in precedence order (lowest to highest)."""
    root_name = os.environ.get("MNG_ROOT_NAME", "mng")
    base_dir = read_default_host_dir()

    configs: list[dict[str, Any]] = []

    # User config
    profile_dir = find_profile_dir_lightweight(base_dir)
    if profile_dir is not None:
        raw = try_load_toml(get_user_config_path(profile_dir))
        if raw is not None:
            configs.append(raw)

    # Project + local config need the project root
    cg = ConcurrencyGroup(name="config-pre-reader")
    with cg:
        raw_project = load_project_config(context_dir, root_name, cg)
        raw_local = load_local_config(context_dir, root_name, cg)

    if raw_project is not None:
        configs.append(raw_project)
    if raw_local is not None:
        configs.append(raw_local)

    return configs


# --- Default subcommand pre-reader ---


def read_default_command(command_name: str) -> str:
    """Return the configured default subcommand for command_name.

    If no config files set default_subcommand for the given command
    group, falls back to "create".  An empty string means "disabled"
    (the caller should show help instead of defaulting).
    """
    merged: dict[str, str] = {}
    for raw in _resolve_config_files():
        raw_commands = raw.get("commands")
        if not isinstance(raw_commands, dict):
            continue
        for cmd_name, cmd_section in raw_commands.items():
            if not isinstance(cmd_section, dict):
                continue
            value = cmd_section.get("default_subcommand")
            if value is not None:
                merged[cmd_name] = str(value)
    return merged.get(command_name, "create")


# --- Disabled plugins pre-reader ---


def read_disabled_plugins() -> frozenset[str]:
    """Return the set of plugin names disabled across all config layers.

    Reads user, project, and local config files for [plugins.<name>]
    sections with enabled = false.  Later layers override earlier ones.
    """
    merged: dict[str, bool] = {}
    for raw in _resolve_config_files():
        raw_plugins = raw.get("plugins")
        if not isinstance(raw_plugins, dict):
            continue
        for plugin_name, plugin_section in raw_plugins.items():
            if not isinstance(plugin_section, dict):
                continue
            enabled_value = plugin_section.get("enabled")
            if enabled_value is not None:
                merged[plugin_name] = bool(enabled_value)
    return frozenset(name for name, is_enabled in merged.items() if not is_enabled)
