import ast
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import click
import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.testing import MINDS_APP_DIR
from imbue.minds.testing import MINDS_INSTALLS
from imbue.minds.testing import MindsInstall
from imbue.minds.testing import get_mngr_plugin_package_names
from imbue.minds.testing import read_pyproject_dependency_names
from imbue.mngr.cli.plugin_manager import create_plugin_manager
from imbue.mngr.main import cli

_MINDS_SOURCE_DIR: Final[Path] = MINDS_APP_DIR / "imbue" / "minds"

_MNGR_BINARY_NAMES: Final[frozenset[str]] = frozenset({"mngr_binary", "MNGR_BINARY"})

# Argv whose command is a runtime value, keyed by (file, enclosing function), with every command it can run.
_DYNAMIC_COMMANDS_BY_SITE: Final[Mapping[tuple[str, str], frozenset[str]]] = {
    ("desktop_client/agent_creator.py", "run_mngr_provider_prepare"): frozenset({"gcp", "azure"}),
}

# Commands each argv shape must turn up, so a reshaped call site cannot make the scan pass vacuously.
_EXPECTED_BINARY_HEADED_COMMANDS: Final[frozenset[str]] = frozenset({"create", "exec", "pair"})
_EXPECTED_CALLER_COMMANDS: Final[frozenset[str]] = frozenset({"imbue_cloud", "lima"})


class _PluginCommand(FrozenModel):
    """A top-level mngr command registered by a plugin."""

    plugin_name: str = Field(description="The pluggy name of the plugin that registers the command")
    subcommand_names: frozenset[str] | None = Field(description="The command's subcommands; None if not a group")


class _MngrCallSite(FrozenModel):
    """One mngr argv built in minds' production code."""

    command: str = Field(description="The top-level mngr command the argv runs")
    location: str = Field(description="Where the argv is built, as path:line")


class _MngrArgvScan(FrozenModel):
    """Every mngr argv found in minds' production code."""

    binary_headed: tuple[_MngrCallSite, ...] = Field(description="Argv starting with the mngr binary")
    caller: tuple[_MngrCallSite, ...] = Field(description="Argv with no binary in front, run through MngrCaller")
    dynamic_sites: frozenset[tuple[str, str]] = Field(description="(file, function) of argv with a runtime command")


def _iter_plugin_registered_commands() -> Iterator[tuple[str, click.Command]]:
    """Yield (plugin name, command) for every top-level command an installed mngr plugin registers."""
    for hookimpl in create_plugin_manager().hook.register_cli_commands.get_hookimpls():
        registered_commands = hookimpl.function()
        assert registered_commands is None or isinstance(registered_commands, (list, tuple)), (
            f"plugin '{hookimpl.plugin_name}' registered {registered_commands!r}, not a sequence of commands"
        )
        for command in registered_commands or ():
            assert isinstance(command, click.Command), f"plugin '{hookimpl.plugin_name}' registered {command!r}"
            yield hookimpl.plugin_name, command


def _get_plugin_commands() -> dict[str, _PluginCommand]:
    return {
        command.name: _PluginCommand(
            plugin_name=plugin_name,
            subcommand_names=frozenset(command.commands) if isinstance(command, click.Group) else None,
        )
        for plugin_name, command in _iter_plugin_registered_commands()
        if command.name is not None
    }


def _is_production_source(path: Path) -> bool:
    return not (
        path.name.endswith("_test.py") or path.name.startswith("test_") or path.name in ("testing.py", "conftest.py")
    )


