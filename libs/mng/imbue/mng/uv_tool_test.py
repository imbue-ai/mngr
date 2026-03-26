from pathlib import Path

import pytest

from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.uv_tool import ToolReceipt
from imbue.mng.uv_tool import ToolRequirement
from imbue.mng.uv_tool import _build_uv_tool_install_command
from imbue.mng.uv_tool import _requirement_to_with_arg
from imbue.mng.uv_tool import build_base_specifier
from imbue.mng.uv_tool import build_uv_tool_install_add
from imbue.mng.uv_tool import build_uv_tool_install_add_git
from imbue.mng.uv_tool import build_uv_tool_install_add_many
from imbue.mng.uv_tool import build_uv_tool_install_add_path
from imbue.mng.uv_tool import build_uv_tool_install_add_requirements
from imbue.mng.uv_tool import build_uv_tool_install_remove
from imbue.mng.uv_tool import build_uv_tool_install_remove_multiple
from imbue.mng.uv_tool import get_receipt_path
from imbue.mng.uv_tool import read_receipt
from imbue.mng.uv_tool import require_uv_tool_receipt

# =============================================================================
# Tests for ToolRequirement
# =============================================================================


def test_tool_requirement_minimal() -> None:
    """ToolRequirement should create with just a name."""
    requirement = ToolRequirement(name="mng")
    assert requirement.name == "mng"
    assert requirement.specifier is None
    assert requirement.editable is None
    assert requirement.git is None


def test_tool_requirement_with_specifier() -> None:
    """ToolRequirement should store version specifiers."""
    requirement = ToolRequirement(name="mng", specifier=">=0.1.0")
    assert requirement.specifier == ">=0.1.0"


def test_tool_requirement_with_editable() -> None:
    """ToolRequirement should store editable paths."""
    requirement = ToolRequirement(name="my-plugin", editable="/path/to/plugin")
    assert requirement.editable == "/path/to/plugin"


def test_tool_requirement_with_git() -> None:
    """ToolRequirement should store git URLs."""
    requirement = ToolRequirement(name="my-plugin", git="https://github.com/user/repo.git")
    assert requirement.git == "https://github.com/user/repo.git"


# =============================================================================
# Tests for _requirement_to_with_arg
# =============================================================================


def test_requirement_to_with_arg_plain_name() -> None:
    """Plain name should produce --with name."""
    requirement = ToolRequirement(name="mng-opencode")
    assert _requirement_to_with_arg(requirement) == ("--with", "mng-opencode")


def test_requirement_to_with_arg_with_specifier() -> None:
    """Name with specifier should produce --with name+specifier."""
    requirement = ToolRequirement(name="mng-opencode", specifier=">=1.0")
    assert _requirement_to_with_arg(requirement) == ("--with", "mng-opencode>=1.0")


def test_requirement_to_with_arg_editable() -> None:
    """Editable should produce --with-editable path."""
    requirement = ToolRequirement(name="my-plugin", editable="/path/to/plugin")
    assert _requirement_to_with_arg(requirement) == ("--with-editable", "/path/to/plugin")


def test_requirement_to_with_arg_directory() -> None:
    """Directory should produce --with-editable path."""
    requirement = ToolRequirement(name="my-plugin", directory="/path/to/plugin")
    assert _requirement_to_with_arg(requirement) == ("--with-editable", "/path/to/plugin")


def test_requirement_to_with_arg_git() -> None:
    """Git should produce --with 'name @ git+url'."""
    requirement = ToolRequirement(name="my-plugin", git="https://github.com/user/repo.git")
    assert _requirement_to_with_arg(requirement) == ("--with", "my-plugin @ git+https://github.com/user/repo.git")


# =============================================================================
# Tests for get_receipt_path
# =============================================================================


def test_get_receipt_path_returns_none_in_dev_mode() -> None:
    """get_receipt_path should return None when not running from a uv tool venv."""
    # In tests, sys.prefix is the workspace venv which has no uv-receipt.toml
    assert get_receipt_path() is None


# =============================================================================
# Tests for require_uv_tool_receipt
# =============================================================================


def test_require_uv_tool_receipt_raises_in_dev_mode() -> None:
    """require_uv_tool_receipt should raise AbortError outside a uv tool venv."""
    with pytest.raises(AbortError, match="not installed via 'uv tool install'"):
        require_uv_tool_receipt()


# =============================================================================
# Tests for read_receipt
# =============================================================================


def test_read_receipt_minimal(tmp_path: Path) -> None:
    """read_receipt should parse a minimal receipt into base + empty extras."""
    receipt_path = tmp_path / "uv-receipt.toml"
    receipt_path.write_text('[tool]\nrequirements = [{ name = "mng" }]\n')

    receipt = read_receipt(receipt_path)
    assert receipt.base.name == "mng"
    assert receipt.extras == []


