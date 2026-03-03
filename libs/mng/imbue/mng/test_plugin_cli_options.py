"""Tests for plugin CLI options hook."""

from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any
from typing import Callable

import click
import pluggy
from click.testing import CliRunner
from click_option_group import GroupedOption
from click_option_group import OptionGroup
from click_option_group import optgroup

import imbue.mng.main
from imbue.mng import hookimpl
from imbue.mng.cli.common_opts import TCommand
from imbue.mng.cli.common_opts import _apply_plugin_option_overrides
from imbue.mng.main import apply_plugin_cli_options
from imbue.mng.main import reset_plugin_manager
from imbue.mng.plugins import hookspecs
from imbue.mng.plugins.hookspecs import OptionStackItem


@contextmanager
def _plugin_manager_with_plugins(
    plugins: Sequence[Any],
) -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with registered plugins, restoring state on exit."""
    reset_plugin_manager()
    pm = pluggy.PluginManager("mng")
    pm.add_hookspecs(hookspecs)
    for plugin in plugins:
        pm.register(plugin)

    old_pm = imbue.mng.main._plugin_manager_container["pm"]
    imbue.mng.main._plugin_manager_container["pm"] = pm

    try:
        yield pm
    finally:
        imbue.mng.main._plugin_manager_container["pm"] = old_pm


@contextmanager
def _plugin_manager_with_plugin(
    plugin: Any,
) -> Generator[pluggy.PluginManager, None, None]:
    """Create a plugin manager with a single plugin, restoring state on exit."""
    with _plugin_manager_with_plugins([plugin]) as pm:
        yield pm


class _PluginWithStringOption:
    """A test plugin that adds a string option to the 'create' command."""

    @hookimpl
    def register_cli_options(self, command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
        if command_name == "create":
            return {
                "Test Plugin Options": [
                    OptionStackItem(
                        param_decls=("--test-plugin-option",),
                        type=str,
                        default=None,
                        help="Test option from plugin",
                    ),
                ]
            }
        return None


class _PluginWithFlagOption:
    """A test plugin that adds a flag option to the 'connect' command."""

    @hookimpl
    def register_cli_options(self, command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
        if command_name == "connect":
            return {
                "Test Flag Options": [
                    OptionStackItem(
                        param_decls=("--test-flag", "-tf"),
                        is_flag=True,
                        default=False,
                        help="Test flag from plugin",
                    ),
                ]
            }
        return None


class _PluginWithMultipleOptions:
    """A test plugin that adds multiple options to multiple commands."""

    @hookimpl
    def register_cli_options(self, command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
        if command_name == "create":
            return {
                "Multi Options": [
                    OptionStackItem(
                        param_decls=("--multi-opt-1",),
                        type=str,
                        default="default1",
                        help="First option",
                    ),
                    OptionStackItem(
                        param_decls=("--multi-opt-2",),
                        type=int,
                        default=42,
                        help="Second option",
                    ),
                ]
            }
        elif command_name == "gc":
            return {
                "GC Plugin Options": [
                    OptionStackItem(
                        param_decls=("--gc-plugin-opt",),
                        type=str,
                        default=None,
                        help="GC command option",
                    ),
                ]
            }
        else:
            return None


def test_apply_plugin_cli_options_adds_string_option() -> None:
    """Test that apply_plugin_cli_options adds a string option from a plugin."""
    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @click.command()
        def test_cmd() -> None:
            pass

        apply_plugin_cli_options(test_cmd, "create")

        param_names = [p.name for p in test_cmd.params]
        assert "test_plugin_option" in param_names

        option = next(p for p in test_cmd.params if p.name == "test_plugin_option")
        assert isinstance(option, GroupedOption)
        assert option.default is None
        assert option.help == "Test option from plugin"
        assert option.group.name == "Test Plugin Options"


def test_apply_plugin_cli_options_adds_flag_option() -> None:
    """Test that apply_plugin_cli_options adds a flag option from a plugin."""
    with _plugin_manager_with_plugin(_PluginWithFlagOption()):

        @click.command()
        def test_cmd() -> None:
            pass

        apply_plugin_cli_options(test_cmd, "connect")

        param_names = [p.name for p in test_cmd.params]
        assert "test_flag" in param_names

        option = next(p for p in test_cmd.params if p.name == "test_flag")
        assert isinstance(option, GroupedOption)
        assert option.default is False
        assert option.is_flag is True
        assert option.group.name == "Test Flag Options"


def test_apply_plugin_cli_options_adds_multiple_options() -> None:
    """Test that apply_plugin_cli_options adds multiple options from a plugin."""
    with _plugin_manager_with_plugin(_PluginWithMultipleOptions()):

        @click.command()
        def test_cmd() -> None:
            pass

        apply_plugin_cli_options(test_cmd, "create")

        param_names = [p.name for p in test_cmd.params]
        assert "multi_opt_1" in param_names
        assert "multi_opt_2" in param_names

        opt1 = next(p for p in test_cmd.params if p.name == "multi_opt_1")
        opt2 = next(p for p in test_cmd.params if p.name == "multi_opt_2")
        assert opt1.default == "default1"
        assert opt2.default == 42

        assert isinstance(opt1, GroupedOption)
        assert isinstance(opt2, GroupedOption)
        assert opt1.group is opt2.group
        assert opt1.group.name == "Multi Options"


def test_apply_plugin_cli_options_no_options_for_unknown_command() -> None:
    """Test that apply_plugin_cli_options does nothing for unknown commands."""
    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @click.command()
        def test_cmd() -> None:
            pass

        initial_param_count = len(test_cmd.params)

        apply_plugin_cli_options(test_cmd, "unknown_command")

        assert len(test_cmd.params) == initial_param_count


def test_with_plugin_cli_options_decorator() -> None:
    """Test the with_plugin_cli_options decorator."""
    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @with_plugin_cli_options("create")
        @click.command()
        def decorated_cmd() -> None:
            pass

        param_names = [p.name for p in decorated_cmd.params]
        assert "test_plugin_option" in param_names


def test_plugin_options_are_parsed_correctly() -> None:
    """Test that plugin options are correctly parsed when the command is invoked."""
    captured_value: str | None = None

    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @click.command()
        @click.pass_context
        def test_cmd(ctx: click.Context, **kwargs: Any) -> None:
            nonlocal captured_value
            captured_value = kwargs.get("test_plugin_option")

        apply_plugin_cli_options(test_cmd, "create")

        runner = CliRunner()
        result = runner.invoke(test_cmd, ["--test-plugin-option", "hello"])

        assert result.exit_code == 0
        assert captured_value == "hello"


def test_plugin_flag_option_default_false() -> None:
    """Test that plugin flag options default to False when not specified."""
    captured_value: bool | None = None

    with _plugin_manager_with_plugin(_PluginWithFlagOption()):

        @click.command()
        @click.pass_context
        def test_cmd(ctx: click.Context, **kwargs: Any) -> None:
            nonlocal captured_value
            captured_value = kwargs.get("test_flag")

        apply_plugin_cli_options(test_cmd, "connect")

        runner = CliRunner()
        result = runner.invoke(test_cmd, [])

        assert result.exit_code == 0
        assert captured_value is False


def test_plugin_flag_option_set_to_true() -> None:
    """Test that plugin flag options are True when specified."""
    captured_value: bool | None = None

    with _plugin_manager_with_plugin(_PluginWithFlagOption()):

        @click.command()
        @click.pass_context
        def test_cmd(ctx: click.Context, **kwargs: Any) -> None:
            nonlocal captured_value
            captured_value = kwargs.get("test_flag")

        apply_plugin_cli_options(test_cmd, "connect")

        runner = CliRunner()
        result = runner.invoke(test_cmd, ["--test-flag"])

        assert result.exit_code == 0
        assert captured_value is True


def test_multiple_plugins_can_add_options() -> None:
    """Test that multiple plugins can add options to the same command in different groups."""
    plugins = [_PluginWithStringOption(), _PluginWithMultipleOptions()]
    with _plugin_manager_with_plugins(plugins):

        @click.command()
        def test_cmd() -> None:
            pass

        apply_plugin_cli_options(test_cmd, "create")

        param_names = [p.name for p in test_cmd.params]
        assert "test_plugin_option" in param_names
        assert "multi_opt_1" in param_names
        assert "multi_opt_2" in param_names

        test_opt = next(p for p in test_cmd.params if p.name == "test_plugin_option")
        multi_opt_1 = next(p for p in test_cmd.params if p.name == "multi_opt_1")

        assert isinstance(test_opt, GroupedOption)
        assert isinstance(multi_opt_1, GroupedOption)
        assert test_opt.group.name == "Test Plugin Options"
        assert multi_opt_1.group.name == "Multi Options"
        assert test_opt.group is not multi_opt_1.group


def test_apply_plugin_cli_options_with_no_name() -> None:
    """Test that apply_plugin_cli_options handles commands with no name."""
    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @click.command(name=None)
        def test_cmd() -> None:
            pass

        initial_param_count = len(test_cmd.params)

        result = apply_plugin_cli_options(test_cmd, None)

        assert result is test_cmd
        assert len(test_cmd.params) == initial_param_count


def test_option_stack_item_to_click_option() -> None:
    """Test that OptionStackItem.to_click_option creates the correct click.Option."""
    item = OptionStackItem(
        param_decls=("--test-opt", "-t"),
        type=str,
        default="default_value",
        help="Test help text",
        is_flag=False,
        multiple=False,
        required=False,
        envvar="TEST_ENV_VAR",
    )

    option = item.to_click_option()

    assert option.opts == ["--test-opt", "-t"]
    assert option.default == "default_value"
    assert option.help == "Test help text"
    assert option.is_flag is False
    assert option.multiple is False
    assert option.required is False
    assert option.envvar == "TEST_ENV_VAR"


def test_option_stack_item_with_defaults() -> None:
    """Test that OptionStackItem uses sensible defaults."""
    item = OptionStackItem(param_decls=("--minimal-opt",))

    assert item.type is str
    assert item.default is None
    assert item.help is None
    assert item.is_flag is False
    assert item.multiple is False
    assert item.required is False
    assert item.envvar is None

    # Should still create a valid click option
    option = item.to_click_option()
    assert option.opts == ["--minimal-opt"]


def test_option_stack_item_to_grouped_option() -> None:
    """Test that OptionStackItem.to_click_option with a group returns GroupedOption."""
    item = OptionStackItem(
        param_decls=("--grouped-opt",),
        type=str,
        default="value",
        help="A grouped option",
    )

    group = OptionGroup("My Group")
    option = item.to_click_option(group=group)

    assert isinstance(option, GroupedOption)
    assert option.group is group
    assert option.group.name == "My Group"


def test_plugin_creates_title_fake_option_for_new_group() -> None:
    """Test that applying plugin options creates a title fake option for the group."""
    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @click.command()
        def test_cmd() -> None:
            pass

        apply_plugin_cli_options(test_cmd, "create")

        fake_opts = [p for p in test_cmd.params if p.name and p.name.startswith("fake_")]
        assert len(fake_opts) == 1

        fake_opt = fake_opts[0]
        assert isinstance(fake_opt, click.Option)
        assert fake_opt.hidden is True
        assert fake_opt.expose_value is False


class _PluginAddingToExistingGroup:
    """A test plugin that adds options to an existing 'Behavior' group."""

    @hookimpl
    def register_cli_options(self, command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
        if command_name == "test_existing_group":
            return {
                "Behavior": [
                    OptionStackItem(
                        param_decls=("--plugin-behavior-opt",),
                        type=str,
                        default=None,
                        help="Plugin option added to Behavior group",
                    ),
                ]
            }
        return None


def test_plugin_adds_options_to_existing_group() -> None:
    """Test that a plugin can add options to an existing option group."""
    with _plugin_manager_with_plugin(_PluginAddingToExistingGroup()):

        @click.command()
        @optgroup.group("Behavior")
        @optgroup.option("--existing-opt", help="Existing option")
        def test_cmd(**kwargs: Any) -> None:
            pass

        apply_plugin_cli_options(test_cmd, "test_existing_group")

        existing_opt = next(p for p in test_cmd.params if p.name == "existing_opt")
        plugin_opt = next(p for p in test_cmd.params if p.name == "plugin_behavior_opt")

        assert isinstance(existing_opt, GroupedOption)
        assert isinstance(plugin_opt, GroupedOption)
        assert existing_opt.group is plugin_opt.group
        assert existing_opt.group.name == "Behavior"


class _PluginA:
    """First plugin adding options to a shared group."""

    @hookimpl
    def register_cli_options(self, command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
        if command_name == "shared_group_test":
            return {
                "Shared Group": [
                    OptionStackItem(
                        param_decls=("--opt-a",),
                        type=str,
                        default=None,
                        help="Option A",
                    ),
                ]
            }
        return None


class _PluginB:
    """Second plugin adding options to the same shared group."""

    @hookimpl
    def register_cli_options(self, command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
        if command_name == "shared_group_test":
            return {
                "Shared Group": [
                    OptionStackItem(
                        param_decls=("--opt-b",),
                        type=str,
                        default=None,
                        help="Option B",
                    ),
                ]
            }
        return None


def test_multiple_plugins_can_add_to_same_new_group() -> None:
    """Test that multiple plugins can add options to the same new group."""
    plugins = [_PluginA(), _PluginB()]
    with _plugin_manager_with_plugins(plugins):

        @click.command()
        def test_cmd() -> None:
            pass

        apply_plugin_cli_options(test_cmd, "shared_group_test")

        opt_a = next(p for p in test_cmd.params if p.name == "opt_a")
        opt_b = next(p for p in test_cmd.params if p.name == "opt_b")

        assert isinstance(opt_a, GroupedOption)
        assert isinstance(opt_b, GroupedOption)
        assert opt_a.group is opt_b.group
        assert opt_a.group.name == "Shared Group"


def test_plugin_options_show_in_help_with_group_header() -> None:
    """Test that plugin options appear in help output under their group header."""
    with _plugin_manager_with_plugin(_PluginWithStringOption()):

        @click.command()
        def test_cmd(**kwargs: Any) -> None:
            pass

        apply_plugin_cli_options(test_cmd, "create")

        runner = CliRunner()
        result = runner.invoke(test_cmd, ["--help"])

        assert result.exit_code == 0
        assert "Test Plugin Options" in result.output
        assert "--test-plugin-option" in result.output


# =============================================================================
# Tests for override_command_options hook
# =============================================================================


class _PluginOverridingOption:
    """A test plugin that overrides a parameter value."""

    @hookimpl
    def override_command_options(
        self,
        command_name: str,
        command_class: type,
        params: dict[str, Any],
    ) -> None:
        if command_name == "test_override":
            params["my_option"] = "overridden_value"


class _PluginOverrideChainA:
    """First plugin in a chain that modifies options."""

    @hookimpl
    def override_command_options(
        self,
        command_name: str,
        command_class: type,
        params: dict[str, Any],
    ) -> None:
        if command_name == "test_chain":
            # Append to a list to track order
            params["chain_log"] = params.get("chain_log", []) + ["A"]


class _PluginOverrideChainB:
    """Second plugin in a chain that modifies options."""

    @hookimpl
    def override_command_options(
        self,
        command_name: str,
        command_class: type,
        params: dict[str, Any],
    ) -> None:
        if command_name == "test_chain":
            # Append to a list to track order
            params["chain_log"] = params.get("chain_log", []) + ["B"]


class _PluginUsingCommandClass:
    """A test plugin that uses the command_class for validation."""

    @hookimpl
    def override_command_options(
        self,
        command_name: str,
        command_class: type,
        params: dict[str, Any],
    ) -> None:
        if command_name == "test_validation":
            params["my_option"] = "validated_value"
            # Plugins can optionally validate by constructing the options object
            # (In real usage, this would catch invalid values early)
            params["validated_class_name"] = command_class.__name__


class _DummyCommandClass:
    """A simple class to use as a command_class in tests."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_override_command_options_modifies_params_in_place() -> None:
    """Test that override_command_options modifies params dict in place."""
    with _plugin_manager_with_plugin(_PluginOverridingOption()) as pm:
        params = {"my_option": "original_value", "other_option": "unchanged"}

        pm.hook.override_command_options(
            command_name="test_override",
            command_class=_DummyCommandClass,
            params=params,
        )

        assert params["my_option"] == "overridden_value"
        assert params["other_option"] == "unchanged"