def _iter_sequence_literals(node: ast.AST, enclosing_function: str) -> Iterator[tuple[ast.List | ast.Tuple, str]]:
    """Yield every non-empty list or tuple literal under ``node`` with the name of the function it is in."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.List, ast.Tuple)) and child.elts:
            yield child, enclosing_function
        child_function = (
            child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing_function
        )
        yield from _iter_sequence_literals(child, child_function)


def _is_mngr_binary(node: ast.expr) -> bool:
    match node:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name in _MNGR_BINARY_NAMES
        case _:
            return False


def _string_value(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _plugin_command_of_argv(elements: list[ast.expr], plugin_commands: Mapping[str, _PluginCommand]) -> str | None:
    """The plugin command a literal with no binary in front runs, e.g. ``lima`` for ``["lima", "resize", ...]``.

    The element after the command must be one of its subcommands (or an option, for a command that is not a
    group) or a spread, which tells ``["docker", "resize", ...]`` apart from ``["docker", "rm", ...]``.
    """
    command = _string_value(elements[0])
    plugin_command = plugin_commands.get(command) if command is not None else None
    if command is None or plugin_command is None or len(elements) < 2:
        return None
    if isinstance(elements[1], ast.Starred):
        return command
    argument = _string_value(elements[1])
    if argument is None:
        return None
    if plugin_command.subcommand_names is None:
        return command if argument.startswith("-") else None
    return command if argument in plugin_command.subcommand_names else None


def _scan_mngr_argv(plugin_commands: Mapping[str, _PluginCommand]) -> _MngrArgvScan:
    binary_headed: list[_MngrCallSite] = []
    caller: list[_MngrCallSite] = []
    dynamic_sites: set[tuple[str, str]] = set()
    for path in sorted(_MINDS_SOURCE_DIR.rglob("*.py")):
        if not _is_production_source(path):
            continue
        relative_path = path.relative_to(_MINDS_SOURCE_DIR).as_posix()
        for literal, enclosing_function in _iter_sequence_literals(ast.parse(path.read_text()), "<module>"):
            elements = literal.elts
            location = f"{relative_path}:{literal.lineno}"
            if _is_mngr_binary(elements[0]) and len(elements) >= 2:
                command = _string_value(elements[1])
                if command is not None:
                    binary_headed.append(_MngrCallSite(command=command, location=location))
                else:
                    site = (relative_path, enclosing_function)
                    dynamic_sites.add(site)
                    binary_headed.extend(
                        _MngrCallSite(command=dynamic_command, location=location)
                        for dynamic_command in sorted(_DYNAMIC_COMMANDS_BY_SITE.get(site, ()))
                    )
            else:
                plugin_command_name = _plugin_command_of_argv(elements, plugin_commands)
                if plugin_command_name is not None:
                    caller.append(_MngrCallSite(command=plugin_command_name, location=location))
    return _MngrArgvScan(
        binary_headed=tuple(binary_headed), caller=tuple(caller), dynamic_sites=frozenset(dynamic_sites)
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize("minds_install", MINDS_INSTALLS, ids=lambda install: install.test_id)
def test_mngr_commands_minds_runs_are_installed_with_minds(
    minds_install: MindsInstall,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every mngr command minds runs must come from a package this install of minds depends on."""
    # Load every installed mngr plugin, regardless of what local config disables.
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")
    plugin_commands = _get_plugin_commands()
    core_command_names = set(cli.commands) - set(plugin_commands)

    scan = _scan_mngr_argv(plugin_commands)
    assert _EXPECTED_BINARY_HEADED_COMMANDS <= {site.command for site in scan.binary_headed}, (
        f"The scan of {_MINDS_SOURCE_DIR} no longer finds `[mngr_binary, '<command>', ...]` argv for "
        f"{sorted(_EXPECTED_BINARY_HEADED_COMMANDS)} -- the call sites changed shape; teach the scan the new one."
    )
    assert _EXPECTED_CALLER_COMMANDS <= {site.command for site in scan.caller}, (
        f"The scan of {_MINDS_SOURCE_DIR} no longer finds `['<plugin command>', ...]` argv for "
        f"{sorted(_EXPECTED_CALLER_COMMANDS)} -- the call sites changed shape; teach the scan the new one."
    )
    assert scan.dynamic_sites == set(_DYNAMIC_COMMANDS_BY_SITE), (
        "Every mngr argv whose command is a runtime value must be listed in _DYNAMIC_COMMANDS_BY_SITE with the "
        f"commands it can run. Found: {sorted(scan.dynamic_sites)}; listed: {sorted(_DYNAMIC_COMMANDS_BY_SITE)}."
    )

    locations_by_command: dict[str, list[str]] = {}
    for site in (*scan.binary_headed, *scan.caller):
        locations_by_command.setdefault(site.command, []).append(site.location)

    package_by_plugin_name = get_mngr_plugin_package_names()
    installed_package_names = read_pyproject_dependency_names(minds_install.pyproject_path)
    problems: list[str] = []
    for command, locations in sorted(locations_by_command.items()):
        plugin_command = plugin_commands.get(command)
        if plugin_command is None:
            if command not in core_command_names:
                problems.append(
                    f"`mngr {command}` (built at {locations}) is not a command of mngr or of any installed plugin."
                )
            continue
        package_name = package_by_plugin_name.get(plugin_command.plugin_name)
        if package_name is None:
            problems.append(
                f"`mngr {command}` is registered by plugin '{plugin_command.plugin_name}', which has no "
                "setuptools entry point in the 'mngr' group -- cannot determine its package."
            )
        elif package_name.lower() in installed_package_names:
            pass
        else:
            problems.append(
                f"`mngr {command}` (built at {locations}) is provided by package '{package_name}', which is NOT a "
                f'dependency of {minds_install.description}, so it fails there with "No such command '
                f"'{command}'\". Fix: {minds_install.fix_instructions}"
            )
    assert not problems, f"minds runs mngr commands {minds_install.description} does not have:\n- " + "\n- ".join(
        problems
    )