def test_read_receipt_with_extras(tmp_path: Path) -> None:
    """read_receipt should split mng from extras."""
    receipt_path = tmp_path / "uv-receipt.toml"
    receipt_path.write_text(
        "[tool]\nrequirements = [\n"
        '  { name = "mng" },\n'
        '  { name = "coolname" },\n'
        '  { name = "mng-opencode", editable = "/path/to/opencode" },\n'
        "]\n"
    )

    receipt = read_receipt(receipt_path)
    assert receipt.base.name == "mng"
    assert len(receipt.extras) == 2
    assert receipt.extras[0].name == "coolname"
    assert receipt.extras[1].name == "mng-opencode"
    assert receipt.extras[1].editable == "/path/to/opencode"


def test_read_receipt_with_specifier(tmp_path: Path) -> None:
    """read_receipt should preserve version specifiers on the base requirement."""
    receipt_path = tmp_path / "uv-receipt.toml"
    receipt_path.write_text(
        "[tool]\nrequirements = [\n"
        '  { name = "mng", specifier = ">=0.1.0" },\n'
        '  { name = "coolname", specifier = ">=2.0" },\n'
        "]\n"
    )

    receipt = read_receipt(receipt_path)
    assert receipt.base.specifier == ">=0.1.0"
    assert receipt.extras[0].specifier == ">=2.0"


def test_read_receipt_with_directory_base(tmp_path: Path) -> None:
    """read_receipt should parse a directory field on the base requirement."""
    receipt_path = tmp_path / "uv-receipt.toml"
    receipt_path.write_text('[tool]\nrequirements = [{ name = "mng", directory = "/path/to/mng" }]\n')

    receipt = read_receipt(receipt_path)
    assert receipt.base.directory == "/path/to/mng"


def test_read_receipt_with_git(tmp_path: Path) -> None:
    """read_receipt should parse git URLs in extras."""
    receipt_path = tmp_path / "uv-receipt.toml"
    receipt_path.write_text(
        "[tool]\nrequirements = [\n"
        '  { name = "mng" },\n'
        '  { name = "mng-opencode", git = "https://github.com/imbue-ai/mng.git" },\n'
        "]\n"
    )

    receipt = read_receipt(receipt_path)
    assert receipt.extras[0].git == "https://github.com/imbue-ai/mng.git"


def test_read_receipt_fallback_when_mng_missing(tmp_path: Path) -> None:
    """read_receipt should fall back to a plain mng base if not in requirements."""
    receipt_path = tmp_path / "uv-receipt.toml"
    receipt_path.write_text('[tool]\nrequirements = [{ name = "something-else" }]\n')

    receipt = read_receipt(receipt_path)
    assert receipt.base.name == "mng"
    assert receipt.base.specifier is None
    assert len(receipt.extras) == 1
    assert receipt.extras[0].name == "something-else"


# =============================================================================
# Tests for build_base_specifier
# =============================================================================


def test_build_base_specifier_plain() -> None:
    """build_base_specifier should return just the name."""
    assert build_base_specifier(ToolRequirement(name="mng")) == "mng"


def test_build_base_specifier_with_version() -> None:
    """build_base_specifier should include the version specifier."""
    assert build_base_specifier(ToolRequirement(name="mng", specifier=">=0.1.0")) == "mng>=0.1.0"


# =============================================================================
# Tests for _build_uv_tool_install_command
# =============================================================================


def test_build_uv_tool_install_command_no_extras() -> None:
    """_build_uv_tool_install_command with no extras should produce minimal command."""
    base = ToolRequirement(name="mng")
    cmd = _build_uv_tool_install_command(base, [])
    assert cmd == ("uv", "tool", "install", "mng", "--reinstall")


def test_build_uv_tool_install_command_with_extras() -> None:
    """_build_uv_tool_install_command should include --with for each extra."""
    base = ToolRequirement(name="mng")
    extras = [
        ToolRequirement(name="coolname"),
        ToolRequirement(name="mng-opencode", editable="/path/to/opencode"),
    ]
    cmd = _build_uv_tool_install_command(base, extras)
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "coolname",
        "--with-editable",
        "/path/to/opencode",
    )


def test_build_uv_tool_install_command_editable_base() -> None:
    """_build_uv_tool_install_command with directory base should use --editable."""
    base = ToolRequirement(name="mng", directory="/path/to/mng")
    cmd = _build_uv_tool_install_command(base, [])
    assert cmd == ("uv", "tool", "install", "--editable", "/path/to/mng", "--reinstall")


# =============================================================================
# Tests for build_uv_tool_install_add / add_path / add_git / remove
# =============================================================================


def _make_receipt(
    base_specifier: str | None = None,
    base_directory: str | None = None,
    extras: list[ToolRequirement] | None = None,
) -> ToolReceipt:
    """Create a ToolReceipt for testing."""
    base = ToolRequirement(name="mng", specifier=base_specifier, directory=base_directory)
    return ToolReceipt(base=base, extras=extras or [])


def test_build_uv_tool_install_add_appends_new_dep() -> None:
    """build_uv_tool_install_add should preserve existing extras and append."""
    receipt = _make_receipt(extras=[ToolRequirement(name="coolname")])
    cmd = build_uv_tool_install_add(receipt, "mng-opencode")
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "coolname",
        "--with",
        "mng-opencode",
    )


