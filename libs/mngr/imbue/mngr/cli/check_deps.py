"""Check and optionally install system dependencies for mngr."""

import sys
from typing import Any

import click
from click_option_group import MutuallyExclusiveOptionGroup
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.utils.deps import ALL_DEPS
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import SystemDependency
from imbue.mngr.utils.deps import check_bash_version
from imbue.mngr.utils.deps import detect_os
from imbue.mngr.utils.deps import install_deps_batch
from imbue.mngr.utils.deps import install_modern_bash


def _read_tty_choice(prompt: str) -> str:
    """Read a single line from /dev/tty (works even when stdin is piped)."""
    try:
        with open("/dev/tty") as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip()
    except OSError:
        return ""


def _print_status_table(
    deps: tuple[SystemDependency, ...],
    missing: list[SystemDependency],
    bash_ok: bool,
    os_name: str,
) -> None:
    """Print a table showing each dependency and its status."""
    missing_set = {id(d) for d in missing}
    name_width = max(len(d.binary) for d in deps)

    for dep in deps:
        status = "missing" if id(dep) in missing_set else "ok"
        category = "core" if dep.category == DependencyCategory.CORE else "optional"
        write_human_line(
            "  {:<{}}  {:>8}  {}  ({})",
            dep.binary,
            name_width,
            f"[{category}]",
            status,
            dep.purpose,
        )

    if os_name == OsName.MACOS and not bash_ok:
        write_human_line(
            "  {:<{}}  {:>8}  {}  ({})",
            "bash(4+)",
            name_width,
            "[core]",
            "missing",
            "modern bash required for mngr scripts",
        )


def _check_deps_impl(interactive: bool, core: bool, install_all: bool) -> None:
    """Implementation of check-deps command."""
    os_name = detect_os()

    # Check which deps are missing
    missing = [dep for dep in ALL_DEPS if not dep.is_available()]
    missing_core = [dep for dep in missing if dep.category == DependencyCategory.CORE]
    # Check bash version (only matters on macOS)
    bash_ok = True
    if os_name == OsName.MACOS:
        bash_ok = check_bash_version()

    # Print status
    write_human_line("System dependencies ({})", os_name)
    _print_status_table(ALL_DEPS, missing, bash_ok, os_name)
    write_human_line("")

    all_ok = len(missing) == 0 and bash_ok
    if all_ok:
        write_human_line("All system dependencies are present.")
        return

    # Report-only mode (no install flags)
    if not interactive and not core and not install_all:
        count = len(missing) + (0 if bash_ok else 1)
        write_human_line("{} missing dependency(ies). Use -i to install interactively.", count)
        sys.exit(1)

    # Determine what to install
    to_install: list[SystemDependency] = []

    if install_all:
        to_install = missing
    elif core:
        to_install = missing_core
    else:
        write_human_line("  [a] Install all ({})", ", ".join(d.binary for d in missing))
        if missing_core:
            write_human_line("  [c] Install core only ({})", ", ".join(d.binary for d in missing_core))
        write_human_line("  [n] Skip -- I'll install them myself")
        write_human_line("")

        choice = _read_tty_choice("Choice [a/c/n]: ")
        if choice.lower() in ("a", "y", ""):
            to_install = missing
        elif choice.lower() == "c":
            to_install = missing_core
        else:
            write_human_line("Skipping dependency installation.")
            return

    if not to_install:
        write_human_line("Nothing to install.")
        return

    need_bash = os_name == OsName.MACOS and not bash_ok

    # Do the installation
    write_human_line("Installing: {}", ", ".join(d.binary for d in to_install))
    failed = install_deps_batch(to_install, os_name)

    # Install modern bash on macOS if needed
    if need_bash:
        write_human_line("Installing modern bash via brew...")
        if not install_modern_bash():
            write_human_line("WARNING: Failed to install modern bash.")

    # Post-install status
    write_human_line("")
    still_missing = [dep for dep in ALL_DEPS if not dep.is_available()]
    bash_ok_now = check_bash_version() if os_name == OsName.MACOS else True

    if failed:
        write_human_line("Failed to install: {}", ", ".join(d.binary for d in failed))

    if still_missing:
        write_human_line("Still missing: {}", ", ".join(d.binary for d in still_missing))

    # Deferred warnings
    if os_name == OsName.MACOS and not bash_ok_now and need_bash:
        write_human_line(
            "WARNING: PATH-resolved bash is still old after install. "
            "Ensure /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel) is before /bin in your PATH."
        )

    # Exit code: 0 if all core deps present, 1 otherwise
    still_missing_core = [d for d in still_missing if d.category == DependencyCategory.CORE]
    if still_missing_core or (os_name == OsName.MACOS and not bash_ok_now):
        sys.exit(1)


@click.command(name="dependencies", hidden=True)
@optgroup.group("Install mode", cls=MutuallyExclusiveOptionGroup)
@optgroup.option(
    "-i",
    "--interactive",
    is_flag=True,
    help="Interactively prompt to install missing dependencies",
)
@optgroup.option(
    "-c",
    "--core",
    is_flag=True,
    help="Automatically install core dependencies without prompting",
)
@optgroup.option(
    "-a",
    "--all",
    "install_all",
    is_flag=True,
    help="Automatically install all dependencies (core + optional) without prompting",
)
@add_common_options
@click.pass_context
def check_deps(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _check_deps_impl(
            interactive=kwargs["interactive"],
            core=kwargs["core"],
            install_all=kwargs["install_all"],
        )
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


CommandHelpMetadata(
    key="dependencies",
    one_line_description="Check and install system dependencies",
    synopsis="mngr dependencies [OPTIONS]",
    description="""Checks whether the system dependencies required by mngr are installed.
By default, prints a status table and exits 0 (all present) or 1 (something missing).

Use -i to interactively choose what to install, -c to auto-install core dependencies,
or -a to auto-install everything (core + optional).

Core dependencies: ssh, git, tmux, jq
Optional dependencies: claude (agent type), rsync (push/pull), unison (pair)""",
    examples=(
        ("Check which dependencies are missing", "mngr check-deps"),
        ("Interactively install missing dependencies", "mngr check-deps -i"),
        ("Auto-install core dependencies", "mngr check-deps -c"),
        ("Auto-install everything", "mngr check-deps -a"),
    ),
    see_also=(("extras", "Install optional extras (plugins, completion, etc.)"),),
).register()
add_pager_help_option(check_deps)
