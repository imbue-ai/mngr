"""Install optional extras for mngr: plugins, shell completion, Claude Code plugin."""

import os
import platform
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.complete import generate_bash_script
from imbue.mngr.cli.complete import generate_zsh_script
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import read_tty_choice
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.plugin_install_wizard import install_wizard_impl
from imbue.mngr.cli.urwid_utils import has_interactive_terminal
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.loader import get_or_create_profile_dir
from imbue.mngr.config.pre_readers import find_profile_dir_lightweight
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.plugin_catalog import PLUGIN_CATALOG
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
    """Check if mngr shell completion is already configured."""
    if not rc_path.exists():
        return False
    return "_mngr_complete" in rc_path.read_text()


def _generate_completion_script(shell_type: str) -> str:
    """Generate the completion script using the existing complete module."""
    if shell_type == "zsh":
        return generate_zsh_script()
    return generate_bash_script()


# -- Completion extra --


def _completion_status() -> tuple[bool, str, Path]:
    """Return (is_configured, shell_type, rc_path)."""
    shell_type = _detect_shell()
    rc_path = _get_shell_rc(shell_type)
    configured = _is_completion_configured(rc_path)
    return configured, shell_type, rc_path


def _install_completion(auto: bool) -> bool:
    """Install shell completion. Returns True if installed (or already configured)."""
    configured, shell_type, rc_path = _completion_status()

    if configured:
        write_human_line("Shell completion already configured in {}", rc_path)
        return True

    if not auto:
        write_human_line("Enable shell completion? This will add a line to {}", rc_path)
        choice = read_tty_choice("[y/n]: ")
        if choice == "" or choice.lower() != "y":
            if choice == "":
                write_human_line("No interactive terminal available. Skipping shell completion.")
            else:
                write_human_line("Skipping shell completion.")
            return False

    script = _generate_completion_script(shell_type)

    with rc_path.open("a") as f:
        f.write(f"\n{script}\n")

    write_human_line("Shell completion enabled in {}", rc_path)
    return True


# -- Claude Code plugin extra --


def _claude_plugin_status() -> tuple[bool, bool]:
    """Return (claude_available, plugin_installed)."""
    claude_available = shutil.which("claude") is not None
    if not claude_available:
        return False, False

    # Check if the plugin is installed
    try:
        with ConcurrencyGroup(name="extras-claude-check") as cg:
            result = cg.run_process_to_completion(["claude", "plugin", "list"])
        plugin_installed = "imbue-code-guardian" in result.stdout
        return True, plugin_installed
    except (OSError, ProcessError):
        return True, False


def _install_claude_plugin(auto: bool) -> bool:
    """Install the Claude Code review plugin. Returns True if installed (or already present)."""
    claude_available, plugin_installed = _claude_plugin_status()

    if not claude_available:
        write_human_line("Claude Code is not installed -- skipping Claude Code plugin.")
        return False

    if plugin_installed:
        write_human_line("Claude Code review plugin is already installed.")
        return True

    if not auto:
        write_human_line("Install the Claude Code review plugin (imbue-code-guardian)?")
        choice = read_tty_choice("[y/n]: ")
        if choice == "" or choice.lower() != "y":
            if choice == "":
                write_human_line("No interactive terminal available. Skipping Claude Code plugin.")
            else:
                write_human_line("Skipping Claude Code plugin.")
            return False

    write_human_line("Installing Claude Code review plugin...")
    try:
        with ConcurrencyGroup(name="extras-claude-install") as cg:
            cg.run_process_to_completion(["claude", "plugin", "marketplace", "add", "imbue-ai/code-guardian"])
            cg.run_process_to_completion(["claude", "plugin", "install", "imbue-code-guardian@imbue-code-guardian"])
        write_human_line("Claude Code review plugin installed.")
        return True
    except (OSError, ProcessError) as e:
        detail = ""
        if isinstance(e, ProcessError):
            detail = e.stderr.strip() or e.stdout.strip()
        write_human_line("WARNING: Failed to install Claude Code plugin. {}", detail)
        return False


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
    """Show a numbered picker and return the chosen agent type, or None to skip.

    Returns None if the user picks "keep no default", enters something
    other than a valid number, or if no TTY is available. The "skip"
    option is always last.
    """
    write_human_line("Pick a default agent type for 'mngr create':")
    for index, name in enumerate(available, start=1):
        write_human_line("  {}) {}", index, name)
    skip_index = len(available) + 1
    write_human_line("  {}) keep no default", skip_index)

    choice = read_tty_choice(f"[1-{skip_index}]: ")
    if choice == "":
        write_human_line("No interactive terminal available. Skipping default agent type.")
        return None

    try:
        picked = int(choice)
    except ValueError:
        write_human_line("Not a number; skipping default agent type.")
        return None

    if picked == skip_index:
        write_human_line("Skipping default agent type.")
        return None
    if 1 <= picked <= len(available):
        return available[picked - 1]
    write_human_line("Out of range; skipping default agent type.")
    return None


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
    terminal, only prints status and the suggested ``mngr config set``
    command -- never writes anything.
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


