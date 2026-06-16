"""Tests for the mngr extras command."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.mngr.cli.completion_install import COMPLETION_SHIM_MARKER
from imbue.mngr.cli.completion_install import get_managed_completion_script_path
from imbue.mngr.cli.extras import _CLAUDE_CODE_PLUGINS
from imbue.mngr.cli.extras import _completion_status
from imbue.mngr.cli.extras import _detect_shell
from imbue.mngr.cli.extras import _generate_completion_script
from imbue.mngr.cli.extras import _get_shell_rc
from imbue.mngr.cli.extras import _install_claude_plugin
from imbue.mngr.cli.extras import _install_completion
from imbue.mngr.cli.extras import _install_default_agent_type
from imbue.mngr.cli.extras import _is_completion_configured
from imbue.mngr.cli.extras import _list_extras_agent_type_choices
from imbue.mngr.cli.extras import _plugins_status
from imbue.mngr.cli.extras import _print_extras_status
from imbue.mngr.cli.extras import _read_current_default_agent_type
from imbue.mngr.cli.extras import extras

# A byte-identical copy of an old self-contained zsh completion function mngr generated
# before the managed-shim model (any baked python path). strip_legacy_completion_block
# removes such a block, so installing migrates it to the shim.
_OLD_SELF_CONTAINED_ZSH_BLOCK = (
    "_mngr_complete() {\n"
    "    local -a completions\n"
    "    (( ! $+commands[mngr] )) && return 1\n"
    '    completions=(${(@f)"$(COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1))'
    ' /some/python -m imbue.mngr.cli.complete)"})\n'
    "    compadd -U -V unsorted -a completions\n"
    "}\n"
    "compdef _mngr_complete mngr"
)


def test_detect_shell_returns_zsh_or_bash() -> None:
    """_detect_shell returns a valid shell type."""
    shell = _detect_shell()
    assert shell in ("zsh", "bash")


def test_detect_shell_returns_zsh_for_zsh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_shell returns 'zsh' when SHELL env is set to zsh."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _detect_shell() == "zsh"


def test_detect_shell_returns_bash_for_bash_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_shell returns 'bash' when SHELL env is set to bash."""
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert _detect_shell() == "bash"


def test_detect_shell_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_shell falls back based on OS when SHELL is unrecognized."""
    monkeypatch.setenv("SHELL", "/bin/fish")
    # On the current platform, verify it returns a valid shell type
    shell = _detect_shell()
    assert shell in ("zsh", "bash")


def test_get_shell_rc_zsh() -> None:
    """_get_shell_rc returns .zshrc for zsh."""
    rc_path = _get_shell_rc("zsh")
    assert rc_path.name == ".zshrc"


def test_get_shell_rc_bash() -> None:
    """_get_shell_rc returns .bashrc for bash."""
    rc_path = _get_shell_rc("bash")
    assert rc_path.name == ".bashrc"


def test_is_completion_configured_false_for_nonexistent_file(tmp_path: Path) -> None:
    """_is_completion_configured returns False for a file that doesn't exist."""
    assert _is_completion_configured(tmp_path / "nonexistent") is False


def test_is_completion_configured_false_for_empty_file(tmp_path: Path) -> None:
    """_is_completion_configured returns False when the RC file has no mngr completion."""
    rc = tmp_path / ".zshrc"
    rc.write_text("# empty rc file\n")
    assert _is_completion_configured(rc) is False


def test_is_completion_configured_true_when_shim_present(tmp_path: Path) -> None:
    """_is_completion_configured returns True when the managed shim marker is present."""
    rc = tmp_path / ".zshrc"
    rc.write_text(f"# some config\n{COMPLETION_SHIM_MARKER}; ...)\n...\n")
    assert _is_completion_configured(rc) is True


def test_is_completion_configured_false_for_old_function_only(tmp_path: Path) -> None:
    """An rc with only the old self-contained function (no shim marker) is treated as not configured.

    This is what lets `mngr extras` install the up-to-date shim over an old install.
    """
    rc = tmp_path / ".zshrc"
    rc.write_text("# some config\n_mngr_complete() { ... }\ncompdef _mngr_complete mngr\n")
    assert _is_completion_configured(rc) is False


