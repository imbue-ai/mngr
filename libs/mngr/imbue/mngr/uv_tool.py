"""Read and manipulate the ``uv tool`` receipt for mngr.

When mngr is installed via ``uv tool install imbue-mngr``, uv stores a receipt
at ``<venv>/uv-receipt.toml`` that records the base package and any
extra ``--with`` dependencies.  This module reads that receipt and
builds ``uv tool install`` commands that preserve existing dependencies
while adding or removing plugins.
"""

import importlib.metadata
import sys
import tomllib
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.output_helpers import AbortError

_CONSTRAINTS_FILENAME: Final[str] = "constraints.txt"

_RECEIPT_FILENAME: Final[str] = "uv-receipt.toml"


class ToolRequirement(FrozenModel):
    """A single requirement entry from the uv-receipt.toml file."""

    name: str = Field(description="Package name")
    specifier: str | None = Field(default=None, description="Version specifier (e.g. '>=1.0')")
    editable: str | None = Field(default=None, description="Local editable path (from --with-editable)")
    directory: str | None = Field(default=None, description="Local directory path (from -e / --editable on the base)")
    git: str | None = Field(default=None, description="Git URL")


class ToolReceipt(FrozenModel):
    """Parsed uv-receipt.toml split into the base mngr requirement and extras."""

    base: ToolRequirement = Field(description="The base mngr requirement (positional arg to uv tool install)")
    extras: list[ToolRequirement] = Field(description="Additional --with / --with-editable dependencies")


@pure
def _requirement_to_with_arg(requirement: ToolRequirement) -> tuple[str, str]:
    """Convert a requirement to a (flag, value) pair for ``uv tool install``.

    Returns either ``("--with", specifier)`` or ``("--with-editable", path)``.
    """
    if requirement.editable is not None:
        return ("--with-editable", requirement.editable)

    if requirement.directory is not None:
        return ("--with-editable", requirement.directory)

    if requirement.git is not None:
        return ("--with", f"{requirement.name} @ git+{requirement.git}")

    if requirement.specifier is not None:
        return ("--with", f"{requirement.name}{requirement.specifier}")

    return ("--with", requirement.name)


def get_receipt_path() -> Path | None:
    """Return the path to the uv-receipt.toml if it exists, else None.

    The receipt lives at ``sys.prefix / uv-receipt.toml`` when mngr was
    installed via ``uv tool install``.
    """
    receipt = Path(sys.prefix) / _RECEIPT_FILENAME
    if receipt.is_file():
        return receipt
    return None


def require_uv_tool_receipt() -> Path:
    """Return the receipt path or raise if mngr was not installed via ``uv tool``.

    Call this at the top of any command that modifies the tool's dependencies.
    """
    receipt = get_receipt_path()
    if receipt is None:
        raise AbortError(
            "The current mngr instance is not installed via 'uv tool install'. "
            "To add or remove plugins, simply use whatever commands you use to manage Python dependencies."
        )
    return receipt


def read_receipt(receipt_path: Path) -> ToolReceipt:
    """Parse a uv-receipt.toml into a base requirement and extras."""
    with receipt_path.open("rb") as f:
        data = tomllib.load(f)

    raw_reqs: list[dict[str, Any]] = data.get("tool", {}).get("requirements", [])
    requirements = [ToolRequirement(**r) for r in raw_reqs]

    base = ToolRequirement(name="imbue-mngr")
    for requirement in requirements:
        if requirement.name == "imbue-mngr":
            base = requirement
            break

    extras = [r for r in requirements if r.name != "imbue-mngr"]

    return ToolReceipt(base=base, extras=extras)


def has_mngr_entry_points(package_name: str) -> bool:
    """Return whether an installed package registers any ``mngr`` entry points.

    This is what distinguishes an actual mngr plugin from a plain library: the
    uv-tool receipt's extras include every ``--with`` dependency (e.g. workspace
    libraries like ``imbue-common`` or ``concurrency-group``), but only packages
    that declare ``mngr``-group entry points are plugins. Returns False if the
    package is not installed.
    """
    try:
        dist = importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return any(entry_point.group == "mngr" for entry_point in dist.entry_points)


def _read_receipt_or_none(receipt_path: Path) -> ToolReceipt | None:
    """Parse a uv-tool receipt, returning None when it cannot be read.

    The receipt is machine-written by uv; a parse/read failure means it is corrupt or
    unreadable. Callers degrade gracefully (no plugin names), but the corruption is surfaced
    via a warning rather than swallowed silently.
    """
    try:
        return read_receipt(receipt_path)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read uv-tool receipt at {} for plugin completion: {}", receipt_path, e)
        return None


