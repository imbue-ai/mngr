"""Tests for click utilities."""

import click

from imbue.mngr.utils.click_utils import detect_alias_to_canonical
from imbue.mngr.utils.click_utils import detect_aliases_by_command


def _make_group_with_aliases() -> click.Group:
    """Create a click group with commands and aliases for testing."""
    group = click.Group()

    @click.command(name="create")
    def create_cmd() -> None:
        pass

    @click.command(name="list")
    def list_cmd() -> None:
        pass

    @click.command(name="destroy")
    def destroy_cmd() -> None:
        pass

    # Register commands under their canonical names
    group.add_command(create_cmd)
    group.add_command(list_cmd)
    group.add_command(destroy_cmd)

    # Register aliases (different registered name than cmd.name)
    group.add_command(create_cmd, name="c")
    group.add_command(list_cmd, name="ls")

    return group


# =============================================================================
# detect_aliases_by_command Tests
# =============================================================================


def test_detect_aliases_by_command_groups_aliases() -> None:
    """detect_aliases_by_command should group aliases by canonical name."""
    group = _make_group_with_aliases()
    result = detect_aliases_by_command(group)

    assert "create" in result
    assert "c" in result["create"]
    assert "list" in result
    assert "ls" in result["list"]
    # "destroy" has no aliases, so it should not appear
    assert "destroy" not in result


def test_detect_aliases_by_command_empty_for_no_aliases() -> None:
    """detect_aliases_by_command should return empty dict when no aliases exist."""
    group = click.Group()

    @click.command(name="simple")
    def simple_cmd() -> None:
        pass

    group.add_command(simple_cmd)

    result = detect_aliases_by_command(group)
    assert result == {}


def test_detect_aliases_by_command_empty_group() -> None:
    """detect_aliases_by_command should return empty dict for an empty group."""
    group = click.Group()
    result = detect_aliases_by_command(group)
    assert result == {}


# =============================================================================
# detect_alias_to_canonical Tests
# =============================================================================


def test_detect_alias_to_canonical_maps_aliases() -> None:
    """detect_alias_to_canonical should map each alias to its canonical name."""
    group = _make_group_with_aliases()
    result = detect_alias_to_canonical(group)

    assert result["c"] == "create"
    assert result["ls"] == "list"
    # Canonical names should not appear as keys
    assert "create" not in result
    assert "list" not in result
    assert "destroy" not in result


def test_detect_alias_to_canonical_empty_for_no_aliases() -> None:
    """detect_alias_to_canonical should return empty dict when no aliases exist."""
    group = click.Group()

    @click.command(name="only")
    def only_cmd() -> None:
        pass

    group.add_command(only_cmd)

    result = detect_alias_to_canonical(group)
    assert result == {}
