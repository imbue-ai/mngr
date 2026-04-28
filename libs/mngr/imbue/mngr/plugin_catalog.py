"""Catalog of mngr plugins with tier and signal metadata.

This module defines the full plugin catalog, signal checks for binary
detection, and helpers used by the install wizard and test fixtures.
"""

from typing import Final

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import PluginTier

# Packages not yet published on PyPI. Excluded from the install wizard.
# Remove entries from here as they get published.
UNPUBLISHED_PACKAGES: Final[frozenset[str]] = frozenset(
    {
        "imbue-mngr-schedule",
        "imbue-mngr-tmr",
    }
)


class SignalCheck(FrozenModel):
    """A heuristic to detect if the user likely wants a plugin enabled.

    Subclass and set ``command`` to define a concrete signal check.
    The command is run as a subprocess. Exit code 0 means the signal
    passes (the user probably wants this plugin). Any nonzero exit or
    FileNotFoundError means the signal does not pass.
    """

    command: tuple[str, ...] = Field(description="Command to run; exit 0 = signal passes")


class ClaudeSignalCheck(SignalCheck):
    """Detects whether the Claude Code CLI is installed."""

    command: tuple[str, ...] = ("claude", "--version")


class OpenCodeSignalCheck(SignalCheck):
    """Detects whether the OpenCode CLI is installed."""

    command: tuple[str, ...] = ("opencode", "--version")


class PiSignalCheck(SignalCheck):
    """Detects whether the Pi coding agent CLI is installed."""

    command: tuple[str, ...] = ("sh", "-c", "pi --help 2>&1 | grep -q 'pi - AI coding assistant'")


class ModalSignalCheck(SignalCheck):
    """Detects whether Modal credentials are configured."""

    command: tuple[str, ...] = ("sh", "-c", "test -f ~/.modal.toml")


class LimaSignalCheck(SignalCheck):
    """Detects whether the limactl CLI is installed."""

    command: tuple[str, ...] = ("limactl", "--version")


# Shared instances for use across catalog entries.
_CLAUDE_SIGNAL: Final[ClaudeSignalCheck] = ClaudeSignalCheck()
_OPENCODE_SIGNAL: Final[OpenCodeSignalCheck] = OpenCodeSignalCheck()
_PI_SIGNAL: Final[PiSignalCheck] = PiSignalCheck()
_MODAL_SIGNAL: Final[ModalSignalCheck] = ModalSignalCheck()
_LIMA_SIGNAL: Final[LimaSignalCheck] = LimaSignalCheck()


class CatalogEntry(FrozenModel):
    """Metadata for a plugin entry point in the catalog."""

    entry_point_name: str = Field(description="Pluggy entry point name")
    package_name: str = Field(description="PyPI package name")
    description: str = Field(description="Human-readable description")
    tier: PluginTier = Field(description="INDEPENDENT (works alone) or DEPENDENT (needs another plugin's signal)")
    signal: SignalCheck | None = Field(default=None, description="Signal check, or None")
    is_recommended: bool = Field(default=False, description="Whether this plugin is recommended for most users")
    cli_command_names: tuple[str, ...] = Field(
        default=(),
        description="Top-level CLI command names this plugin registers, when different from entry_point_name",
    )


# Descriptions sourced from each plugin's pyproject.toml.
PLUGIN_CATALOG: Final[tuple[CatalogEntry, ...]] = (
    # --- INDEPENDENT with signal (binary/credential detection) ---
    CatalogEntry(
        entry_point_name="claude",
        package_name="imbue-mngr-claude",
        description="Claude agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        signal=_CLAUDE_SIGNAL,
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="opencode",
        package_name="imbue-mngr-opencode",
        description="OpenCode agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        signal=_OPENCODE_SIGNAL,
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="pi_coding",
        package_name="imbue-mngr-pi-coding",
        description="Pi coding agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        signal=_PI_SIGNAL,
    ),
    CatalogEntry(
        entry_point_name="modal",
        package_name="imbue-mngr-modal",
        description="Modal provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        signal=_MODAL_SIGNAL,
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="lima",
        package_name="imbue-mngr-lima",
        description="Lima VM provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        signal=_LIMA_SIGNAL,
    ),
    CatalogEntry(
        entry_point_name="vultr",
        package_name="imbue-mngr-vultr",
        description="Vultr provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="tutor",
        package_name="imbue-mngr-tutor",
        description="Interactive tutorial plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        is_recommended=True,
    ),
    # --- DEPENDENT (require another plugin's signal) ---
    CatalogEntry(
        entry_point_name="code_guardian",
        package_name="imbue-mngr-claude",
        description="Code guardian agent for mngr",
        tier=PluginTier.DEPENDENT,
        signal=_CLAUDE_SIGNAL,
    ),
    CatalogEntry(
        entry_point_name="fixme_fairy",
        package_name="imbue-mngr-claude",
        description="Fixme fairy agent for mngr",
        tier=PluginTier.DEPENDENT,
        signal=_CLAUDE_SIGNAL,
    ),
    CatalogEntry(
        entry_point_name="headless_claude",
        package_name="imbue-mngr-claude",
        description="Headless Claude agent for mngr",
        tier=PluginTier.DEPENDENT,
        signal=_CLAUDE_SIGNAL,
    ),
    # --- INDEPENDENT, no signal ---
    CatalogEntry(
        entry_point_name="ttyd",
        package_name="imbue-mngr-ttyd",
        description="ttyd web terminal plugin for mngr - automatically launches a ttyd server alongside agents",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="file",
        package_name="imbue-mngr-file",
        description="File command plugin for mngr - read, write, and list files on agents and hosts",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="kanpan",
        package_name="imbue-mngr-kanpan",
        description="All-seeing agent tracker",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="notifications",
        package_name="imbue-mngr-notifications",
        description="Notification plugin for mngr - alerts when agents transition to WAITING state",
        tier=PluginTier.INDEPENDENT,
        cli_command_names=("notify",),
    ),
    CatalogEntry(
        entry_point_name="pair",
        package_name="imbue-mngr-pair",
        description="Pair command plugin for mngr - continuous file sync between agent and local directory",
        tier=PluginTier.INDEPENDENT,
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="recursive",
        package_name="imbue-mngr-recursive",
        description="Recursive mngr plugin: injects mngr config and dependencies into remote hosts",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="schedule",
        package_name="imbue-mngr-schedule",
        description="Schedule command plugin for mngr - schedule remote invocations of mngr commands",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="tmr",
        package_name="imbue-mngr-tmr",
        description="Test map-reduce plugin for mngr - launch agents to run and fix tests in parallel",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="wait",
        package_name="imbue-mngr-wait",
        description="Wait plugin for mngr - wait for agents/hosts to reach target states",
        tier=PluginTier.INDEPENDENT,
    ),
)

