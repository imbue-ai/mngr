import json
from pathlib import Path
from typing import Any

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_diagnose.cli import DIAGNOSE_CLONE_DIR
from imbue.mngr.cli.issue_reporting import get_mngr_version
from imbue.mngr_diagnose.cli import diagnose


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

    def fake_ensure(clone_dir: Path, cg: ConcurrencyGroup) -> None:
        clone_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("imbue.mngr_diagnose.cli.ensure_mngr_clone", fake_ensure)

    # Capture the create invocation by replacing the module-level create_cmd reference
    captured_args: list[list[str]] = []

    original_make_context = click.Command.make_context

    def capturing_make_context(self: click.Command, info_name: str, args: list[str], **kwargs: Any) -> click.Context:
        if info_name == "diagnose" and "--from" in args:
            # This is the create_cmd.make_context call from within diagnose
            captured_args.append(args)
            raise SystemExit(0)
        return original_make_context(self, info_name, args, **kwargs)

    monkeypatch.setattr(click.Command, "make_context", capturing_make_context)

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
    def fake_ensure(clone_dir: Path, cg: ConcurrencyGroup) -> None:
        clone_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("imbue.mngr_diagnose.cli.ensure_mngr_clone", fake_ensure)

    captured_args: list[list[str]] = []
    original_make_context = click.Command.make_context

    def capturing_make_context(self: click.Command, info_name: str, args: list[str], **kwargs: Any) -> click.Context:
        if info_name == "diagnose" and "--from" in args:
            captured_args.append(args)
            raise SystemExit(0)
        return original_make_context(self, info_name, args, **kwargs)

    monkeypatch.setattr(click.Command, "make_context", capturing_make_context)

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
    def fake_ensure(clone_dir: Path, cg: ConcurrencyGroup) -> None:
        clone_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("imbue.mngr_diagnose.cli.ensure_mngr_clone", fake_ensure)

    captured_args: list[list[str]] = []
    original_make_context = click.Command.make_context

    def capturing_make_context(self: click.Command, info_name: str, args: list[str], **kwargs: Any) -> click.Context:
        if info_name == "diagnose" and "--from" in args:
            captured_args.append(args)
            raise SystemExit(0)
        return original_make_context(self, info_name, args, **kwargs)

    monkeypatch.setattr(click.Command, "make_context", capturing_make_context)

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
    def fake_ensure(clone_dir: Path, cg: ConcurrencyGroup) -> None:
        clone_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("imbue.mngr_diagnose.cli.ensure_mngr_clone", fake_ensure)

    captured_args: list[list[str]] = []
    original_make_context = click.Command.make_context

    def capturing_make_context(self: click.Command, info_name: str, args: list[str], **kwargs: Any) -> click.Context:
        if info_name == "diagnose" and "--from" in args:
            captured_args.append(args)
            raise SystemExit(0)
        return original_make_context(self, info_name, args, **kwargs)

    monkeypatch.setattr(click.Command, "make_context", capturing_make_context)

    cli_runner.invoke(
        diagnose,
        ["error", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(captured_args) == 1
    assert "--type" not in captured_args[0]


def test_diagnose_default_clone_dir() -> None:
    """Default clone dir is /tmp/mngr-diagnose."""
    assert DIAGNOSE_CLONE_DIR == Path("/tmp/mngr-diagnose")
