import json
import tomllib
from pathlib import Path

import click
import pluggy
import pytest
from click.testing import CliRunner
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.cli.issue_reporting import DIAGNOSE_FLOW_META_KEY
from imbue.mngr.cli.issue_reporting import _is_diagnose_command
from imbue.mngr.cli.issue_reporting import get_mngr_version
from imbue.mngr_diagnose.cli import DIAGNOSE_CLONE_DIR
from imbue.mngr_diagnose.cli import diagnose

_MNGR_PYPROJECT = Path(__file__).resolve().parents[4] / "libs" / "mngr" / "pyproject.toml"


class _CreateRecord(FrozenModel):
    """What a recording stand-in for ``create_cmd`` captured on invocation."""

    model_config = {"arbitrary_types_allowed": True}

    args: list[str] = Field(description="Argv forwarded to the fake create command")
    parent_ctx: click.Context = Field(description="Click context under which create was invoked")


def _install_recording_create(monkeypatch: pytest.MonkeyPatch) -> list[_CreateRecord]:
    """Replace the ``create_cmd`` imported by diagnose with a recording fake.

    This is targeted: we swap the module-level reference, not any click
    internals. The fake is itself a real click command, so all the
    existing ``make_context``/``invoke`` flow still runs.

    Also stubs ``ensure_mngr_clone`` so the test does not hit the network.
    """

    def fake_ensure(clone_dir: Path, cg: ConcurrencyGroup) -> None:
        clone_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("imbue.mngr_diagnose.cli.ensure_mngr_clone", fake_ensure)

    records: list[_CreateRecord] = []

    @click.command(context_settings={"ignore_unknown_options": True})
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def fake_create(ctx: click.Context, args: tuple[str, ...]) -> None:
        assert ctx.parent is not None
        records.append(_CreateRecord(args=list(args), parent_ctx=ctx.parent))

    monkeypatch.setattr("imbue.mngr_diagnose.cli.create_cmd", fake_create)
    return records


def test_get_mngr_version_matches_pyproject() -> None:
    """get_mngr_version returns the version declared in mngr's pyproject.toml."""
    expected = tomllib.loads(_MNGR_PYPROJECT.read_text())["project"]["version"]
    assert get_mngr_version() == expected


def test_diagnose_with_context_file(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose reads context file and passes info to create."""
    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(
        json.dumps(
            {
                "traceback_str": "Traceback:\n  ValueError",
                "mngr_version": "0.2.4",
                "error_type": "ValueError",
                "error_message": "oops",
            }
        )
    )

    records = _install_recording_create(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["--context-file", str(ctx_path), "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(records) == 1
    args = records[0].args
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
    records = _install_recording_create(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["--description", "test error description", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(records) == 1
    args = records[0].args
    msg_idx = args.index("--message") + 1
    assert "test error description" in args[msg_idx]


def test_diagnose_forwards_unknown_options_to_create(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Options not recognized by diagnose are forwarded verbatim to create."""
    records = _install_recording_create(monkeypatch)

    cli_runner.invoke(
        diagnose,
        [
            "--description",
            "error",
            "--clone-dir",
            str(tmp_path / "clone"),
            "--type",
            "opencode",
            "--provider",
            "modal",
            "--idle-timeout",
            "5m",
        ],
        obj=plugin_manager,
    )

    assert len(records) == 1
    args = records[0].args
    assert "--type" in args
    assert "opencode" in args
    assert "--provider" in args
    assert "modal" in args
    assert "--idle-timeout" in args
    assert "5m" in args


def test_diagnose_rejects_reserved_flags(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose refuses pass-through args that conflict with its hardcoded create options."""
    _install_recording_create(monkeypatch)

    result = cli_runner.invoke(
        diagnose,
        ["--description", "error", "--from", "@some-host"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot pass --from to diagnose" in result.output


def test_diagnose_rejects_reserved_message_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnose refuses --message in pass-through (it builds the message itself)."""
    _install_recording_create(monkeypatch)

    result = cli_runner.invoke(
        diagnose,
        ["--description", "error", "--message", "user-supplied"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot pass --message" in result.output


def test_diagnose_rejects_reserved_flag_with_equals_form(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reserved flag detection handles --flag=value form too."""
    _install_recording_create(monkeypatch)

    result = cli_runner.invoke(
        diagnose,
        ["--description", "error", "--branch=feature"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot pass --branch to diagnose" in result.output


def test_diagnose_default_clone_dir() -> None:
    """Default clone dir is /tmp/mngr-diagnose."""
    assert DIAGNOSE_CLONE_DIR == Path("/tmp/mngr-diagnose")


def test_diagnose_sets_flow_flag_before_invoking_create(
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: running diagnose sets the meta flag that the error handler uses.

    Uses a recording stand-in for create_cmd so the test exercises the real
    diagnose flow without launching an actual agent.
    """
    records = _install_recording_create(monkeypatch)

    cli_runner.invoke(
        diagnose,
        ["--description", "error", "--clone-dir", str(tmp_path / "clone")],
        obj=plugin_manager,
    )

    assert len(records) == 1
    # The parent ctx captured when create was invoked is the same ctx that
    # AliasAwareGroup will pass to handle_unexpected_error on a crash.
    parent_ctx = records[0].parent_ctx
    assert parent_ctx.meta.get(DIAGNOSE_FLOW_META_KEY) is True
    assert _is_diagnose_command(parent_ctx) is True
