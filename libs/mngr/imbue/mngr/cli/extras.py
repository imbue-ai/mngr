"""Install optional extras for mngr: plugins, shell completion, Claude Code plugin."""

import json
import os
import platform
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.completion_install import COMPLETION_SHIM_MARKER
from imbue.mngr.cli.completion_install import generate_completion_shim
from imbue.mngr.cli.completion_install import get_managed_completion_script_path
from imbue.mngr.cli.completion_install import strip_legacy_completion_block
from imbue.mngr.cli.completion_install import write_managed_completion_scripts
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.plugin_install_wizard import install_wizard_impl
from imbue.mngr.cli.urwid_picker import run_multi_select_picker
from imbue.mngr.cli.urwid_picker import run_single_select_picker
from imbue.mngr.cli.urwid_utils import has_interactive_terminal
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.config.pre_readers import find_profile_dir_lightweight
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
from imbue.mngr.utils.deps import CLAUDE
from imbue.mngr.utils.deps import SUBPROCESS_ERRORS
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.toml_config import load_config_file_tomlkit
from imbue.mngr.utils.toml_config import save_config_file
from imbue.mngr.utils.toml_config import set_nested_value
from imbue.mngr.uv_tool import read_receipt
from imbue.mngr.uv_tool import require_uv_tool_receipt


def _detect_shell() -> str:
    """Detect the user's shell type (zsh or bash)."""
    shell_env = os.environ.get("SHELL", "")
    if "zsh" in shell_env:
        return "zsh"
    if "bash" in shell_env:
        return "bash"
    # Fallback based on OS
    if platform.system() == "Darwin":
        return "zsh"
    return "bash"


def _get_shell_rc(shell_type: str) -> Path:
    """Get the shell RC file path."""
    home = Path.home()
    if shell_type == "zsh":
        return home / ".zshrc"
    return home / ".bashrc"


def _is_completion_configured(rc_path: Path) -> bool:
    """Check if the (current, managed) mngr shell completion shim is installed.

    Looks for the managed-shim marker rather than just ``_mngr_complete``: an rc
    that still holds the old self-contained completion function (no marker) is
    treated as *not* configured, so installing adds the up-to-date shim (which,
    sourced last, supersedes the old function).
    """
    if not rc_path.exists():
        return False
    return COMPLETION_SHIM_MARKER in rc_path.read_text()


def _generate_completion_script(shell_type: str) -> str:
    """Generate the rc shim that sources the managed completion file."""
    return generate_completion_shim(shell_type)


# -- Shared picker helper --


def _confirm_install(question: str, install_label: str, skip_label: str = "Skip") -> bool:
    """Show a 2-option picker. Returns True if the user picks the install option.

    Caller must check ``has_interactive_terminal()`` before calling: this
    helper assumes a TTY is available.
    """
    idx = run_single_select_picker(
        options=[install_label, skip_label],
        title="mngr extras",
        header_text=question,
    )
    return idx == 0


# -- Completion extra --


def _completion_status() -> tuple[bool, str, Path]:
    """Return (is_configured, shell_type, rc_path)."""
    shell_type = _detect_shell()
    rc_path = _get_shell_rc(shell_type)
    configured = _is_completion_configured(rc_path)
    return configured, shell_type, rc_path


def _default_completion_confirm(rc_path: Path, will_replace: bool) -> bool:
    """Prompt to install/replace shell completion, surfacing whether an old block was detected."""
    if will_replace:
        question = (
            f"Found an existing mngr completion in {rc_path} from an older version. "
            "Replace it with the auto-updating managed shim?"
        )
        install_label = "Replace shell completion"
    else:
        question = f"Enable shell completion? This sets up an auto-updating mngr completion in {rc_path}."
        install_label = "Enable shell completion"
    return _confirm_install(question, install_label)