def test_generate_completion_script_zsh_is_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_generate_completion_script returns the rc shim (marker + sources the managed file) for zsh."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    script = _generate_completion_script("zsh")
    assert COMPLETION_SHIM_MARKER in script
    assert "completions/mngr.zsh" in script
    # The shim must NOT inline the completion function body.
    assert "compadd" not in script


def test_generate_completion_script_bash_is_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_generate_completion_script returns the rc shim for bash."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    script = _generate_completion_script("bash")
    assert COMPLETION_SHIM_MARKER in script
    assert "completions/mngr.bash" in script
    assert "complete -o default" not in script


def test_completion_status_returns_tuple() -> None:
    """_completion_status returns a 3-tuple."""
    result = _completion_status()
    assert len(result) == 3
    configured, shell_type, rc_path = result
    assert isinstance(configured, bool)
    assert shell_type in ("zsh", "bash")
    assert isinstance(rc_path, Path)


def test_install_completion_auto_writes_shim_and_managed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_install_completion writes the rc shim + managed files when auto=True; idempotent once present."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    rc = tmp_path / ".zshrc"
    rc.write_text("# existing config\n")

    def _status() -> tuple[bool, str, Path]:
        return (COMPLETION_SHIM_MARKER in rc.read_text(), "zsh", rc)

    # First call: not configured yet -> writes the shim and the managed file
    assert _install_completion(auto=True, status_fn=_status) is True
    assert COMPLETION_SHIM_MARKER in rc.read_text()
    assert (tmp_path / "completions" / "mngr.zsh").is_file()

    # The success path prints how to activate completion in the current shell,
    # pointing at the managed completion file (no new shell needed).
    install_out = capsys.readouterr().out
    assert "source" in install_out
    assert str(get_managed_completion_script_path("zsh")) in install_out

    # Second call: now configured -> returns True without appending the shim again
    assert _install_completion(auto=False, status_fn=_status) is True
    assert rc.read_text().count(COMPLETION_SHIM_MARKER) == 1


def test_install_completion_replaces_old_self_contained_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installing migrates an old self-contained completion function to the managed shim.

    The byte-identical old block is removed and the shim is added (so `mngr extras
    completion` cleans up the stale function rather than leaving it orphaned).
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    rc = tmp_path / ".zshrc"
    rc.write_text(f"# config\n{_OLD_SELF_CONTAINED_ZSH_BLOCK}\n")

    def _status() -> tuple[bool, str, Path]:
        return (COMPLETION_SHIM_MARKER in rc.read_text(), "zsh", rc)

    assert _install_completion(auto=True, status_fn=_status) is True

    text = rc.read_text()
    assert COMPLETION_SHIM_MARKER in text
    # The old self-contained function is gone (migrated to the shim).
    assert "compadd -U -V unsorted -a completions" not in text
    assert "# config" in text


def test_install_completion_skips_without_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an interactive terminal, _install_completion skips and returns False.

    The confirm_fn would install if reached, but the is_interactive_fn
    gate fires first.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    rc = tmp_path / ".zshrc"
    rc.write_text("# existing config\n")
    assert (
        _install_completion(
            auto=False,
            status_fn=lambda: (False, "zsh", rc),
            is_interactive_fn=lambda: False,
            confirm_fn=lambda _rc, _replace: True,
        )
        is False
    )
    assert COMPLETION_SHIM_MARKER not in rc.read_text()


def test_install_completion_picker_skip_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the picker confirm returns False, the shim is not added to the rc."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    rc = tmp_path / ".zshrc"
    rc.write_text("# existing config\n")
    assert (
        _install_completion(
            auto=False,
            status_fn=lambda: (False, "zsh", rc),
            is_interactive_fn=lambda: True,
            confirm_fn=lambda _rc, _replace: False,
        )
        is False
    )
    assert COMPLETION_SHIM_MARKER not in rc.read_text()


def test_install_completion_confirm_prompt_signals_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an old block is present, the confirm prompt is told it's a replacement (will_replace=True)."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    rc = tmp_path / ".zshrc"
    rc.write_text(f"# config\n{_OLD_SELF_CONTAINED_ZSH_BLOCK}\n")
    captured: dict[str, bool] = {}

    def _confirm(_rc: Path, will_replace: bool) -> bool:
        captured["will_replace"] = will_replace
        return True

    assert (
        _install_completion(
            auto=False,
            status_fn=lambda: (False, "zsh", rc),
            is_interactive_fn=lambda: True,
            confirm_fn=_confirm,
        )
        is True
    )
    assert captured["will_replace"] is True
    assert COMPLETION_SHIM_MARKER in rc.read_text()
    assert "compadd -U -V unsorted -a completions" not in rc.read_text()


