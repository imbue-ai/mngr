import os
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.consts import PROFILES_DIRNAME
from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.utils.git_utils import find_git_worktree_root

# Filenames of the per-scope settings files, relative to their containing
# directory (the user profile dir, or the resolved project config dir). Private
# so callers go through the path helpers below (``get_user_config_path`` /
# ``get_project_config_path`` / ``get_local_config_path``) instead of
# reconstructing paths from a shared filename constant.
_SETTINGS_FILENAME: Final[str] = "settings.toml"
_LOCAL_SETTINGS_FILENAME: Final[str] = "settings.local.toml"

# =============================================================================
# Config File Discovery and Loading
# =============================================================================


def try_load_toml(path: Path | None) -> dict[str, Any] | None:
    """Load and parse a TOML file, returning None if path is None or missing.

    Raises ConfigParseError if the file exists but contains invalid TOML.
    """
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as e:
        raise ConfigParseError(f"Failed to parse config file {path}: {e}") from e


def _config_opts_into_pytest(raw: dict[str, Any]) -> bool:
    """Whether a raw config dict explicitly opts into being loaded under pytest."""
    return raw.get("is_allowed_in_pytest") is True


def enforce_pytest_config_opt_in(loaded_configs: list[tuple[str, dict[str, Any]]]) -> None:
    """Refuse to read a non-test config during a pytest run.

    During a pytest run (``PYTEST_CURRENT_TEST`` set), every config file that was
    actually loaded must set ``is_allowed_in_pytest = true``. This keeps a real
    config -- the developer's ~/.mngr or the repo's .mngr/settings.toml -- from
    being picked up by a poorly-scoped test and used to drive real operations,
    even when it is loaded alongside a test config that does opt in: every layer
    is checked on its own, not just the merged value. If no config file was
    loaded there is nothing to protect against, so mngr runs normally.

    ``loaded_configs`` is the list of (source, raw) for the config files that
    were actually present (callers filter out missing files); ``source`` is a
    path or human-readable label used only in the error message.

    DO NOT strip ``PYTEST_CURRENT_TEST`` from a test's (or a subprocess's)
    environment to get past this guard. That marker is load-bearing for several
    safety features -- the Modal backend's ``TEST_ENV_PATTERN`` guard and this
    config guard among them -- and removing it has leaked un-sweepable real
    resources in the past. The only correct way to permit a config during a
    pytest run is to set ``is_allowed_in_pytest = true`` on that config.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return
    for source, raw in loaded_configs:
        if not _config_opts_into_pytest(raw):
            raise ConfigParseError(
                f"Running mngr within pytest is not allowed: the config file ({source}) does not "
                "set is_allowed_in_pytest = true. Every config file loaded during a pytest run "
                "must opt in. If this is a test config, set that field; otherwise a test is "
                "loading a config that was not written for testing."
            )


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
    return profile_dir / _SETTINGS_FILENAME


def get_project_config_path(project_config_dir: Path) -> Path:
    """Get the project settings file inside a resolved project config directory."""
    return project_config_dir / _SETTINGS_FILENAME


def get_local_config_path(project_config_dir: Path) -> Path:
    """Get the local settings file inside a resolved project config directory."""
    return project_config_dir / _LOCAL_SETTINGS_FILENAME


def get_project_config_name(root_name: str) -> Path:
    """Get the project config relative path based on root name."""
    return Path(f".{root_name}") / _SETTINGS_FILENAME


def get_local_config_name(root_name: str) -> Path:
    """Get the local config relative path based on root name."""
    return Path(f".{root_name}") / _LOCAL_SETTINGS_FILENAME


def _find_project_root(cg: ConcurrencyGroup, start: Path | None = None) -> Path | None:
    """Find the project root by looking for git worktree root."""
    return find_git_worktree_root(start, cg)


def resolve_project_config_dir(
    root_name: str,
    cg: ConcurrencyGroup,
) -> Path | None:
    """Resolve the project config directory.

    If MNGR_PROJECT_CONFIG_DIR is set, returns that path directly.
    Otherwise, returns <git_root>/.<root_name>/ (the default behavior).
    Returns None if no project root can be determined and MNGR_PROJECT_CONFIG_DIR is not set.
    """
    env_project_dir = os.environ.get("MNGR_PROJECT_CONFIG_DIR")
    if env_project_dir:
        return Path(env_project_dir)
    root = _find_project_root(cg=cg)
    if root is None:
        return None
    return root / f".{root_name}"


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
# OutputOptions and MngrContext, which aren't available until after config
# loading). Trace-level logs will only be visible with loguru's default
# stderr sink if someone explicitly lowers the level.
#
# _resolve_config_files returns the raw config dicts in precedence order
# (user, project, local). Each pre-reader iterates these and merges the
# results, so later layers naturally override earlier ones.


def read_config_layers(
    profile_dir: Path | None,
    project_config_dir: Path | None,
) -> list[tuple[ConfigScope, Path, dict[str, Any]]]:
    """Read the user/project/local config layers and enforce the pytest guard.

    This is the single chokepoint for reading config files: it loads each layer
    that exists and runs ``enforce_pytest_config_opt_in`` over them, so no code
    path can read config during a pytest run without the guard being applied.
    Returns ``(scope, path, raw)`` per present layer, in precedence order
    (USER < PROJECT < LOCAL); ``scope`` is the :class:`ConfigScope` the file
    belongs to, which lets ``load_config`` attribute narrowing diagnostics to a
    specific file (and matches what ``mngr config set --scope`` accepts).

    ``profile_dir`` and ``project_config_dir`` are resolved by the caller (so
    this does no directory resolution and needs no ConcurrencyGroup): profile_dir
    via the lightweight read-only lookup for the pre-readers, or create-on-demand
    for ``load_config``; project_config_dir via ``resolve_project_config_dir``.
    """
    candidate_paths: list[tuple[ConfigScope, Path]] = []
    if profile_dir is not None:
        candidate_paths.append((ConfigScope.USER, get_user_config_path(profile_dir)))
    if project_config_dir is not None:
        candidate_paths.append((ConfigScope.PROJECT, get_project_config_path(project_config_dir)))
        candidate_paths.append((ConfigScope.LOCAL, get_local_config_path(project_config_dir)))
    loaded: list[tuple[ConfigScope, Path, dict[str, Any]]] = []
    for scope, path in candidate_paths:
        raw = try_load_toml(path)
        if raw is not None:
            loaded.append((scope, path, raw))
    enforce_pytest_config_opt_in([(str(path), raw) for _scope, path, raw in loaded])
    return loaded


def _resolve_config_files() -> list[dict[str, Any]]:
    """Return parsed config dicts in precedence order (lowest to highest).

    Used by the lightweight pre-readers; the project root is resolved from
    MNGR_PROJECT_CONFIG_DIR or the cwd's git worktree root.
    """
    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    base_dir = read_default_host_dir()
    profile_dir = find_profile_dir_lightweight(base_dir)

    # Resolve the project dir inside the ConcurrencyGroup (it needs git lookups),
    # but read the TOML files outside it (in read_config_layers) so a
    # ConfigParseError propagates directly instead of being wrapped in a
    # ConcurrencyExceptionGroup.
    cg = ConcurrencyGroup(name="config-pre-reader")
    with cg:
        project_config_dir = resolve_project_config_dir(root_name, cg)

    return [raw for _scope, _path, raw in read_config_layers(profile_dir, project_config_dir)]


# --- Default subcommand pre-reader ---


def read_default_command(command_name: str) -> str | None:
    """Return the configured default subcommand for command_name.

    Returns None if no config files set default_subcommand for the given
    command group (the caller should use its compile-time default).
    An empty string means "explicitly disabled" (the caller should show
    help instead of defaulting).

    The project root is resolved from MNGR_PROJECT_CONFIG_DIR or the cwd's git
    worktree root.
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
    return merged.get(command_name)


