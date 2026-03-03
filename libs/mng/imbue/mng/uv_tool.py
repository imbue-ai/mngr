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
    editable: str | None = Field(default=None, description="Local editable path")
    git: str | None = Field(default=None, description="Git URL")


class ToolReceipt(FrozenModel):
    """Parsed uv-receipt.toml split into the base mng requirement and extras."""

    base: ToolRequirement = Field(description="The base mng requirement (positional arg to uv tool install)")
    extras: list[ToolRequirement] = Field(description="Additional --with / --with-editable dependencies")


@pure
def _requirement_to_with_arg(req: ToolRequirement) -> tuple[str, str]:
    """Convert a requirement to a (flag, value) pair for ``uv tool install``.

    Returns either ``("--with", specifier)`` or ``("--with-editable", path)``.
    """
    if req.editable is not None:
        return ("--with-editable", req.editable)

    if req.git is not None:
        return ("--with", f"{req.name} @ git+{req.git}")

    if req.specifier is not None:
        return ("--with", f"{req.name}{req.specifier}")

    return ("--with", req.name)


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
            "mng is not installed via 'uv tool install'. "
            "Plugin management only works with the uv-tool-installed version of mng. "
            "If you manage your own virtualenv, install plugins directly with pip or uv."
        )
    return receipt


def read_receipt(receipt_path: Path) -> ToolReceipt:
    """Parse a uv-receipt.toml into a base requirement and extras."""
    with receipt_path.open("rb") as f:
        data = tomllib.load(f)

    raw_reqs: list[dict[str, Any]] = data.get("tool", {}).get("requirements", [])
    requirements = [ToolRequirement(**r) for r in raw_reqs]

    base = ToolRequirement(name="mng")
    for req in requirements:
        if req.name == "mng":
            base = req
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
    """
    cmd: list[str] = ["uv", "tool", "install", build_base_specifier(base), "--reinstall"]
    for req in extras:
        flag, value = _requirement_to_with_arg(req)
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
    new_req = ToolRequirement(name=package_name, editable=local_path)
    all_extras = list(receipt.extras) + [new_req]
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
    new_req = ToolRequirement(name=git_url)
    all_extras = list(receipt.extras) + [new_req]
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