def _install_completion(
    auto: bool,
    *,
    # Dependencies are exposed as keyword arguments so tests can substitute
    # in-memory fakes without monkeypatching module-level callables.
    status_fn: Callable[[], tuple[bool, str, Path]] = _completion_status,
    is_interactive_fn: Callable[[], bool] = has_interactive_terminal,
    confirm_fn: Callable[[Path, bool], bool] = _default_completion_confirm,
) -> bool:
    """Install shell completion. Returns True if installed (or already configured).

    The rc gets a small shim that sources a managed completion file mngr keeps up
    to date; the managed files are (re)written here so completion-logic changes
    reach the user without further rc edits. An old self-contained completion
    block left by a previous version is detected and replaced (the confirm prompt
    says so).
    """
    configured, shell_type, rc_path = status_fn()

    # Always refresh the managed files so they reflect the current logic, even
    # when the shim is already installed.
    write_managed_completion_scripts()

    rc_text = rc_path.read_text() if rc_path.exists() else ""
    cleaned_text, removed_legacy = strip_legacy_completion_block(rc_text)

    if configured:
        # The managed shim is already installed; just tidy up an old self-contained
        # block if one is left over (and byte-matches a form we generated).
        if removed_legacy:
            atomic_write(rc_path, cleaned_text)
            write_human_line("Removed the old completion block from {} (managed shim already present)", rc_path)
        else:
            write_human_line("Shell completion already configured in {} (refreshed completion files)", rc_path)
        return True

    if removed_legacy:
        write_human_line(
            "Found an existing mngr completion in {} from an older version; it will be replaced.", rc_path
        )

    if not auto:
        if not is_interactive_fn():
            write_human_line("No interactive terminal available. Skipping shell completion.")
            return False
        if not confirm_fn(rc_path, removed_legacy):
            write_human_line("Skipping shell completion.")
            return False

    shim = _generate_completion_script(shell_type)
    if cleaned_text and not cleaned_text.endswith("\n"):
        cleaned_text += "\n"
    atomic_write(rc_path, f"{cleaned_text}\n{shim}\n")

    if removed_legacy:
        write_human_line("Replaced the old completion block with the managed shim in {}", rc_path)
    else:
        write_human_line("Shell completion enabled in {}", rc_path)
    # A child process can't load completion into the parent shell, so tell the user
    # how to activate it. The source command is on its own line for easy copying.
    write_human_line("To use it, start a new shell, or run:")
    write_human_line("source {}", get_managed_completion_script_path(shell_type))
    return True


# -- Claude Code plugin extra --


class ClaudeCodePlugin(FrozenModel):
    """An installable Claude Code plugin offered by `mngr extras claude-plugin`."""

    name: str = Field(description="Plugin name, e.g. 'imbue-code-guardian'")
    description: str = Field(description="One-line description shown next to the name in the picker")
    marketplace_repo: str = Field(
        description="GitHub repo hosting the plugin marketplace, e.g. 'imbue-ai/code-guardian'"
    )
    install_ref: str = Field(
        description=(
            "Plugin id passed to `claude plugin install` and matched against the `id` field of"
            " `claude plugin list --json`, e.g. 'imbue-code-guardian@imbue-code-guardian'"
        )
    )


# The Claude Code plugins mngr knows how to install. Each lives in its own
# GitHub repo, published as a Claude Code plugin marketplace.
_CLAUDE_CODE_PLUGINS: Final[tuple[ClaudeCodePlugin, ...]] = (
    ClaudeCodePlugin(
        name="imbue-code-guardian",
        description="Automated code review enforcement for Claude Code",
        marketplace_repo="imbue-ai/code-guardian",
        install_ref="imbue-code-guardian@imbue-code-guardian",
    ),
    ClaudeCodePlugin(
        name="imbue-mngr-skills",
        description="Skills that teach Claude how to use mngr, e.g. to coordinate with other agents",
        marketplace_repo="imbue-ai/mngr-claude-skills",
        install_ref="imbue-mngr-skills@imbue-mngr",
    ),
)


def _claude_native_plugin_status() -> tuple[bool, dict[str, bool]]:
    """Return (claude_available, {plugin_name: is_installed}).

    When Claude Code is not on PATH, the per-plugin map reports every plugin
    as not installed.
    """
    not_installed = {plugin.name: False for plugin in _CLAUDE_CODE_PLUGINS}
    claude_available = CLAUDE.is_available()
    if not claude_available:
        return False, not_installed

    try:
        with ConcurrencyGroup(name="extras-claude-check") as cg:
            result = cg.run_process_to_completion(["claude", "plugin", "list", "--json"], is_checked_after=False)
    except SUBPROCESS_ERRORS:
        return True, not_installed

    if result.returncode != 0:
        return True, not_installed

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning(
            "Could not parse `claude plugin list --json` output ({}); unable to detect installed plugins.", e
        )
        return True, not_installed

    # `claude plugin list --json` returns objects whose `id` is "<name>@<marketplace>",
    # which is exactly our install_ref. Match on that rather than substring-scanning
    # human output.
    installed_ids = {entry["id"] for entry in entries if isinstance(entry, dict) and "id" in entry}
    installed = {plugin.name: plugin.install_ref in installed_ids for plugin in _CLAUDE_CODE_PLUGINS}
    return True, installed


