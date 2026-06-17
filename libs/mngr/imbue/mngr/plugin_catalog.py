"""Catalog of mngr plugins with tier and signal metadata.

This module defines the full plugin catalog, signal checks for binary
detection, and helpers used by the install wizard and test fixtures.
"""

from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import PluginKind
from imbue.mngr.primitives import PluginTier

# Workspace packages that are deliberately NOT published to PyPI. This is the
# single opt-out consulted by two consumers:
#   - the install wizard (below), which never offers an unpublished package; and
#   - the release tooling (scripts/utils.py), which auto-discovers every libs/*
#     package as a publish candidate and subtracts this set. Anything NOT listed
#     here is treated as publishable and will be offered for release.
#
# Some entries are permanently internal (a library with no CLI, an experimental
# plugin, or a test-only helper); others are simply not ready yet. Remove an
# entry once the package should start publishing -- the next release run will
# then offer it (a first publication also needs a PyPI Trusted Publisher
# registered; see scripts/release.py's new-package flow).
UNPUBLISHED_PACKAGES: Final[frozenset[str]] = frozenset(
    {
        # Map-reduce framework library; consumed only by recipes (e.g. tmr), no CLI of its own.
        "imbue-mngr-mapreduce",
        # Experimental: reroutes Claude Code subagents; depends on Claude Code internals.
        "imbue-mngr-claude-subagent-proxy",
        # Canonical mapreduce recipe (test fan-out); internal tooling, offered nowhere on PyPI.
        "imbue-mngr-tmr",
        # End-to-end test helper used only by mngr's own test suite (not an mngr plugin).
        "skitwright",
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


class CodexSignalCheck(SignalCheck):
    """Detects whether the OpenAI Codex CLI is installed."""

    command: tuple[str, ...] = ("codex", "--version")


class AntigravitySignalCheck(SignalCheck):
    """Detects whether the Antigravity CLI is installed."""

    command: tuple[str, ...] = ("agy", "--version")


class PiSignalCheck(SignalCheck):
    """Detects whether the Pi coding agent CLI is installed."""

    command: tuple[str, ...] = ("sh", "-c", "pi --help 2>&1 | grep -q 'pi - AI coding assistant'")


class ModalSignalCheck(SignalCheck):
    """Detects whether Modal credentials are configured."""

    command: tuple[str, ...] = ("sh", "-c", "test -f ~/.modal.toml")


class LimaSignalCheck(SignalCheck):
    """Detects whether the limactl CLI is installed."""

    command: tuple[str, ...] = ("limactl", "--version")


class AwsSignalCheck(SignalCheck):
    """Detects whether the AWS CLI is installed."""

    command: tuple[str, ...] = ("aws", "--version")


class GcloudSignalCheck(SignalCheck):
    """Detects whether the Google Cloud CLI is installed."""

    command: tuple[str, ...] = ("gcloud", "--version")


class AzureSignalCheck(SignalCheck):
    """Detects whether the Azure CLI is installed."""

    command: tuple[str, ...] = ("az", "--version")


# Shared instances for use across catalog entries.
_CLAUDE_SIGNAL: Final[ClaudeSignalCheck] = ClaudeSignalCheck()
_OPENCODE_SIGNAL: Final[OpenCodeSignalCheck] = OpenCodeSignalCheck()
_CODEX_SIGNAL: Final[CodexSignalCheck] = CodexSignalCheck()
_ANTIGRAVITY_SIGNAL: Final[AntigravitySignalCheck] = AntigravitySignalCheck()
_PI_SIGNAL: Final[PiSignalCheck] = PiSignalCheck()
_MODAL_SIGNAL: Final[ModalSignalCheck] = ModalSignalCheck()
_LIMA_SIGNAL: Final[LimaSignalCheck] = LimaSignalCheck()
_AWS_SIGNAL: Final[AwsSignalCheck] = AwsSignalCheck()
_GCLOUD_SIGNAL: Final[GcloudSignalCheck] = GcloudSignalCheck()
_AZURE_SIGNAL: Final[AzureSignalCheck] = AzureSignalCheck()


class SignalGate(FrozenModel):
    """A gate keyed on a detectable tool.

    On an INDEPENDENT entry the signal is probed to decide phase-1 preselection,
    and once the entry is selected the signal becomes "accepted" for dependents.
    A DEPENDENT entry carrying this gate is offered in phase 2 when its signal was
    accepted in phase 1.
    """

    signal: SignalCheck

    def detection_signal(self) -> SignalCheck | None:
        return self.signal

    def is_unlocked(self, *, accepted_signals: set[SignalCheck], present_packages: frozenset[str]) -> bool:
        return self.signal in accepted_signals


class RequiredPackagesGate(FrozenModel):
    """A gate for a DEPENDENT entry, offered in phase 2 only when every named
    package is present -- already installed, or selected earlier in the wizard.
    """

    packages: tuple[str, ...]

    def detection_signal(self) -> SignalCheck | None:
        return None

    def is_unlocked(self, *, accepted_signals: set[SignalCheck], present_packages: frozenset[str]) -> bool:
        return all(package in present_packages for package in self.packages)


Gate = SignalGate | RequiredPackagesGate


class SignalGate(FrozenModel):
    """A gate keyed on a detectable tool.

    On an INDEPENDENT entry the signal is probed to decide phase-1 preselection,
    and once the entry is selected the signal becomes "accepted" for dependents.
    A DEPENDENT entry carrying this gate is offered in phase 2 when its signal was
    accepted in phase 1.
    """

    signal: SignalCheck

    def detection_signal(self) -> SignalCheck | None:
        return self.signal

    def is_unlocked(self, *, accepted_signals: set[SignalCheck], present_packages: frozenset[str]) -> bool:
        return self.signal in accepted_signals


class RequiredPackagesGate(FrozenModel):
    """A gate for a DEPENDENT entry, offered in phase 2 only when every named
    package is present -- already installed, or selected earlier in the wizard.
    """

    packages: tuple[str, ...]

    def detection_signal(self) -> SignalCheck | None:
        return None

    def is_unlocked(self, *, accepted_signals: set[SignalCheck], present_packages: frozenset[str]) -> bool:
        return all(package in present_packages for package in self.packages)


Gate = SignalGate | RequiredPackagesGate


class CatalogEntry(FrozenModel):
    """Metadata for a plugin entry point in the catalog."""

    entry_point_name: str = Field(description="Pluggy entry point name")
    package_name: str = Field(description="PyPI package name")
    description: str = Field(description="Human-readable description")
    tier: PluginTier = Field(description="INDEPENDENT (works alone) or DEPENDENT (unlocked by its gate)")
    gate: Gate | None = Field(
        default=None,
        description=(
            "How the wizard decides to offer this entry: a SignalGate (a detected tool) or a"
            " RequiredPackagesGate (other packages must be present). None means no gate."
        ),
    )
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
        gate=SignalGate(signal=_CLAUDE_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="opencode",
        package_name="imbue-mngr-opencode",
        description="OpenCode agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_OPENCODE_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="codex",
        package_name="imbue-mngr-codex",
        description="Codex agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_CODEX_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="antigravity",
        package_name="imbue-mngr-antigravity",
        description="Antigravity agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_ANTIGRAVITY_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="pi_coding",
        package_name="imbue-mngr-pi-coding",
        description="Pi coding agent type plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_PI_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="modal",
        package_name="imbue-mngr-modal",
        description="Modal provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_MODAL_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="lima",
        package_name="imbue-mngr-lima",
        description="Lima VM provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_LIMA_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="vultr",
        package_name="imbue-mngr-vultr",
        description="Vultr provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="aws",
        package_name="imbue-mngr-aws",
        description="AWS provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_AWS_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="gcp",
        package_name="imbue-mngr-gcp",
        description="GCP Compute Engine provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_GCLOUD_SIGNAL),
        is_recommended=True,
    ),
    CatalogEntry(
        entry_point_name="ovh",
        package_name="imbue-mngr-ovh",
        description="OVH Cloud VPS provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
    ),
    CatalogEntry(
        entry_point_name="azure",
        package_name="imbue-mngr-azure",
        description="Azure Virtual Machines provider backend plugin for mngr",
        tier=PluginTier.INDEPENDENT,
        gate=SignalGate(signal=_AZURE_SIGNAL),
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
        gate=SignalGate(signal=_CLAUDE_SIGNAL),
    ),
    CatalogEntry(
        entry_point_name="fixme_fairy",
        package_name="imbue-mngr-claude",
        description="Fixme fairy agent for mngr",
        tier=PluginTier.DEPENDENT,
        gate=SignalGate(signal=_CLAUDE_SIGNAL),
    ),
    CatalogEntry(
        entry_point_name="headless_claude",
        package_name="imbue-mngr-claude",
        description="Headless Claude agent for mngr",
        tier=PluginTier.DEPENDENT,
        gate=SignalGate(signal=_CLAUDE_SIGNAL),
    ),
    # Per-harness usage data providers: each only makes sense once you have both
    # the matching agent plugin and the base usage plugin, so they are offered in
    # phase 2 gated on both being present (installed or selected in phase 1).
    CatalogEntry(
        entry_point_name="claude_usage",
        package_name="imbue-mngr-claude-usage",
        description="Claude usage data provider for `mngr usage`",
        tier=PluginTier.DEPENDENT,
        is_recommended=True,
        gate=RequiredPackagesGate(packages=("imbue-mngr-claude", "imbue-mngr-usage")),
    ),
    CatalogEntry(
        entry_point_name="codex_usage",
        package_name="imbue-mngr-codex-usage",
        description="Codex usage data provider for `mngr usage`",
        tier=PluginTier.DEPENDENT,
        is_recommended=True,
        gate=RequiredPackagesGate(packages=("imbue-mngr-codex", "imbue-mngr-usage")),
    ),
    CatalogEntry(
        entry_point_name="opencode_usage",
        package_name="imbue-mngr-opencode-usage",
        description="OpenCode usage data provider for `mngr usage`",
        tier=PluginTier.DEPENDENT,
        is_recommended=True,
        gate=RequiredPackagesGate(packages=("imbue-mngr-opencode", "imbue-mngr-usage")),
    ),
    CatalogEntry(
        entry_point_name="pi_coding_usage",
        package_name="imbue-mngr-pi-coding-usage",
        description="pi usage data provider for `mngr usage`",
        tier=PluginTier.DEPENDENT,
        is_recommended=True,
        gate=RequiredPackagesGate(packages=("imbue-mngr-pi-coding", "imbue-mngr-usage")),
    ),
    # --- INDEPENDENT, no signal ---
    CatalogEntry(
        entry_point_name="usage",
        package_name="imbue-mngr-usage",
        description="Cost / quota usage tracking for mngr agents (`mngr usage`)",
        tier=PluginTier.INDEPENDENT,
        is_recommended=True,
    ),
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


def get_plugin_install_hint(
    name: str,
    kind: PluginKind = PluginKind.AGENT_TYPE,
) -> str:
    """Return user-facing help text for a missing plugin entry point.

    If the name appears in the catalog, names the actual PyPI package and
    description. Otherwise returns a generic prompt to check installed
    plugins, since fabricating a package name for an unknown name would be
    misleading.

    When ``kind`` is ``PluginKind.AGENT_TYPE`` (the default), the fallback
    also points at ``--type command -- <shell command>`` for callers who
    actually just want to run a shell command. That tip is suppressed for
    ``PluginKind.PROVIDER``, where it would be irrelevant (provider backends
    have no equivalent shell-command escape hatch).
    """
    entry = get_catalog_entry(name)
    if entry is not None:
        return _format_install_hint(entry)
    fallback = (
        f"We do not recognize '{name}'. If it is provided by a third-party"
        " plugin, install that package and ensure the plugin is enabled."
    )
    match kind:
        case PluginKind.AGENT_TYPE:
            fallback += (
                " To run an arbitrary shell command without registering a type,"
                " use `--type command -- <shell command>` instead."
            )
        case PluginKind.PROVIDER:
            pass
        case _:
            assert_never(kind)
    return fallback


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