def _print_extras_status() -> None:
    """Print the status of all extras."""
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

    # Claude Code plugin
    claude_available, plugin_installed = _claude_plugin_status()
    if not claude_available:
        write_human_line("  claude-plugin    claude not installed")
    elif plugin_installed:
        write_human_line("  claude-plugin    installed")
    else:
        write_human_line("  claude-plugin    not installed")

    # Default agent type
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
    write_human_line("--- Claude Code Plugin ---")
    write_human_line("")
    _install_claude_plugin(auto=False)

    write_human_line("")
    write_human_line("--- Default Agent Type ---")
    write_human_line("")
    # Newly-installed plugin agent types only become visible on the next
    # mngr invocation, so users who installed plugins above will only see
    # agent types that were already registered at startup; they can pick
    # one then or re-run `mngr extras default-type` later.
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


@extras.command(name="default-type")
@add_common_options
@click.pass_context
def extras_default_type(ctx: click.Context, **kwargs: Any) -> None:
    # Mirrors the other extras subcommands: with a TTY, prompt the user
    # to pick a default agent type; without one (the read_tty_choice
    # fallback), fall back to printing the suggested `mngr config set`
    # command and the available agent type list.
    _install_default_agent_type(auto=False)


# Help metadata

CommandHelpMetadata(
    key="extras",
    one_line_description="Install optional extras (plugins, completion, Claude Code plugin)",
    synopsis="mngr extras [OPTIONS] [COMMAND]",
    description="""Manage optional extras that enhance mngr. With no subcommand, shows
the status of all extras. Use -i to walk through each extra interactively.

Extras:
  plugins        Run the plugin install wizard
  completion     Set up shell tab completion
  claude-plugin  Install the Claude Code review plugin
  default-type   Pick a default agent type for `mngr create`""",
    examples=(
        ("Show status of all extras", "mngr extras"),
        ("Interactively set up all extras", "mngr extras -i"),
        ("Set up shell completion", "mngr extras completion"),
        ("Auto-install shell completion", "mngr extras completion -y"),
        ("Install Claude Code plugin", "mngr extras claude-plugin"),
        ("Pick a default agent type", "mngr extras default-type"),
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
    one_line_description="Install the Claude Code review plugin",
    synopsis="mngr extras claude-plugin [-y]",
    description="""Install the imbue-code-guardian plugin for Claude Code, which provides
automated code review enforcement.

Requires Claude Code to be installed. Use -y to skip the confirmation prompt.""",
    examples=(
        ("Install the plugin interactively", "mngr extras claude-plugin"),
        ("Auto-install the plugin", "mngr extras claude-plugin -y"),
    ),
).register()

CommandHelpMetadata(
    key="extras.default-type",
    one_line_description="Pick a default agent type for `mngr create`",
    synopsis="mngr extras default-type",
    description="""Set the default agent type used by `mngr create` when no type is
provided on the command line.

With an interactive terminal, presents a numbered picker of every
available agent type plus an option to keep no default. The selection
is written to `[commands.create] type` in your user-scope settings.toml.

Without an interactive terminal, prints the suggested
`mngr config set commands.create.type <name> --scope user` command and
the list of available agent types -- writes nothing.""",
    examples=(("Pick a default agent type", "mngr extras default-type"),),
    see_also=(
        ("config set", "Write a config value directly"),
        ("plugin list", "List discovered plugins, including agent types"),
    ),
).register()

add_pager_help_option(extras)
add_pager_help_option(extras_plugins)
add_pager_help_option(extras_completion)
add_pager_help_option(extras_claude_plugin)
add_pager_help_option(extras_default_type)