def test_build_uv_tool_install_add_path() -> None:
    """build_uv_tool_install_add_path should use --with-editable."""
    receipt = _make_receipt()
    cmd = build_uv_tool_install_add_path(receipt, "/path/to/plugin", "my-plugin")
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with-editable",
        "/path/to/plugin",
    )


def test_build_uv_tool_install_add_git() -> None:
    """build_uv_tool_install_add_git should use git+ prefixed URL."""
    receipt = _make_receipt()
    cmd = build_uv_tool_install_add_git(receipt, "https://github.com/user/repo.git")
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "git+https://github.com/user/repo.git",
    )


def test_build_uv_tool_install_remove_filters_dep() -> None:
    """build_uv_tool_install_remove should rebuild without the target dep."""
    receipt = _make_receipt(
        extras=[
            ToolRequirement(name="coolname"),
            ToolRequirement(name="mng-opencode"),
        ]
    )
    cmd = build_uv_tool_install_remove(receipt, "mng-opencode")
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "coolname",
    )


def test_build_uv_tool_install_remove_last_dep() -> None:
    """build_uv_tool_install_remove should work when removing the only extra."""
    receipt = _make_receipt(extras=[ToolRequirement(name="mng-opencode")])
    cmd = build_uv_tool_install_remove(receipt, "mng-opencode")
    assert cmd == ("uv", "tool", "install", "mng", "--reinstall")


# =============================================================================
# Tests for build_uv_tool_install_add_many
# =============================================================================


def test_build_uv_tool_install_add_many_appends_all() -> None:
    """build_uv_tool_install_add_many should add all specifiers in one command."""
    receipt = _make_receipt(extras=[ToolRequirement(name="existing")])
    cmd = build_uv_tool_install_add_many(receipt, ["mng-pair", "mng-tutor"])
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "existing",
        "--with",
        "mng-pair",
        "--with",
        "mng-tutor",
    )


def test_build_uv_tool_install_add_many_empty_list() -> None:
    """build_uv_tool_install_add_many with no new specifiers should preserve extras only."""
    receipt = _make_receipt(extras=[ToolRequirement(name="existing")])
    cmd = build_uv_tool_install_add_many(receipt, [])
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "existing",
    )


def test_build_uv_tool_install_add_many_no_existing_extras() -> None:
    """build_uv_tool_install_add_many should work with no prior extras."""
    receipt = _make_receipt()
    cmd = build_uv_tool_install_add_many(receipt, ["mng-opencode", "mng-pair", "mng-tutor"])
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "mng-opencode",
        "--with",
        "mng-pair",
        "--with",
        "mng-tutor",
    )


# =============================================================================
# Tests for build_uv_tool_install_add_requirements
# =============================================================================


def test_build_uv_tool_install_add_requirements_multiple_paths() -> None:
    """build_uv_tool_install_add_requirements should add multiple editable deps in one command."""
    receipt = _make_receipt()
    new_requirements = [
        ToolRequirement(name="plugin-a", editable="/path/to/a"),
        ToolRequirement(name="plugin-b", editable="/path/to/b"),
    ]
    cmd = build_uv_tool_install_add_requirements(receipt, new_requirements)
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with-editable",
        "/path/to/a",
        "--with-editable",
        "/path/to/b",
    )


def test_build_uv_tool_install_add_requirements_preserves_existing_extras() -> None:
    """build_uv_tool_install_add_requirements should preserve existing extras."""
    receipt = _make_receipt(extras=[ToolRequirement(name="existing-dep")])
    new_requirements = [ToolRequirement(name="new-dep", editable="/path/to/new")]
    cmd = build_uv_tool_install_add_requirements(receipt, new_requirements)
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "existing-dep",
        "--with-editable",
        "/path/to/new",
    )


def test_build_uv_tool_install_add_requirements_empty_list() -> None:
    """build_uv_tool_install_add_requirements with empty list should just reinstall."""
    receipt = _make_receipt()
    cmd = build_uv_tool_install_add_requirements(receipt, [])
    assert cmd == ("uv", "tool", "install", "mng", "--reinstall")


# =============================================================================
# Tests for build_uv_tool_install_remove_multiple
# =============================================================================


def test_build_uv_tool_install_remove_multiple_removes_all() -> None:
    """build_uv_tool_install_remove_multiple should remove all specified packages."""
    receipt = _make_receipt(
        extras=[
            ToolRequirement(name="keep-me"),
            ToolRequirement(name="remove-a"),
            ToolRequirement(name="remove-b"),
        ]
    )
    cmd = build_uv_tool_install_remove_multiple(receipt, {"remove-a", "remove-b"})
    assert cmd == (
        "uv",
        "tool",
        "install",
        "mng",
        "--reinstall",
        "--with",
        "keep-me",
    )


def test_build_uv_tool_install_remove_multiple_all_deps() -> None:
    """build_uv_tool_install_remove_multiple should work when removing all extras."""
    receipt = _make_receipt(
        extras=[
            ToolRequirement(name="dep-a"),
            ToolRequirement(name="dep-b"),
        ]
    )
    cmd = build_uv_tool_install_remove_multiple(receipt, {"dep-a", "dep-b"})
    assert cmd == ("uv", "tool", "install", "mng", "--reinstall")