def _install_one_claude_plugin(plugin: ClaudeCodePlugin) -> bool:
    """Add the marketplace for and install a single Claude Code plugin.

    Returns True on success, False (with a warning) on failure.
    """
    write_human_line("Installing {}...", plugin.name)
    commands = (
        ["claude", "plugin", "marketplace", "add", plugin.marketplace_repo],
        ["claude", "plugin", "install", plugin.install_ref],
    )
    try:
        with ConcurrencyGroup(name="extras-claude-install") as cg:
            for command in commands:
                result = cg.run_process_to_completion(command, is_checked_after=False)
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip()
                    logger.warning("Failed to install {}. {}", plugin.name, detail)
                    return False
    except SUBPROCESS_ERRORS as e:
        logger.warning("Failed to install {}. {}", plugin.name, str(e))
        return False

    write_human_line("Installed {}.", plugin.name)
    return True


def _prompt_claude_plugins_choice(candidates: tuple[ClaudeCodePlugin, ...]) -> tuple[ClaudeCodePlugin, ...]:
    """Ask the user which of the not-yet-installed plugins to install.

    Presents a checkbox per candidate (all preselected), toggled with
    Space and confirmed with Enter. Each row shows the plugin name padded
    to a common width followed by its description, matching the
    `mngr extras plugins` wizard. Returns the checked plugins (empty when
    the user unchecks everything or cancels). Caller must check
    ``has_interactive_terminal()`` first.
    """
    name_width = max(len(plugin.name) for plugin in candidates)
    selected_indices = run_multi_select_picker(
        options=[f"{plugin.name.ljust(name_width)}  {plugin.description}" for plugin in candidates],
        title="mngr extras",
        header_text="Select Claude Code plugins to install:",
        preselected=[True] * len(candidates),
    )
    if selected_indices is None:
        return ()
    return tuple(candidates[index] for index in selected_indices)


def _install_claude_plugin(
    auto: bool,
    *,
    # Dependencies are exposed as keyword arguments so tests can substitute
    # in-memory fakes without monkeypatching module-level callables.
    status_fn: Callable[[], tuple[bool, dict[str, bool]]] = _claude_native_plugin_status,
    is_interactive_fn: Callable[[], bool] = has_interactive_terminal,
    select_fn: Callable[[tuple[ClaudeCodePlugin, ...]], tuple[ClaudeCodePlugin, ...]] = _prompt_claude_plugins_choice,
    install_fn: Callable[[ClaudeCodePlugin], bool] = _install_one_claude_plugin,
) -> bool:
    """Install Claude Code plugins (code review and/or agent skills).

    Returns True when every selected plugin was installed successfully (or
    when all known plugins were already installed). Returns False when Claude
    Code is unavailable, the user skipped, or any selected install failed.
    """
    claude_available, installed_by_name = status_fn()

    if not claude_available:
        write_human_line("Claude Code is not installed -- skipping Claude Code plugins.")
        return False

    candidates = tuple(plugin for plugin in _CLAUDE_CODE_PLUGINS if not installed_by_name.get(plugin.name, False))

    if not candidates:
        write_human_line("All Claude Code plugins are already installed.")
        return True

    if auto:
        selected = candidates
    elif not is_interactive_fn():
        write_human_line("No interactive terminal available. Skipping Claude Code plugins.")
        return False
    else:
        selected = select_fn(candidates)

    if not selected:
        write_human_line("Skipping Claude Code plugins.")
        return False

    return all([install_fn(plugin) for plugin in selected])


# -- Plugins extra (delegates to existing wizard) --


def _plugins_status() -> str:
    """Return a brief status string for the plugins extra."""
    try:
        receipt_path = require_uv_tool_receipt()
        receipt = read_receipt(receipt_path)
        installed_names = frozenset(r.name for r in receipt.extras)
        available = [p for p in PLUGIN_CATALOG if p.package_name not in installed_names]
        if not available:
            return "all plugins installed"
        # Count unique packages
        available_packages = {p.package_name for p in available}
        return f"{len(available_packages)} plugin package(s) available"
    except (OSError, ValueError, KeyError, AbortError):
        return "status unknown"


