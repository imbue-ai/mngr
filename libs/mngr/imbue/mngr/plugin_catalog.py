"""Catalog of mngr plugins with tier and signal metadata.

This module defines the full plugin catalog, signal checks for binary
detection, and helpers used by the install wizard and test fixtures.
"""

import subprocess
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import PluginTier


class SignalCheck(FrozenModel):
    """A heuristic to detect if the user likely wants a plugin enabled.

    The command is run as a subprocess. Exit code 0 means the signal
    passes (the user probably wants this plugin). Any nonzero exit or
    FileNotFoundError means the signal does not pass.
    """

    command: tuple[str, ...] = Field(description="Command to run; exit 0 = signal passes")


SIGNAL_CHECKS: Final[dict[str, SignalCheck]] = {
    "claude": SignalCheck(command=("claude", "--version")),
    "opencode": SignalCheck(command=("opencode", "--version")),
    "pi": SignalCheck(command=("sh", "-c", "pi --help 2>&1 | grep -q 'pi - AI coding assistant'")),
    "llm": SignalCheck(command=("sh", "-c", "llm --help 2>&1 | grep -q datasette.io")),
    "modal": SignalCheck(command=("sh", "-c", "test -f ~/.modal.toml")),
}


class CatalogEntry(FrozenModel):
    """Metadata for a plugin entry point in the catalog."""

    entry_point_name: str = Field(description="Pluggy entry point name")
    package_name: str = Field(description="PyPI package name")
    description: str = Field(description="Human-readable description")
    tier: PluginTier = Field(description="BASIC (preselected when signal passes) or EXTRA (manual opt-in)")
    signal: str | None = Field(default=None, description="Key into SIGNAL_CHECKS, or None")


# Descriptions sourced from each plugin's pyproject.toml.
PLUGIN_CATALOG: Final[tuple[CatalogEntry, ...]] = (
    # --- BASIC tier: preselected in wizard when signal passes ---
    CatalogEntry(
        entry_point_name="claude",
        package_name="imbue-mngr-claude",
        description="Claude agent type plugin for mngr",
        tier=PluginTier.BASIC,
        signal="claude",
    ),
    CatalogEntry(
        entry_point_name="opencode",
        package_name="imbue-mngr-opencode",
        description="OpenCode agent type plugin for mngr",
        tier=PluginTier.BASIC,
        signal="opencode",
    ),
    CatalogEntry(
        entry_point_name="pi_coding",
        package_name="imbue-mngr-pi-coding",
        description="Pi coding agent type plugin for mngr",
        tier=PluginTier.BASIC,
        signal="pi",
    ),
    CatalogEntry(
        entry_point_name="llm",
        package_name="imbue-mngr-llm",
        description="LLM agent plugin for mngr - runs the llm CLI tool as an agent",
        tier=PluginTier.BASIC,
        signal="llm",
    ),
    CatalogEntry(
        entry_point_name="modal",
        package_name="imbue-mngr-modal",
        description="Modal provider backend plugin for mngr",
        tier=PluginTier.BASIC,
        signal="modal",
    ),
    # --- EXTRA tier with signal ---
    CatalogEntry(
        entry_point_name="code_guardian",
        package_name="imbue-mngr-claude",
        description="Code guardian agent for mngr",
        tier=PluginTier.EXTRA,
        signal="claude",
    ),
    CatalogEntry(
        entry_point_name="fixme_fairy",
        package_name="imbue-mngr-claude",
        description="Fixme fairy agent for mngr",
        tier=PluginTier.EXTRA,
        signal="claude",
    ),
    CatalogEntry(
        entry_point_name="headless_claude",
        package_name="imbue-mngr-claude",
        description="Headless Claude agent for mngr",
        tier=PluginTier.EXTRA,
        signal="claude",
    ),
    CatalogEntry(
        entry_point_name="claude_mind",
        package_name="imbue-mngr-claude-mind",
        description="Claude mind agent plugin for mngr - base class for mind agents built on Claude Code",
        tier=PluginTier.EXTRA,
        signal="claude",
    ),
    CatalogEntry(
        entry_point_name="elena_code",
        package_name="imbue-mngr-elena-code",
        description="Elena Code agent plugin for mngr - a conversational AI mind powered by Claude Code",
        tier=PluginTier.EXTRA,
        signal="claude",
    ),
    CatalogEntry(
        entry_point_name="test_coder",
        package_name="imbue-mngr-test-coder",
        description="Test agent type plugin for mngr - uses matched-responses model for testing without API keys",
        tier=PluginTier.EXTRA,
        signal="llm",
    ),
    # --- EXTRA tier, no signal ---
    CatalogEntry(
        entry_point_name="ttyd",
        package_name="imbue-mngr-ttyd",
        description="ttyd web terminal plugin for mngr - automatically launches a ttyd server alongside agents",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="file",
        package_name="imbue-mngr-file",
        description="File command plugin for mngr - read, write, and list files on agents and hosts",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="kanpan",
        package_name="imbue-mngr-kanpan",
        description="All-seeing agent tracker",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="mind",
        package_name="imbue-mngr-mind",
        description="Common code for mind-based agents in mngr",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="mind_chat",
        package_name="imbue-mngr-mind-chat",
        description="Chat command plugin for mngr - connect to mind chat sessions",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="notifications",
        package_name="imbue-mngr-notifications",
        description="Notification plugin for mngr - alerts when agents transition to WAITING state",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="pair",
        package_name="imbue-mngr-pair",
        description="Pair command plugin for mngr - continuous file sync between agent and local directory",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="recursive",
        package_name="imbue-mngr-recursive",
        description="Recursive mngr plugin: injects mngr config and dependencies into remote hosts",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="schedule",
        package_name="imbue-mngr-schedule",
        description="Schedule command plugin for mngr - schedule remote invocations of mngr commands",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="tmr",
        package_name="imbue-mngr-tmr",
        description="Test map-reduce plugin for mngr - launch agents to run and fix tests in parallel",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="tutor",
        package_name="imbue-mngr-tutor",
        description="Interactive tutorial plugin for mngr",
        tier=PluginTier.EXTRA,
    ),
    CatalogEntry(
        entry_point_name="wait",
        package_name="imbue-mngr-wait",
        description="Wait plugin for mngr - wait for agents/hosts to reach target states",
        tier=PluginTier.EXTRA,
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


def check_signal(signal: SignalCheck) -> bool:
    """Run a signal check and return whether it passes.

    Returns True if the command exits with code 0, False otherwise.
    """
    try:
        result = subprocess.run(
            signal.command,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def get_installable_packages() -> tuple[CatalogEntry, ...]:
    """Return one representative CatalogEntry per unique package.

    Used by the install wizard to show per-package choices. Returns the
    first catalog entry for each package (typically the BASIC-tier entry
    if one exists).
    """
    seen: set[str] = set()
    result: list[CatalogEntry] = []
    for entry in PLUGIN_CATALOG:
        if entry.package_name not in seen:
            seen.add(entry.package_name)
            result.append(entry)
    return tuple(result)
