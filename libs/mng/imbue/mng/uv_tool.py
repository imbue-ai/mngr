"""Read and manipulate the ``uv tool`` receipt for mng.

When mng is installed via ``uv tool install mng``, uv stores a receipt
at ``<venv>/uv-receipt.toml`` that records the base package and any
extra ``--with`` dependencies.  This module reads that receipt and
builds ``uv tool install`` commands that preserve existing dependencies
while adding or removing plugins.
"""

import sys
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mng.cli.output_helpers import AbortError

_RECEIPT_FILENAME: Final[str] = "uv-receipt.toml"


class ToolRequirement(FrozenModel):
    """A single requirement entry from the uv-receipt.toml file."""

    name: str = Field(description="Package name")
    specifier: str | None = Field(default=None, description="Version specifier (e.g. '>=1.0')")
    editable: str | None = Field(default=None, description="Local editable path (from --with-editable)")
    directory: str | None = Field(default=None, description="Local directory path (from -e / --editable on the base)")
    git: str | None = Field(default=None, description="Git URL")


class ToolReceipt(FrozenModel):
    """Parsed uv-receipt.toml split into the base mng requirement and extras."""

    base: ToolRequirement = Field(description="The base mng requirement (positional arg to uv tool install)")
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

    The receipt lives at ``sys.prefix / uv-receipt.toml`` when mng was
    installed via ``uv tool install``.
    """
    receipt = Path(sys.prefix) / _RECEIPT_FILENAME
    if receipt.is_file():
        return receipt
    return None


def require_uv_tool_receipt() -> Path:
    """Return the receipt path or raise if mng was not installed via ``uv tool``.

    Call this at the top of any command that modifies the tool's dependencies.
    """
    receipt = get_receipt_path()
    if receipt is None:
        raise AbortError(
            "The current mng instance is not installed via 'uv tool install'. "
            "To add or remove plugins, simply use whatever commands you use to manage Python dependencies."
        )
    return receipt


def read_receipt(receipt_path: Path) -> ToolReceipt:
    """Parse a uv-receipt.toml into a base requirement and extras."""
    with receipt_path.open("rb") as f:
        data = tomllib.load(f)

    raw_reqs: list[dict[str, Any]] = data.get("tool", {}).get("requirements", [])
    requirements = [ToolRequirement(**r) for r in raw_reqs]

    base = ToolRequirement(name="mng")
    for requirement in requirements:
        if requirement.name == "mng":
            base = requirement
            break

    extras = [r for r in requirements if r.name != "mng"]

    return ToolReceipt(base=base, extras=extras)


@pure
def build_base_specifier(base: ToolRequirement) -> str:
    """Build the positional specifier for ``uv tool install <specifier>``.

    Examples: ``"mng"``, ``"mng>=0.1.0"``.
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