def _all_installed() -> tuple[bool, dict[str, bool]]:
    return True, {plugin.name: True for plugin in _CLAUDE_CODE_PLUGINS}


def _none_installed() -> tuple[bool, dict[str, bool]]:
    return True, {plugin.name: False for plugin in _CLAUDE_CODE_PLUGINS}


def test_install_claude_plugin_returns_false_when_claude_missing() -> None:
    """When claude is not on PATH, _install_claude_plugin returns False."""
    assert _install_claude_plugin(auto=True, status_fn=lambda: (False, {})) is False


def test_install_claude_plugin_returns_true_when_already_installed() -> None:
    """When every plugin is already installed, _install_claude_plugin short-circuits to True."""
    assert _install_claude_plugin(auto=True, status_fn=_all_installed) is True


def test_install_claude_plugin_auto_installs_all_missing() -> None:
    """With auto=True, every not-yet-installed plugin is installed."""
    installed: list[str] = []
    result = _install_claude_plugin(
        auto=True,
        status_fn=_none_installed,
        install_fn=lambda plugin: installed.append(plugin.name) or True,
    )
    assert result is True
    assert installed == [plugin.name for plugin in _CLAUDE_CODE_PLUGINS]


def test_install_claude_plugin_auto_only_installs_missing() -> None:
    """With auto=True, plugins that are already installed are not reinstalled."""
    names = [plugin.name for plugin in _CLAUDE_CODE_PLUGINS]
    installed: list[str] = []
    result = _install_claude_plugin(
        auto=True,
        # First plugin already present; only the rest should be installed.
        status_fn=lambda: (True, {name: (name == names[0]) for name in names}),
        install_fn=lambda plugin: installed.append(plugin.name) or True,
    )
    assert result is True
    assert installed == names[1:]


def test_install_claude_plugin_skips_without_tty() -> None:
    """Without an interactive terminal, _install_claude_plugin skips and returns False.

    The select_fn would install if reached, but the is_interactive_fn
    gate fires first.
    """
    installed: list[str] = []
    assert (
        _install_claude_plugin(
            auto=False,
            status_fn=_none_installed,
            is_interactive_fn=lambda: False,
            select_fn=lambda candidates: candidates,
            install_fn=lambda plugin: installed.append(plugin.name) or True,
        )
        is False
    )
    assert installed == []


def test_install_claude_plugin_picker_skip_returns_false() -> None:
    """When the picker returns no selection, nothing is installed and it returns False."""
    installed: list[str] = []
    assert (
        _install_claude_plugin(
            auto=False,
            status_fn=_none_installed,
            is_interactive_fn=lambda: True,
            select_fn=lambda candidates: (),
            install_fn=lambda plugin: installed.append(plugin.name) or True,
        )
        is False
    )
    assert installed == []


def test_install_claude_plugin_picker_installs_selected_subset() -> None:
    """When the picker selects a subset, only those plugins are installed."""
    installed: list[str] = []
    result = _install_claude_plugin(
        auto=False,
        status_fn=_none_installed,
        is_interactive_fn=lambda: True,
        select_fn=lambda candidates: (candidates[0],),
        install_fn=lambda plugin: installed.append(plugin.name) or True,
    )
    assert result is True
    assert installed == [_CLAUDE_CODE_PLUGINS[0].name]


def test_install_claude_plugin_returns_false_when_install_fails() -> None:
    """When a selected install fails, _install_claude_plugin returns False."""
    result = _install_claude_plugin(
        auto=True,
        status_fn=_none_installed,
        install_fn=lambda plugin: False,
    )
    assert result is False


def test_plugins_status_returns_string() -> None:
    """_plugins_status returns a string describing plugin status."""
    status = _plugins_status()
    assert isinstance(status, str)
    assert len(status) > 0


def test_print_extras_status_runs_without_error() -> None:
    """_print_extras_status completes without error."""
    # Inject a fast claude-plugin status so the test does not shell out to the
    # `claude` CLI -- a Node process whose startup dominated this test's runtime
    # and made it flaky under the 10s offload timeout (observed at 10.05s in CI;
    # ~0.4s locally). Report claude as available so the richer status-formatting
    # branch is still exercised. The other status paths are fast local reads.
    _print_extras_status(
        claude_native_plugin_status_fn=lambda: (True, {plugin.name: False for plugin in _CLAUDE_CODE_PLUGINS})
    )