def _plugin_package_names_from_receipt(receipt: ToolReceipt, is_plugin_package: Callable[[str], bool]) -> list[str]:
    """Filter a receipt's extras down to installed plugin package names, sorted and deduplicated.

    The extras list every ``--with`` dependency, including non-plugin libraries pulled in
    alongside editable plugins; ``is_plugin_package`` keeps only packages that actually register
    ``mngr`` entry points (see ``has_mngr_entry_points``).
    """
    return sorted({requirement.name for requirement in receipt.extras if is_plugin_package(requirement.name)})


def get_installed_plugin_package_names() -> list[str]:
    """Return installed plugin package names from the live uv-tool receipt, best-effort.

    These are the package names ``mngr plugin remove`` accepts. Returns an empty list when mngr
    was not installed via ``uv tool`` or the receipt cannot be read -- callers (e.g. the tab
    completion cache writer) must never fail on a missing/garbled receipt.
    """
    receipt_path = get_receipt_path()
    if receipt_path is None:
        return []
    receipt = _read_receipt_or_none(receipt_path)
    if receipt is None:
        return []
    return _plugin_package_names_from_receipt(receipt, has_mngr_entry_points)


@pure
def build_base_specifier(base: ToolRequirement) -> str:
    """Build the positional specifier for ``uv tool install <specifier>``.

    Examples: ``"imbue-mngr"``, ``"imbue-mngr>=0.1.0"``.
    """
    if base.specifier is not None:
        return f"{base.name}{base.specifier}"
    return base.name


@pure
def _build_uv_tool_install_command(
    base: ToolRequirement,
    extras: list[ToolRequirement],
) -> tuple[str, ...]:
    """Build a full ``uv tool install`` command from the base + extras.

    Always includes ``--reinstall`` so that ``uv tool`` actually re-resolves.
    When the base was installed from a local directory (``-e``), the command
    uses ``--editable <directory>`` instead of the package name.
    """
    cmd: list[str] = ["uv", "tool", "install"]
    if base.directory is not None:
        cmd.extend(["--editable", base.directory])
    else:
        cmd.append(build_base_specifier(base))
    cmd.append("--reinstall")
    for requirement in extras:
        flag, value = _requirement_to_with_arg(requirement)
        cmd.extend([flag, value])
    return tuple(cmd)


# A file shipped with mngr lives in one of two places depending on how mngr was installed.
# In a wheel it is force-included under the package at ``imbue/mngr/<relative_path>`` (this file's
# parent); in a source/editable checkout the same file lives at ``libs/mngr/<relative_path>``
# (``parents[2]``) because the top-level ``libs/mngr`` tree is not otherwise packaged (see CLAUDE.md).
_SHIPPED_PACKAGE_ROOT: Final = Path(__file__).resolve().parent
_SHIPPED_SOURCE_ROOT: Final = Path(__file__).resolve().parents[2]


def _resolve_shipped_path(relative_path: str) -> Path | None:
    """Resolve a file shipped with mngr: the packaged copy (a wheel) or the source-checkout copy.

    Only one of the two locations exists in any given install -- a wheel force-includes the file
    under ``imbue/mngr``, while a source/editable checkout keeps it under ``libs/mngr`` -- so this
    checks the packaged location first, then the source tree, and returns None if neither has it.
    """
    packaged_path = _SHIPPED_PACKAGE_ROOT / relative_path
    if packaged_path.exists():
        return packaged_path
    source_path = _SHIPPED_SOURCE_ROOT / relative_path
    if source_path.exists():
        return source_path
    return None


@pure
def _append_constraints_arg(command: tuple[str, ...], constraint_path: Path) -> tuple[str, ...]:
    """Append ``--constraints <path>`` to a finished uv command.

    ``uv`` accepts the option after the positional and ``--with`` args, so appending keeps the
    ``@pure`` command builders unaware of install-time constraints.
    """
    return (*command, "--constraints", str(constraint_path))