# Pre-computed index for fast lookup by entry point name.
_CATALOG_BY_ENTRY_POINT: Final[dict[str, CatalogEntry]] = {e.entry_point_name: e for e in PLUGIN_CATALOG}


def _build_catalog_by_cli_command() -> dict[str, CatalogEntry]:
    index: dict[str, CatalogEntry] = {}
    for entry in PLUGIN_CATALOG:
        names = entry.cli_command_names or (entry.entry_point_name,)
        for name in names:
            index.setdefault(name, entry)
    return index


_CATALOG_BY_CLI_COMMAND: Final[dict[str, CatalogEntry]] = _build_catalog_by_cli_command()


def get_catalog_entry(entry_point_name: str) -> CatalogEntry | None:
    """Look up a catalog entry by its pluggy entry point name.

    Returns None if the entry point is not in the catalog (e.g. third-party plugin).
    """
    return _CATALOG_BY_ENTRY_POINT.get(entry_point_name)


def get_all_cataloged_entry_point_names() -> frozenset[str]:
    """Return all entry point names in the catalog."""
    return frozenset(_CATALOG_BY_ENTRY_POINT.keys())


def get_independent_entry_point_names() -> frozenset[str]:
    """Return entry point names for all INDEPENDENT-tier plugins."""
    return frozenset(e.entry_point_name for e in PLUGIN_CATALOG if e.tier == PluginTier.INDEPENDENT)


def check_signal(signal: SignalCheck) -> bool:
    """Run a signal check and return whether it passes.

    Returns True if the command exits with code 0, False otherwise.
    """
    with ConcurrencyGroup(name="signal-check") as cg:
        try:
            cg.run_process_to_completion(signal.command, timeout=10.0)
            return True
        except (ProcessError, FileNotFoundError, OSError):
            return False


def _format_install_hint(entry: CatalogEntry) -> str:
    return (
        f"This plugin is provided by '{entry.package_name}' ({entry.description})."
        f" Install it (e.g. reinstall the mngr uv tool with '--with {entry.package_name}')"
        " and ensure the plugin is enabled."
    )


def get_plugin_install_hint(name: str) -> str:
    """Return user-facing help text for a missing plugin entry point.

    If the name appears in the catalog, names the actual PyPI package and
    description. Otherwise returns a generic prompt to check installed
    plugins, since fabricating a package name for an unknown name would be
    misleading.
    """
    entry = get_catalog_entry(name)
    if entry is not None:
        return _format_install_hint(entry)
    return (
        f"'{name}' is not a known mngr plugin. If it is provided by a"
        " third-party plugin, install that package and ensure the plugin is"
        " enabled. Run 'mngr extras' to see installable plugins."
    )


def get_install_hint_for_cli_command(command_name: str) -> str | None:
    """Return install help for a CLI command provided by a known plugin, or None.

    Returns None when the command name is not registered by any cataloged
    plugin, so callers can fall back to the default click "no such command"
    error without fabricating advice.
    """
    entry = _CATALOG_BY_CLI_COMMAND.get(command_name)
    if entry is None:
        return None
    return _format_install_hint(entry)


def get_installable_packages() -> tuple[CatalogEntry, ...]:
    """Return one representative CatalogEntry per unique package.

    Used by the install wizard to show per-package choices. Returns the
    first catalog entry for each package (typically the INDEPENDENT-tier entry
    if one exists). Excludes packages not yet published on PyPI.
    """
    seen: set[str] = set()
    result: list[CatalogEntry] = []
    for entry in PLUGIN_CATALOG:
        if entry.package_name in UNPUBLISHED_PACKAGES:
            continue
        if entry.package_name not in seen:
            seen.add(entry.package_name)
            result.append(entry)
    return tuple(result)