def _run_plugin_wizard() -> None:
    """Run the plugin install wizard (delegates to existing implementation)."""
    install_wizard_impl()


# -- Default agent type extra --


def _read_user_config_raw() -> dict[str, Any]:
    """Read the user-scope settings.toml as a raw dict (empty if not present).

    Lightweight: avoids ``setup_command_context``/``MngrConfig`` so this
    can be called from the extras walkthrough without paying for full
    config validation. Same pattern as ``install_wizard_impl``.
    """
    profile_dir = find_profile_dir_lightweight(read_default_host_dir())
    if profile_dir is None:
        return {}
    config_path = get_user_config_path(profile_dir)
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def _read_current_default_agent_type(raw: dict[str, Any]) -> str | None:
    """Return the user-config default for [commands.create] type, or None."""
    commands = raw.get("commands")
    if not isinstance(commands, dict):
        return None
    create = commands.get("create")
    if not isinstance(create, dict):
        return None
    value = create.get("type")
    return str(value) if value is not None else None


def _list_extras_agent_type_choices(raw: dict[str, Any], registered: list[str]) -> list[str]:
    """Return the set of agent type names the user can pick.

    Mirrors ``list_available_agent_types(config)`` but reads the user
    config raw to avoid loading a full ``MngrConfig``. ``registered`` is
    the plugin-registered list (typically ``list_registered_agent_types()``);
    user-config-defined types come from ``[agent_types.X]`` in ``raw``.
    """
    custom: list[str] = []
    raw_agent_types = raw.get("agent_types")
    if isinstance(raw_agent_types, dict):
        custom = [str(k) for k in raw_agent_types.keys()]
    return sorted(set(registered + custom))


def _default_agent_type_status() -> tuple[str | None, list[str]]:
    """Return (current_default_or_None, available_agent_types)."""
    raw = _read_user_config_raw()
    return _read_current_default_agent_type(raw), _list_extras_agent_type_choices(raw, list_registered_agent_types())


def _write_default_agent_type(value: str) -> Path:
    """Write [commands.create] type to the user-scope settings.toml.

    Returns the path written. Creates the profile directory if needed
    so this works on a fresh installation.
    """
    profile_dir = get_or_create_profile_dir(read_default_host_dir())
    config_path = get_user_config_path(profile_dir)
    doc = load_config_file_tomlkit(config_path)
    set_nested_value(doc, "commands.create.type", value)
    save_config_file(config_path, doc)
    return config_path


def _prompt_default_agent_type_choice(available: list[str]) -> str | None:
    """Show an urwid picker and return the chosen agent type, or None to skip.

    Returns None if the user picks "Keep no default" (the trailing
    sentinel row) or cancels via q/Ctrl+C. Caller must check
    ``has_interactive_terminal()`` first.
    """
    options = [*available, "Keep no default"]
    idx = run_single_select_picker(
        options=options,
        title="mngr extras",
        header_text="Pick a default agent type for 'mngr create':",
    )
    if idx is None:
        write_human_line("Cancelled; skipping default agent type.")
        return None
    if idx == len(available):
        write_human_line("Skipping default agent type.")
        return None
    return available[idx]


def _install_default_agent_type(
    auto: bool,
    *,
    # Dependencies are exposed as keyword arguments so tests can substitute
    # in-memory fakes without monkeypatching module-level callables.
    status_fn: Callable[[], tuple[str | None, list[str]]] = _default_agent_type_status,
    is_interactive_fn: Callable[[], bool] = has_interactive_terminal,
    prompt_fn: Callable[[list[str]], str | None] = _prompt_default_agent_type_choice,
    write_fn: Callable[[str], Path] = _write_default_agent_type,
) -> bool:
    """Set the default agent type for `mngr create`.

    Returns True if a default is set after this call (already-set or
    newly-set), False otherwise. With ``auto=True`` or no interactive
    terminal, never writes anything: if a default is already set, only
    prints the "already set" status; otherwise prints the suggested
    ``mngr config set`` command and the available agent types.
    """
    current, available = status_fn()

    if current is not None:
        write_human_line("Default agent type for 'mngr create' is already set to '{}'.", current)
        return True

    if not available:
        write_human_line("No agent types are registered yet -- skipping default agent type.")
        write_human_line("Install an agent-type plugin first (e.g. 'mngr extras plugins').")
        return False

    if auto or not is_interactive_fn():
        write_human_line("To set a default agent type for 'mngr create', run:")
        write_human_line("    mngr config set commands.create.type <name> --scope user")
        write_human_line("Available agent types:")
        for name in available:
            write_human_line("    {}", name)
        return False

    chosen = prompt_fn(available)
    if chosen is None:
        return False

    config_path = write_fn(chosen)
    write_human_line("Set commands.create.type = '{}' in {}", chosen, config_path)
    return True