def _constraints_arg_or_abort(command: tuple[str, ...], constraint_path: Path | None) -> tuple[str, ...]:
    """Append the shipped constraints file to ``command``, or abort if it could not be resolved.

    A missing file is not a per-plugin problem but a sign the mngr installation itself is broken
    (the single, whole-tree constraints file is force-included in the wheel and committed in the
    source tree), so pinning aborts rather than silently resolving unpinned.
    """
    if constraint_path is None:
        raise AbortError(
            f"mngr's bundled {_CONSTRAINTS_FILENAME} could not be found in this installation. "
            "This is a packaging bug -- reinstall mngr (e.g. via the install script or "
            "'uv tool install imbue-mngr') and try again."
        )
    return _append_constraints_arg(command, constraint_path)


def with_shipped_constraints(command: tuple[str, ...]) -> tuple[str, ...]:
    """Append ``--constraints <shipped constraints.txt>`` to a ``uv tool install`` command, aborting if it is missing.

    A single lockfile-derived constraints file -- the whole third-party dependency tree from the
    workspace lock, not a per-plugin file -- ships inside the wheel (force-included at
    ``imbue/mngr/constraints.txt``) and is committed in the source tree, so it is present in every
    real install. It pins that tree to the versions CI tested, so any ``uv tool install`` mngr runs
    (adding a plugin, or re-resolving the surviving tree when removing one) cannot pull an untested
    (potentially breaking) release. Its absence signals a broken mngr installation, so this aborts
    with a clear error rather than silently resolving unpinned.
    """
    return _constraints_arg_or_abort(command, _resolve_shipped_path(_CONSTRAINTS_FILENAME))


@pure
def build_uv_tool_install_add(
    receipt: ToolReceipt,
    new_specifier: str,
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that adds a PyPI dependency.

    Preserves all existing extras and appends the new one.
    """
    all_extras = list(receipt.extras) + [ToolRequirement(name=new_specifier)]
    return _build_uv_tool_install_command(receipt.base, all_extras)


@pure
def build_uv_tool_install_add_path(
    receipt: ToolReceipt,
    local_path: str,
    package_name: str,
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that adds a local editable dependency.

    Preserves all existing extras and appends the new editable one.
    """
    new_requirement = ToolRequirement(name=package_name, editable=local_path)
    all_extras = list(receipt.extras) + [new_requirement]
    return _build_uv_tool_install_command(receipt.base, all_extras)


@pure
def build_uv_tool_install_add_requirements(
    receipt: ToolReceipt,
    new_requirements: list[ToolRequirement],
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that adds multiple dependencies at once.

    Preserves all existing extras and appends the new ones. This avoids
    running ``uv tool install`` multiple times when adding several plugins.
    """
    all_extras = list(receipt.extras) + new_requirements
    return _build_uv_tool_install_command(receipt.base, all_extras)


@pure
def build_uv_tool_install_add_git(
    receipt: ToolReceipt,
    url: str,
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that adds a git dependency.

    The URL should not include a ``git+`` prefix; that is added
    by ``_requirement_to_with_arg`` when converting to ``--with``.
    """
    # We don't know the package name from the URL alone, so we use the
    # URL as the --with argument directly in PEP 508 format.
    git_url = url if url.startswith("git+") else f"git+{url}"
    new_requirement = ToolRequirement(name=git_url)
    all_extras = list(receipt.extras) + [new_requirement]
    return _build_uv_tool_install_command(receipt.base, all_extras)


@pure
def build_uv_tool_install_add_many(
    receipt: ToolReceipt,
    new_specifiers: Sequence[str],
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that adds multiple PyPI dependencies at once.

    Preserves all existing extras and appends all new ones in a single command,
    avoiding the overhead of reinstalling once per plugin.
    """
    all_extras = list(receipt.extras) + [ToolRequirement(name=s) for s in new_specifiers]
    return _build_uv_tool_install_command(receipt.base, all_extras)


@pure
def build_uv_tool_install_remove(
    receipt: ToolReceipt,
    package_name: str,
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that removes a dependency.

    Rebuilds with all extras *except* the one matching ``package_name``.
    """
    filtered = [r for r in receipt.extras if r.name != package_name]
    return _build_uv_tool_install_command(receipt.base, filtered)


@pure
def build_uv_tool_install_remove_multiple(
    receipt: ToolReceipt,
    package_names: set[str],
) -> tuple[str, ...]:
    """Build a ``uv tool install`` command that removes multiple dependencies at once.

    Rebuilds with all extras *except* those whose names are in ``package_names``.
    """
    filtered = [r for r in receipt.extras if r.name not in package_names]
    return _build_uv_tool_install_command(receipt.base, filtered)