def test_override_command_options_only_applies_to_matching_command() -> None:
    """Test that override_command_options only applies to the specified command."""
    with _plugin_manager_with_plugin(_PluginOverridingOption()) as pm:
        params = {"my_option": "original_value"}

        pm.hook.override_command_options(
            command_name="other_command",
            command_class=_DummyCommandClass,
            params=params,
        )

        assert params["my_option"] == "original_value"


def test_override_command_options_chains_multiple_plugins() -> None:
    """Test that multiple plugins can chain their modifications."""
    plugins = [_PluginOverrideChainA(), _PluginOverrideChainB()]
    with _plugin_manager_with_plugins(plugins) as pm:
        params: dict[str, Any] = {}

        pm.hook.override_command_options(
            command_name="test_chain",
            command_class=_DummyCommandClass,
            params=params,
        )

        assert "chain_log" in params
        assert "A" in params["chain_log"]
        assert "B" in params["chain_log"]


def test_override_command_options_receives_command_class() -> None:
    """Test that plugins receive the command_class and can use it."""
    with _plugin_manager_with_plugin(_PluginUsingCommandClass()) as pm:
        params: dict[str, Any] = {}

        pm.hook.override_command_options(
            command_name="test_validation",
            command_class=_DummyCommandClass,
            params=params,
        )

        assert params["my_option"] == "validated_value"
        assert params["validated_class_name"] == "_DummyCommandClass"


def test_apply_plugin_option_overrides_function() -> None:
    """Test the _apply_plugin_option_overrides helper function."""
    with _plugin_manager_with_plugin(_PluginOverridingOption()) as pm:
        params = {"my_option": "original_value", "other_option": "unchanged"}

        _apply_plugin_option_overrides(pm, "test_override", _DummyCommandClass, params)

        assert params["my_option"] == "overridden_value"
        assert params["other_option"] == "unchanged"


def with_plugin_cli_options(command_name: str) -> Callable[[TCommand], TCommand]:
    """Decorator to apply plugin-registered CLI options to a click command."""
    return lambda cmd: apply_plugin_cli_options(cmd, command_name=command_name)