# -- Status display --


def _print_extras_status(
    *,
    claude_native_plugin_status_fn: Callable[[], tuple[bool, dict[str, bool]]] = _claude_native_plugin_status,
) -> None:
    """Print the status of all extras.

    ``claude_native_plugin_status_fn`` reports whether claude is available and which
    known Claude Code plugins are installed. It is injectable (mirroring the
    ``status_fn`` seam on the ``_install_*`` helpers) so tests can avoid shelling
    out to ``claude plugin list`` -- a Node process whose startup is the slow,
    variable part of this call.
    """
    write_human_line("Extras")
    write_human_line("")

    # Plugins
    plugins_status = _plugins_status()
    write_human_line("  plugins          {}", plugins_status)

    # Completion
    configured, shell_type, rc_path = _completion_status()
    if configured:
        write_human_line("  completion       configured ({} in {})", shell_type, rc_path)
    else:
        write_human_line("  completion       not configured")

    # Claude Code plugins
    claude_available, installed_by_name = claude_native_plugin_status_fn()
    if not claude_available:
        write_human_line("  claude-plugin    claude not installed")
    else:
        statuses = ", ".join(
            f"{plugin.name}: {'installed' if installed_by_name.get(plugin.name, False) else 'not installed'}"
            for plugin in _CLAUDE_CODE_PLUGINS
        )
        write_human_line("  claude-plugin    {}", statuses)

    # Default agent type (the only setting `extras config` walks through today)
    current_default, _ = _default_agent_type_status()
    if current_default is not None:
        write_human_line("  default-type     {}", current_default)
    else:
        write_human_line("  default-type     not set")

    write_human_line("")


# -- CLI commands --


@click.group(name="extras", invoke_without_command=True, hidden=True)
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    help="Walk through all extras interactively",
)
@add_common_options
@click.pass_context
def extras(ctx: click.Context, **kwargs: Any) -> None:
    if ctx.invoked_subcommand is not None:
        return

    interactive = kwargs["interactive"]

    if not interactive:
        _print_extras_status()
        return

    # Interactive mode: walk through all extras
    try:
        write_human_line("--- Plugins ---")
        write_human_line("")
        _run_plugin_wizard()
    except AbortError as e:
        # The wizard already surfaced the abort to the user via its own output;
        # log at debug so a record exists for diagnostics without redundant noise.
        logger.debug("Plugin wizard aborted: {}", e.message)

    write_human_line("")
    write_human_line("--- Shell Completion ---")
    write_human_line("")
    _install_completion(auto=False)

    write_human_line("")
    write_human_line("--- Claude Code Plugins ---")
    write_human_line("")
    _install_claude_plugin(auto=False)

    write_human_line("")
    write_human_line("--- Default Agent Type ---")
    write_human_line("")
    # Newly-installed plugin agent types only become visible on the next
    # mngr invocation, so users who installed plugins above will only see
    # agent types that were already registered at startup; they can pick
    # one then or re-run `mngr extras config` later.
    _install_default_agent_type(auto=False)


@extras.command(name="plugins")
@add_common_options
@click.pass_context
def extras_plugins(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _run_plugin_wizard()
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


@extras.command(name="completion")
@click.option("-y", "--yes", is_flag=True, help="Auto-install without prompting")
@add_common_options
@click.pass_context
def extras_completion(ctx: click.Context, **kwargs: Any) -> None:
    _install_completion(auto=kwargs["yes"])


@extras.command(name="claude-plugin")
@click.option("-y", "--yes", is_flag=True, help="Auto-install without prompting")
@add_common_options
@click.pass_context
def extras_claude_plugin(ctx: click.Context, **kwargs: Any) -> None:
    _install_claude_plugin(auto=kwargs["yes"])


@extras.command(name="config")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip prompts; just print suggested commands for unset settings",
)
@add_common_options
@click.pass_context
def extras_config(ctx: click.Context, **kwargs: Any) -> None:
    # Walks through user-scope config settings the installer would
    # otherwise leave blank. Currently just the default agent type for
    # `mngr create`; future config-related setup steps will be added
    # here as additional walk steps. Each step short-circuits if the
    # corresponding setting is already configured, so re-running this
    # subcommand only prompts for the gaps.
    _install_default_agent_type(auto=kwargs["yes"])