# Probes plugin / shell-completion / claude-plugin status, which can stall on a
# contended CI sandbox and trip the global 10s pytest-timeout even though it runs
# in well under a second locally. Bump the per-test timeout for headroom.
@pytest.mark.timeout(30)
def test_extras_no_args_shows_status(cli_runner: CliRunner) -> None:
    """Running 'mngr extras' with no flags shows status."""
    result = cli_runner.invoke(extras, [])
    assert result.exit_code == 0
    assert "Extras" in result.output


def test_extras_interactive_mode(cli_runner: CliRunner) -> None:
    """Running 'mngr extras -i' walks through all extras interactively."""
    # In the test environment, has_interactive_terminal() returns False
    # (no /dev/tty), so each _install_* short-circuits before reaching the
    # urwid picker.
    result = cli_runner.invoke(extras, ["-i"])
    assert result.exit_code == 0
    assert "Plugins" in result.output
    assert "Shell Completion" in result.output
    assert "Claude Code Plugins" in result.output


def test_extras_help(cli_runner: CliRunner) -> None:
    """The --help flag should work for the extras command."""
    result = cli_runner.invoke(extras, ["--help"])
    assert result.exit_code == 0


def test_extras_completion_subcommand(cli_runner: CliRunner) -> None:
    """The 'extras completion' subcommand should work."""
    result = cli_runner.invoke(extras, ["completion"])
    assert result.exit_code == 0


def test_extras_claude_plugin_subcommand(cli_runner: CliRunner) -> None:
    """The 'extras claude-plugin' subcommand should work."""
    result = cli_runner.invoke(extras, ["claude-plugin"])
    assert result.exit_code == 0


def test_extras_completion_yes_flag(cli_runner: CliRunner) -> None:
    """The 'extras completion -y' subcommand auto-installs."""
    result = cli_runner.invoke(extras, ["completion", "-y"])
    assert result.exit_code == 0


