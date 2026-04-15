import json
from pathlib import Path
from typing import Any

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.issue_reporting import get_mngr_version
from imbue.mngr_diagnose.cli import DIAGNOSE_CLONE_DIR
from imbue.mngr_diagnose.cli import diagnose


def _stub_clone_and_capture_create_args(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub out the clone step and capture the args passed to `create`.

    Patches `ensure_mngr_clone` to be a no-op (just mkdir) and replaces
    `click.Command.make_context` with a spy that records the args list
    whenever the diagnose command invokes the create command, then raises
    SystemExit(0) to short-circuit the actual agent creation.

    Returns the (initially empty) list that will be populated with one
    entry per intercepted invocation.
    """
    def fake_ensure(clone_dir: Path, cg: ConcurrencyGroup) -> None:
        clone_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("imbue.mngr_diagnose.cli.ensure_mngr_clone", fake_ensure)

    captured: list[list[str]] = []
    original_make_context = click.Command.make_context

    def capturing_make_context(self: click.Command, info_name: str, args: list[str], **kwargs: Any) -> click.Context:
        if info_name == "diagnose" and "--from" in args:
            captured.append(args)
            raise SystemExit(0)
        return original_make_context(self, info_name, args, **kwargs)

    monkeypatch.setattr(click.Command, "make_context", capturing_make_context)
    return captured


def test_get_mngr_version() -> None:
    """get_mngr_version returns a version string."""
    version = get_mngr_version()
    assert isinstance(version, str)
    assert len(version) > 0


def test_diagnose_with_context_file(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose reads context file and passes info to create."""
    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(json.dumps({
        "traceback_str": "Traceback:\n  ValueError",
        "mngr_version": "0.2.4",
        "error_type": "ValueError",
        "error_message": "oops",
    }))

    captured_args = _stub_clone_and_capture_create_args(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["--context-file", str(ctx_path), "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(captured_args) == 1
    args = captured_args[0]
    assert "--from" in args
    assert "--transfer" in args
    assert "git-worktree" in args
    assert "--branch" in args
    assert "main:" in args
    assert "--message" in args
    msg_idx = args.index("--message") + 1
    assert "0.2.4" in args[msg_idx]
    assert "ValueError" in args[msg_idx]


def test_diagnose_with_description(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose with just a description passes it through."""
    captured_args = _stub_clone_and_capture_create_args(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["test error description", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(captured_args) == 1
    args = captured_args[0]
    msg_idx = args.index("--message") + 1
    assert "test error description" in args[msg_idx]


def test_diagnose_with_agent_type(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose with --type passes it through to create."""
    captured_args = _stub_clone_and_capture_create_args(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["error", "--type", "opencode", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(captured_args) == 1
    args = captured_args[0]
    assert "--type" in args
    assert "opencode" in args


def test_diagnose_no_type_omits_type_flag(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --type is not specified, the create args should not contain --type."""
    captured_args = _stub_clone_and_capture_create_args(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["error", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(captured_args) == 1
    assert "--type" not in captured_args[0]


def test_diagnose_does_not_pass_auto_approve(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose relies on interactive trust prompts, not blanket -y."""
    captured_args = _stub_clone_and_capture_create_args(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["error", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(captured_args) == 1
    assert "-y" not in captured_args[0]


def test_diagnose_default_clone_dir() -> None:
    """Default clone dir is /tmp/mngr-diagnose."""
    assert DIAGNOSE_CLONE_DIR == Path("/tmp/mngr-diagnose")