# Help metadata

CommandHelpMetadata(
    key="extras",
    one_line_description="Install optional extras (plugins, completion, Claude Code plugins, user config)",
    synopsis="mngr extras [OPTIONS] [COMMAND]",
    description="""Manage optional extras that enhance mngr. With no subcommand, shows
the status of all extras. Use -i to walk through each extra interactively.

Extras:
  plugins        Run the plugin install wizard
  completion     Set up shell tab completion
  claude-plugin  Install Claude Code plugins (code review and/or agent skills)
  config         Walk through user-scope config settings (e.g. default agent type)""",
    examples=(
        ("Show status of all extras", "mngr extras"),
        ("Interactively set up all extras", "mngr extras -i"),
        ("Set up shell completion", "mngr extras completion"),
        ("Auto-install shell completion", "mngr extras completion -y"),
        ("Install Claude Code plugins", "mngr extras claude-plugin"),
        ("Walk through user-scope config settings", "mngr extras config"),
    ),
    see_also=(
        ("dependencies", "Check and install system dependencies"),
        ("plugin", "Manage plugins directly"),
    ),
).register()

CommandHelpMetadata(
    key="extras.plugins",
    one_line_description="Run the plugin install wizard",
    synopsis="mngr extras plugins",
    description="Launches the interactive plugin install wizard to select and install recommended plugins.",
    see_also=(
        ("plugin add", "Install a plugin package directly"),
        ("plugin list", "List discovered plugins"),
    ),
).register()

CommandHelpMetadata(
    key="extras.completion",
    one_line_description="Set up shell tab completion",
    synopsis="mngr extras completion [-y]",
    description="""Configure tab completion for mngr in your shell. Detects your shell
type (zsh/bash) and appends the completion script to your shell RC file.

Use -y to skip the confirmation prompt.""",
    examples=(
        ("Set up completion interactively", "mngr extras completion"),
        ("Auto-set up completion", "mngr extras completion -y"),
    ),
).register()

CommandHelpMetadata(
    key="extras.claude-plugin",
    one_line_description="Install Claude Code plugins (code review and/or agent skills)",
    synopsis="mngr extras claude-plugin [-y]",
    description="""Install mngr's Claude Code plugins (imbue-code-guardian and
imbue-mngr-skills). With an interactive terminal you pick which to install;
-y auto-installs any that are missing. Requires Claude Code.""",
    examples=(
        ("Choose which plugins to install", "mngr extras claude-plugin"),
        ("Auto-install all Claude Code plugins", "mngr extras claude-plugin -y"),
    ),
).register()

CommandHelpMetadata(
    key="extras.config",
    one_line_description="Walk through user-scope config settings",
    synopsis="mngr extras config [-y]",
    description="""Walk through user-scope config settings the installer would otherwise
leave blank. Each step short-circuits if the corresponding setting is
already configured, so re-running this subcommand only prompts for the
gaps.

Currently this just covers the default agent type for `mngr create`.
With an interactive terminal, presents an interactive picker of every
available agent type plus an option to keep no default; writes the
selection to `[commands.create] type` in your user-scope settings.toml.

With `-y` or without an interactive terminal, prints the suggested
`mngr config set commands.create.type <name> --scope user` command and
the list of available agent types -- writes nothing.""",
    examples=(
        ("Walk through user-scope config settings", "mngr extras config"),
        ("Print suggested config commands without prompting", "mngr extras config -y"),
    ),
    see_also=(
        ("config set", "Write a config value directly"),
        ("plugin list", "List discovered plugins, including agent types"),
    ),
).register()

add_pager_help_option(extras)
add_pager_help_option(extras_plugins)
add_pager_help_option(extras_completion)
add_pager_help_option(extras_claude_plugin)
add_pager_help_option(extras_config)