def test_extras_claude_plugin_yes_flag(cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """'extras claude-plugin -y' installs exactly the not-yet-installed plugins.

    Prepend a stub ``claude`` to PATH so the command deterministically
    exercises the real auto-install plumbing (marketplace add + install per
    plugin) without a network round-trip or a dependency on whether the real
    marketplace is reachable. The stub reports imbue-code-guardian as already
    installed via ``claude plugin list``, so only imbue-mngr-skills should be
    installed -- letting us assert that the already-installed plugin is left
    untouched.
    """
    stub_claude = tmp_path / "claude"
    # `claude plugin list --json` returns an array of objects keyed by `id`;
    # report imbue-code-guardian as already installed and succeed otherwise.
    listing = json.dumps(
        [{"id": "imbue-code-guardian@imbue-code-guardian", "version": "0.2.1", "scope": "project", "enabled": True}]
    )
    stub_claude.write_text(
        f'#!/usr/bin/env bash\nif [ "$1" = "plugin" ] && [ "$2" = "list" ]; then\n  echo \'{listing}\'\nfi\nexit 0\n'
    )
    stub_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = cli_runner.invoke(extras, ["claude-plugin", "-y"])
    assert result.exit_code == 0
    # Only the not-yet-installed plugin is installed; code-guardian is untouched.
    assert "Installed imbue-mngr-skills." in result.output
    assert "imbue-code-guardian" not in result.output


def test_read_current_default_agent_type_returns_value() -> None:
    """_read_current_default_agent_type extracts [commands.create] type."""
    raw = {"commands": {"create": {"type": "claude"}}}
    assert _read_current_default_agent_type(raw) == "claude"


def test_read_current_default_agent_type_returns_none_when_missing() -> None:
    """_read_current_default_agent_type returns None when no value is set."""
    assert _read_current_default_agent_type({}) is None
    assert _read_current_default_agent_type({"commands": {}}) is None
    assert _read_current_default_agent_type({"commands": {"create": {}}}) is None


def test_list_extras_agent_type_choices_includes_user_config_types() -> None:
    """_list_extras_agent_type_choices unions registered + user-config-defined types."""
    raw = {"agent_types": {"my-custom": {"parent_type": "claude"}}}
    result = _list_extras_agent_type_choices(raw, ["claude", "command"])
    assert result == ["claude", "command", "my-custom"]


def test_list_extras_agent_type_choices_handles_empty_raw() -> None:
    """_list_extras_agent_type_choices returns just the registered list when raw has no agent_types."""
    assert _list_extras_agent_type_choices({}, ["claude"]) == ["claude"]


def test_install_default_agent_type_already_set() -> None:
    """_install_default_agent_type returns True without prompting if already set."""
    written: list[str] = []
    result = _install_default_agent_type(
        auto=False,
        status_fn=lambda: ("claude", ["claude", "command"]),
        is_interactive_fn=lambda: True,
        prompt_fn=lambda _avail: "should-not-be-called",
        write_fn=lambda v: written.append(v) or Path("/x"),
    )
    assert result is True
    assert written == []


def test_install_default_agent_type_no_choices() -> None:
    """_install_default_agent_type returns False when no agent types are registered."""
    written: list[str] = []
    result = _install_default_agent_type(
        auto=False,
        status_fn=lambda: (None, []),
        is_interactive_fn=lambda: True,
        prompt_fn=lambda _avail: "should-not-be-called",
        write_fn=lambda v: written.append(v) or Path("/x"),
    )
    assert result is False
    assert written == []


def test_install_default_agent_type_auto_prints_suggestion(capsys: pytest.CaptureFixture[str]) -> None:
    """_install_default_agent_type with auto=True prints command + types but writes nothing."""
    written: list[str] = []
    result = _install_default_agent_type(
        auto=True,
        status_fn=lambda: (None, ["claude", "command"]),
        is_interactive_fn=lambda: True,
        prompt_fn=lambda _avail: "should-not-be-called",
        write_fn=lambda v: written.append(v) or Path("/x"),
    )
    assert result is False
    out = capsys.readouterr().out
    assert "mngr config set commands.create.type" in out
    assert "claude" in out
    assert "command" in out
    assert written == []


def test_install_default_agent_type_no_tty_prints_suggestion(capsys: pytest.CaptureFixture[str]) -> None:
    """Without an interactive terminal, falls back to the auto=True behavior."""
    written: list[str] = []
    result = _install_default_agent_type(
        auto=False,
        status_fn=lambda: (None, ["claude"]),
        is_interactive_fn=lambda: False,
        prompt_fn=lambda _avail: "should-not-be-called",
        write_fn=lambda v: written.append(v) or Path("/x"),
    )
    assert result is False
    out = capsys.readouterr().out
    assert "mngr config set commands.create.type" in out
    assert written == []


def test_install_default_agent_type_writes_picked_value() -> None:
    """When TTY available and a value picked, writes that agent type."""
    written: list[str] = []
    result = _install_default_agent_type(
        auto=False,
        status_fn=lambda: (None, ["claude", "command"]),
        is_interactive_fn=lambda: True,
        prompt_fn=lambda _avail: "claude",
        write_fn=lambda v: written.append(v) or Path("/x"),
    )
    assert result is True
    assert written == ["claude"]


def test_install_default_agent_type_skip_writes_nothing() -> None:
    """When the prompt returns None (skip), writes nothing."""
    written: list[str] = []
    result = _install_default_agent_type(
        auto=False,
        status_fn=lambda: (None, ["claude", "command"]),
        is_interactive_fn=lambda: True,
        prompt_fn=lambda _avail: None,
        write_fn=lambda v: written.append(v) or Path("/x"),
    )
    assert result is False
    assert written == []


def test_extras_config_subcommand(cli_runner: CliRunner) -> None:
    """The 'extras config' subcommand should work."""
    result = cli_runner.invoke(extras, ["config"])
    assert result.exit_code == 0


def test_extras_config_yes_flag(cli_runner: CliRunner) -> None:
    """The 'extras config -y' subcommand runs non-interactively."""
    result = cli_runner.invoke(extras, ["config", "-y"])
    assert result.exit_code == 0


def test_extras_interactive_includes_default_type(cli_runner: CliRunner) -> None:
    """Running 'mngr extras -i' walks through the default agent type prompt."""
    result = cli_runner.invoke(extras, ["-i"])
    assert result.exit_code == 0
    assert "Default Agent Type" in result.output
