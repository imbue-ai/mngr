"""Check and optionally install system dependencies for mngr."""

from typing import Any

import click
from click_option_group import MutuallyExclusiveOptionGroup
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import read_tty_choice
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.utils.deps import ALL_DEPS
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import SystemDependency
from imbue.mngr.utils.deps import check_bash_version
from imbue.mngr.utils.deps import describe_install_commands
from imbue.mngr.utils.deps import detect_os
from imbue.mngr.utils.deps import install_deps_batch
from imbue.mngr.utils.deps import install_modern_bash


def _print_status_table(
    deps: tuple[SystemDependency, ...],
    missing: list[SystemDependency],
    bash_ok: bool,
    os_name: OsName,
) -> None:
    """Print a table showing each dependency and its status."""
    missing_set = {id(d) for d in missing}
    name_width = max(len(d.binary) for d in deps)
    if os_name == OsName.MACOS and not bash_ok:
        name_width = max(name_width, len("bash(4+)"))

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


def _prompt_install_choice(
    missing: list[SystemDependency],
    missing_core: list[SystemDependency],
    need_bash: bool,
    os_name: OsName,
) -> list[SystemDependency] | None:
    """Interactively prompt the user to choose what to install.

    Returns the list of deps to install, or None if the user chose to skip.
    """
    all_commands = describe_install_commands(missing, os_name)
    if need_bash:
        all_commands.append("brew install bash")
    all_names = [d.binary for d in missing]
    if need_bash:
        all_names.append("bash(4+)")
    write_human_line("  [a] Install all ({}):", ", ".join(all_names))
    for cmd in all_commands:
        write_human_line("        {}", cmd)

    if missing_core or need_bash:
        core_commands = describe_install_commands(missing_core, os_name)
        if need_bash:
            core_commands.append("brew install bash")
        core_names = [d.binary for d in missing_core]
        if need_bash:
            core_names.append("bash(4+)")
        write_human_line("  [c] Install core only ({}):", ", ".join(core_names))
        for cmd in core_commands:
            write_human_line("        {}", cmd)

    write_human_line("  [n] Skip -- I'll install them myself")
    write_human_line("")

    choice = read_tty_choice("Choice [a/c/n]: ")
    if choice == "":
        write_human_line("No interactive terminal available. Skipping dependency installation.")
        return None
    if choice.lower() in ("a", "y"):
        return missing
    if choice.lower() == "c":
        return missing_core
    write_human_line("Skipping dependency installation.")
    return None


def _run_installation(
    to_install: list[SystemDependency],
    need_bash: bool,
    os_name: OsName,
) -> list[SystemDependency]:
    """Install the given deps (and modern bash if needed). Returns list of failed deps."""
    failed: list[SystemDependency] = []
    if to_install:
        write_human_line("Installing: {}", ", ".join(d.binary for d in to_install))
        failed = install_deps_batch(to_install, os_name)

    if need_bash:
        write_human_line("Installing modern bash via brew...")
        if not install_modern_bash():
            write_human_line("WARNING: Failed to install modern bash.")

    return failed


def _report_post_install_status(
    failed: list[SystemDependency],
    need_bash: bool,
    os_name: OsName,
) -> bool:
    """Print post-install status. Returns True if all core deps (and bash) are now present."""
    write_human_line("")
    still_missing = [dep for dep in ALL_DEPS if not dep.is_available()]
    bash_ok_now = check_bash_version() if os_name == OsName.MACOS else True

    if failed:
        write_human_line("Failed to install: {}", ", ".join(d.binary for d in failed))

    if still_missing:
        write_human_line("Still missing: {}", ", ".join(d.binary for d in still_missing))

    if os_name == OsName.MACOS and not bash_ok_now and need_bash:
        write_human_line(
            "WARNING: PATH-resolved bash is still old after install. "
            "Ensure /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel) is before /bin in your PATH."
        )

    still_missing_core = [d for d in still_missing if d.category == DependencyCategory.CORE]
    return len(still_missing_core) == 0 and bash_ok_now


def _check_deps_impl(ctx: click.Context, interactive: bool, core: bool, install_all: bool) -> None:
    """Implementation of the dependencies command."""
    os_name = detect_os()

    missing = [dep for dep in ALL_DEPS if not dep.is_available()]
    missing_core = [dep for dep in missing if dep.category == DependencyCategory.CORE]
    bash_ok = check_bash_version() if os_name == OsName.MACOS else True

    write_human_line("System dependencies ({})", os_name)
    _print_status_table(ALL_DEPS, missing, bash_ok, os_name)
    write_human_line("")

    if len(missing) == 0 and bash_ok:
        write_human_line("All system dependencies are present.")
        return

    if not interactive and not core and not install_all:
        count = len(missing) + (0 if bash_ok else 1)
        write_human_line("{} missing dependency(ies). Use -i to install interactively.", count)
        ctx.exit(1)

    need_bash = os_name == OsName.MACOS and not bash_ok

    if install_all:
        to_install: list[SystemDependency] = missing
    elif core:
        to_install = missing_core
    else:
        prompted = _prompt_install_choice(missing, missing_core, need_bash, os_name)
        if prompted is None:
            return
        to_install = prompted

    if not to_install and not need_bash:
        write_human_line("Nothing to install.")
        return

    failed = _run_installation(to_install, need_bash, os_name)
    all_core_ok = _report_post_install_status(failed, need_bash, os_name)
    if not all_core_ok:
        ctx.exit(1)


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
            ctx=ctx,
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
        ("Check which dependencies are missing", "mngr dependencies"),
        ("Interactively install missing dependencies", "mngr dependencies -i"),
        ("Auto-install core dependencies", "mngr dependencies -c"),
        ("Auto-install everything", "mngr dependencies -a"),
    ),
    see_also=(("extras", "Install optional extras (plugins, completion, etc.)"),),
).register()
add_pager_help_option(check_deps)
