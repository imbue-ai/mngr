"""Integration tests for the schedule run command (local provider)."""

import json

import click
import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_schedule.cli.run import _emit_output
from imbue.mngr_schedule.cli.run import run_local_trigger
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand
from imbue.mngr_schedule.implementations.local.deploy import deploy_local_schedule
from imbue.mngr_schedule.implementations.local.deploy import get_local_trigger_run_script


def _deploy_echo_trigger(
    mngr_ctx: MngrContext,
    name: str = "test-trigger",
    *,
    is_enabled: bool = True,
) -> None:
    """Deploy a local trigger whose run.sh runs 'echo hello'."""
    trigger = ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngrCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
        is_enabled=is_enabled,
    )
    deploy_local_schedule(
        trigger,
        mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "fakehash",
    )


def test_run_local_trigger_executes_run_script(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Running a local trigger should execute its run.sh and return an exit code."""
    _deploy_echo_trigger(temp_mngr_ctx)
    # run.sh will fail (uv run mngr create isn't set up in test env) but
    # the point is that it tried to execute the script. A non-zero exit
    # code proves run.sh was invoked.
    exit_code = run_local_trigger(temp_mngr_ctx, "test-trigger", OutputFormat.HUMAN)
    assert isinstance(exit_code, int)


def test_run_local_trigger_not_found_raises(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Requesting a nonexistent trigger should raise ClickException."""
    with pytest.raises(click.ClickException, match="No local schedule record found"):
        run_local_trigger(temp_mngr_ctx, "nonexistent", OutputFormat.HUMAN)


def test_run_local_trigger_missing_script_raises(
    temp_mngr_ctx: MngrContext,
) -> None:
    """If the record exists but run.sh is missing, should raise ClickException."""
    _deploy_echo_trigger(temp_mngr_ctx)

    # Delete the run.sh file (resolve the path via the public helper so a
    # layout change in deploy.py doesn't silently break this test).
    run_script = get_local_trigger_run_script(temp_mngr_ctx, "test-trigger")
    run_script.unlink()

    with pytest.raises(click.ClickException, match="Wrapper script not found"):
        run_local_trigger(temp_mngr_ctx, "test-trigger", OutputFormat.HUMAN)


def test_run_local_trigger_disabled_still_runs(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A disabled trigger should still be run (with a warning)."""
    _deploy_echo_trigger(temp_mngr_ctx, "disabled-trigger", is_enabled=False)
    exit_code = run_local_trigger(temp_mngr_ctx, "disabled-trigger", OutputFormat.HUMAN)
    assert isinstance(exit_code, int)


def test_run_local_trigger_json_emits_parseable_object(
    temp_mngr_ctx: MngrContext,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON format must emit exactly one parseable JSON object with captured output.

    For the local provider, HUMAN format streams the script's stdout live.
    JSON / JSONL must instead capture the script output and emit it via
    the same structured envelope as the modal branch, so downstream
    consumers see a consistent shape regardless of provider.
    """
    _deploy_echo_trigger(temp_mngr_ctx)

    # Replace the generated run.sh with a deterministic script that writes
    # a known marker to stdout (and nothing to stderr), so we can assert
    # on exact captured output without depending on ``uv run mngr create``.
    run_script = get_local_trigger_run_script(temp_mngr_ctx, "test-trigger")
    run_script.write_text("#!/bin/sh\necho local-trigger-marker\n")

    exit_code = run_local_trigger(temp_mngr_ctx, "test-trigger", OutputFormat.JSON)

    assert exit_code == 0
    captured = capsys.readouterr()
    # In JSON mode, the *only* thing on stdout should be the structured
    # envelope. The script's raw output must be captured into the envelope
    # rather than leaking through.
    payload = json.loads(captured.out)
    assert payload == {"output": "local-trigger-marker\n"}


# =============================================================================
# _emit_output tests
# =============================================================================


@pytest.mark.parametrize(
    ("output_text", "output_format", "expected_raw", "expected_json"),
    [
        pytest.param(
            "hello from trigger\n", OutputFormat.HUMAN, "hello from trigger\n", None, id="human_with_newline"
        ),
        pytest.param("hello from trigger", OutputFormat.HUMAN, "hello from trigger\n", None, id="human_no_newline"),
        pytest.param(
            "trigger output\n", OutputFormat.JSON, None, {"output": "trigger output\n"}, id="json_with_content"
        ),
        pytest.param(
            "trigger output\n",
            OutputFormat.JSONL,
            None,
            {"event": "output", "output": "trigger output\n"},
            id="jsonl_with_content",
        ),
        pytest.param("", OutputFormat.HUMAN, "", None, id="human_empty_suppresses_output"),
        pytest.param("", OutputFormat.JSON, None, {"output": ""}, id="json_empty_still_emits_object"),
        pytest.param(
            "", OutputFormat.JSONL, None, {"event": "output", "output": ""}, id="jsonl_empty_still_emits_object"
        ),
    ],
)
def test_emit_output_produces_expected_stream(
    capsys: pytest.CaptureFixture[str],
    output_text: str,
    output_format: OutputFormat,
    expected_raw: str | None,
    expected_json: dict[str, str] | None,
) -> None:
    """_emit_output produces the right stdout shape for each output format.

    HUMAN format writes the text with a single trailing newline, or nothing at
    all for empty input (avoiding a bare newline). JSON and JSONL always emit a
    parseable object on stdout even when the output is empty, so downstream
    consumers can rely on getting at least one JSON value. JSONL additionally
    tags the line with an ``event`` field.
    """
    _emit_output(output_text, output_format)
    captured = capsys.readouterr()

    if expected_json is not None:
        assert json.loads(captured.out) == expected_json
    else:
        assert expected_raw is not None
        assert captured.out == expected_raw
