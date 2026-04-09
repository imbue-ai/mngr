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
        "imbue-mngr-claude-mind",
        "imbue-mngr-llm",
        "imbue-mngr-mind",
        "imbue-mngr-mind-chat",
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


class LlmSignalCheck(SignalCheck):
    """Detects whether Simon Willison's llm CLI is installed."""

    command: tuple[str, ...] = ("sh", "-c", "llm --help 2>&1 | grep -q datasette.io")


class ModalSignalCheck(SignalCheck):
    """Detects whether Modal credentials are configured."""

    command: tuple[str, ...] = ("sh", "-c", "test -f ~/.modal.toml")


# Shared instances for use across catalog entries.
_CLAUDE_SIGNAL: Final[ClaudeSignalCheck] = ClaudeSignalCheck()
_OPENCODE_SIGNAL: Final[OpenCodeSignalCheck] = OpenCodeSignalCheck()
_PI_SIGNAL: Final[PiSignalCheck] = PiSignalCheck()
_LLM_SIGNAL: Final[LlmSignalCheck] = LlmSignalCheck()
_MODAL_SIGNAL: Final[ModalSignalCheck] = ModalSignalCheck()


class CatalogEntry(FrozenModel):
    """Metadata for a plugin entry point in the catalog."""

    entry_point_name: str = Field(description="Pluggy entry point name")
    package_name: str = Field(description="PyPI package name")
    description: str = Field(description="Human-readable description")
    tier: PluginTier = Field(description="INDEPENDENT (works alone) or DEPENDENT (needs another plugin's signal)")
    signal: SignalCheck | None = Field(default=None, description="Signal check, or None")
    is_recommended: bool = Field(default=False, description="Whether this plugin is recommended for most users")


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
        entry_point_name="llm",
        package_name="imbue-mngr-llm",
        description="LLM agent plugin for mngr - runs the llm CLI tool as an agent",
        tier=PluginTier.INDEPENDENT,
        signal=_LLM_SIGNAL,
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
    CatalogEntry(
        entry_point_name="claude_mind",
        package_name="imbue-mngr-claude-mind",
        description="Claude mind agent plugin for mngr - base class for mind agents built on Claude Code",
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
        entry_point_name="mind",
        package_name="imbue-mngr-mind",
        description="Common code for mind-based agents in mngr",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="mind_chat",
        package_name="imbue-mngr-mind-chat",
        description="Chat command plugin for mngr - connect to mind chat sessions",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="notifications",
        package_name="imbue-mngr-notifications",
        description="Notification plugin for mngr - alerts when agents transition to WAITING state",
        tier=PluginTier.INDEPENDENT,
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
