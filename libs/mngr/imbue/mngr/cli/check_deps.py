"""Check and optionally install system dependencies for mngr."""

from enum import auto
from typing import Any

import click
from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
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


class DependencyScope(UpperCaseStrEnum):
    """Which dependencies determine success (the exit code) and the install target.

    This is orthogonal to whether/how we install (see ``InstallMode``): it only
    selects which dependencies "count".
    """

    # Only core dependencies count: exit non-zero only if a core dependency is missing.
    CORE = auto()
    # Every dependency counts (core + optional): exit non-zero if anything is missing.
    ALL = auto()


class InstallMode(UpperCaseStrEnum):
    """Whether (and how) to install missing dependencies.

    Orthogonal to ``DependencyScope``: the scope decides what gets installed under
    ``AUTO`` and what determines the exit code; this decides the install behavior.
    """

    # Check only -- never install.
    NONE = auto()
    # Prompt the user before installing anything.
    INTERACTIVE = auto()
    # Install missing in-scope dependencies without prompting.
    AUTO = auto()


def _scope_choices() -> list[str]:
    return [s.value.lower() for s in DependencyScope]


def _install_choices() -> list[str]:
    return [m.value.lower() for m in InstallMode]


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


def _scope_missing(missing: list[SystemDependency], scope: DependencyScope) -> list[SystemDependency]:
    """Filter the missing deps down to those that count for the given scope.

    Under ``CORE`` scope only core dependencies count; under ``ALL`` scope every
    missing dependency counts. (Modern bash on macOS is a core requirement but is
    not a ``SystemDependency``, so it is tracked separately via ``need_bash``.)
    """
    if scope == DependencyScope.CORE:
        return [dep for dep in missing if dep.category == DependencyCategory.CORE]
    return list(missing)


def _should_fail(missing: list[SystemDependency], scope: DependencyScope, need_bash: bool) -> bool:
    """Whether the command should exit non-zero: any in-scope dep (or core bash) missing."""
    return bool(_scope_missing(missing, scope)) or need_bash


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
    still_missing: list[SystemDependency],
    os_name: OsName,
    tried_bash: bool,
    bash_ok_now: bool,
) -> None:
    """Print which deps failed to install and which are still missing after the attempt."""
    write_human_line("")

    if failed:
        write_human_line("Failed to install: {}", ", ".join(d.binary for d in failed))

    if still_missing:
        write_human_line("Still missing: {}", ", ".join(d.binary for d in still_missing))

    if os_name == OsName.MACOS and tried_bash and not bash_ok_now:
        write_human_line(
            "WARNING: PATH-resolved bash is still old after install. "
            "Ensure /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel) is before /bin in your PATH."
        )


