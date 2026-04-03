"""Install optional extras for mngr: plugins, shell completion, Claude Code plugin."""

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.complete import generate_bash_script
from imbue.mngr.cli.complete import generate_zsh_script
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.plugin_install_wizard import install_wizard_impl
from imbue.mngr.plugin_catalog import RECOMMENDED_PLUGINS
from imbue.mngr.uv_tool import read_receipt
from imbue.mngr.uv_tool import require_uv_tool_receipt


def _read_tty_choice(prompt: str) -> str:
    """Read a single line from /dev/tty (works even when stdin is piped)."""
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip()
    except OSError:
        return ""


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


def _generate_completion_script(shell_type: str) -> str | None:
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
        choice = _read_tty_choice("[y/n]: ")
        if choice.lower() not in ("y", ""):
            write_human_line("Skipping shell completion.")
            return False

    script = _generate_completion_script(shell_type)
    if script is None:
        write_human_line("WARNING: Could not generate completion script.")
        return False

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
        choice = _read_tty_choice("[y/n]: ")
        if choice.lower() not in ("y", ""):
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
        available = [p for p in RECOMMENDED_PLUGINS if p.package_name not in installed_names]
        if not available:
            return "all recommended plugins installed"
        return f"{len(available)} recommended plugin(s) available"
    except (OSError, ValueError, KeyError):
        return "status unknown"


def _run_plugin_wizard() -> None:
    """Run the plugin install wizard (delegates to existing implementation)."""
    install_wizard_impl()


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
        logger.warning("Plugin wizard: {}", e.message)

    write_human_line("")
    write_human_line("--- Shell Completion ---")
    write_human_line("")
    _install_completion(auto=False)

    write_human_line("")
    write_human_line("--- Claude Code Plugin ---")
    write_human_line("")
    _install_claude_plugin(auto=False)


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
  claude-plugin  Install the Claude Code review plugin""",
    examples=(
        ("Show status of all extras", "mngr extras"),
        ("Interactively set up all extras", "mngr extras -i"),
        ("Set up shell completion", "mngr extras completion"),
        ("Auto-install shell completion", "mngr extras completion -y"),
        ("Install Claude Code plugin", "mngr extras claude-plugin"),
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

add_pager_help_option(extras)
add_pager_help_option(extras_plugins)
add_pager_help_option(extras_completion)
add_pager_help_option(extras_claude_plugin)