# --- Disabled plugins pre-reader ---

# Plugins that are DISABLED by default and must be explicitly opted into with
# ``[plugins.<name>] enabled = true`` in a config layer to load. This inverts
# the normal default (plugins load unless explicitly disabled): an opt-in
# plugin is treated as disabled whenever config does not set it to enabled.
#
# The set is hardcoded here in mngr core -- not declared by the plugin itself --
# because plugin blocking happens *before* setuptools entry points are loaded
# (see ``create_plugin_manager``), so the plugin module is not importable at the
# point we must decide whether to block it. Add a plugin's registry name here to
# make it opt-in; nothing else changes, since opting in reuses the same
# ``enabled`` config key as the normal enable/disable mechanism.
#
# ``claude_subagent_proxy`` is opt-in because it is very experimental and breaks
# a lot of other tooling (it intercepts Claude Code's built-in Task tool); see
# that plugin's README for details.
OPT_IN_PLUGINS: Final[frozenset[str]] = frozenset({"claude_subagent_proxy"})


def read_disabled_plugins() -> frozenset[str]:
    """Return the set of plugin names disabled across all config layers.

    Reads user, project, and local config files for [plugins.<name>]
    sections with enabled = false.  Later layers override earlier ones.

    Plugins in :data:`OPT_IN_PLUGINS` are disabled-by-default: they are
    included in the result unless a config layer explicitly sets their
    ``enabled = true``.

    The project root is resolved from MNGR_PROJECT_CONFIG_DIR or the cwd's git
    worktree root.
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
    disabled = {name for name, is_enabled in merged.items() if not is_enabled}
    # Opt-in plugins are disabled unless a config layer explicitly enabled them.
    disabled |= {name for name in OPT_IN_PLUGINS if merged.get(name) is not True}
    return frozenset(disabled)