def _check_deps_impl(ctx: click.Context, scope: DependencyScope, install_mode: InstallMode) -> None:
    """Implementation of the dependencies command.

    Two orthogonal axes:
      - ``scope`` selects which dependencies count: ``core`` means exit non-zero
        only if a core dependency is missing (missing optional deps are tolerated);
        ``all`` means exit non-zero if anything is missing. It also selects the
        ``auto`` install target.
      - ``install_mode`` selects whether/how to install: ``none`` (check only),
        ``interactive`` (prompt), or ``auto`` (install without prompting).
    """
    os_name = detect_os()

    missing = [dep for dep in ALL_DEPS if not dep.is_available()]
    missing_core = [dep for dep in missing if dep.category == DependencyCategory.CORE]
    bash_ok = check_bash_version() if os_name == OsName.MACOS else True
    # Modern bash on macOS is a core requirement, so it counts under either scope.
    need_bash = os_name == OsName.MACOS and not bash_ok

    write_human_line("System dependencies ({})", os_name)
    _print_status_table(ALL_DEPS, missing, bash_ok, os_name)
    write_human_line("")

    if not missing and not need_bash:
        write_human_line("All system dependencies are present.")
        return

    # Check only: report status scoped to what determines the exit code, then exit.
    if install_mode == InstallMode.NONE:
        # bash (macOS) is a core requirement, so it counts toward the in-scope total.
        in_scope_count = len(_scope_missing(missing, scope)) + (0 if bash_ok else 1)
        if in_scope_count > 0:
            noun = "core dependency(ies)" if scope == DependencyScope.CORE else "dependency(ies)"
            write_human_line(
                "{} missing {}. Use --install interactive to choose what to install.", in_scope_count, noun
            )
        else:
            # We only reach the NONE branch when something is missing, and a zero
            # in-scope count with no missing bash means scope is core and every
            # missing dep is optional -- tolerated, so this still exits 0.
            optional_missing_count = len(missing) - len(missing_core)
            write_human_line(
                "All core dependencies present. {} optional dependency(ies) missing (tolerated by --scope core).",
                optional_missing_count,
            )
        if _should_fail(missing, scope, need_bash):
            ctx.exit(1)
        return

    # Decide what to install. AUTO installs the in-scope missing deps directly;
    # INTERACTIVE delegates the choice to the prompt.
    if install_mode == InstallMode.AUTO:
        to_install: list[SystemDependency] = _scope_missing(missing, scope)
    else:
        prompted = _prompt_install_choice(missing, missing_core, need_bash, os_name)
        if prompted is None:
            # User skipped: exit based on the (unchanged) in-scope state.
            if _should_fail(missing, scope, need_bash):
                ctx.exit(1)
            return
        to_install = prompted

    if not to_install and not need_bash:
        # Nothing in scope to install (e.g. --scope core when only optional deps
        # are missing). Honor the scope verdict uniformly rather than assuming a
        # zero exit -- a dep with no install_method would otherwise slip through.
        write_human_line("Nothing to install.")
        if _should_fail(missing, scope, need_bash):
            ctx.exit(1)
        return

    failed = _run_installation(to_install, need_bash, os_name)
    bash_ok_now = check_bash_version() if os_name == OsName.MACOS else True
    need_bash_now = os_name == OsName.MACOS and not bash_ok_now
    still_missing = [dep for dep in ALL_DEPS if not dep.is_available()]
    _report_post_install_status(failed, still_missing, os_name, tried_bash=need_bash, bash_ok_now=bash_ok_now)
    if _should_fail(still_missing, scope, need_bash_now):
        ctx.exit(1)


@click.command(name="dependencies", hidden=True)
@click.option(
    "--scope",
    type=click.Choice(_scope_choices(), case_sensitive=False),
    default=DependencyScope.ALL.value.lower(),
    show_default=True,
    help="Which dependencies must be present for a zero exit code (and which to auto-install): "
    "'core' (exit non-zero only if a core dependency is missing; missing optional deps are tolerated) "
    "or 'all' (exit non-zero if anything is missing).",
)
@click.option(
    "--install",
    "install_mode",
    type=click.Choice(_install_choices(), case_sensitive=False),
    default=InstallMode.NONE.value.lower(),
    show_default=True,
    help="Whether to install missing dependencies: "
    "'none' (check only), 'interactive' (prompt before installing), or "
    "'auto' (install missing in-scope dependencies without prompting).",
)
@add_common_options
@click.pass_context
def check_deps(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _check_deps_impl(
            ctx=ctx,
            scope=DependencyScope(kwargs["scope"].upper()),
            install_mode=InstallMode(kwargs["install_mode"].upper()),
        )
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


CommandHelpMetadata(
    key="dependencies",
    one_line_description="Check and install system dependencies",
    synopsis="mngr dependencies [OPTIONS]",
    description="""Checks whether the system dependencies required by mngr are installed.
Prints a status table; by default exits 0 (all present) or 1 (something missing).

Two orthogonal options control its behavior:
  --scope core|all      Which dependencies count toward the exit code (and which
                        --install auto targets). 'core' exits non-zero only when a
                        core dependency is missing -- missing optional deps are
                        tolerated. 'all' (default) exits non-zero if anything is missing.
  --install none|interactive|auto
                        Whether to install missing deps. 'none' (default) only
                        checks; 'interactive' prompts; 'auto' installs the in-scope
                        missing deps without prompting.

Core dependencies: git, tmux, jq
Optional dependencies: ssh (remote connect / rsync / git over SSH), claude (agent type),
rsync (push/pull), unison (pair)""",
    examples=(
        ("Check which dependencies are missing", "mngr dependencies"),
        ("Fail only if a core dependency is missing", "mngr dependencies --scope core"),
        ("Interactively install missing dependencies", "mngr dependencies --install interactive"),
        ("Auto-install only the core dependencies", "mngr dependencies --scope core --install auto"),
        ("Auto-install everything", "mngr dependencies --install auto"),
    ),
    see_also=(("extras", "Install optional extras (plugins, completion, etc.)"),),
).register()
add_pager_help_option(check_deps)
