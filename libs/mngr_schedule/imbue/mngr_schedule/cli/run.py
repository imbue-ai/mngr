import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_schedule.cli.group import schedule
from imbue.mngr_schedule.cli.options import ScheduleRunCliOptions
from imbue.mngr_schedule.cli.provider_utils import load_schedule_provider
from imbue.mngr_schedule.implementations.local.deploy import get_local_schedule_creation_record
from imbue.mngr_schedule.implementations.local.deploy import get_local_trigger_run_script
from imbue.mngr_schedule.implementations.modal.deploy import get_modal_schedule_creation_record
from imbue.mngr_schedule.implementations.modal.deploy import invoke_modal_trigger_function


@schedule.command(name="run")
@click.argument("name", required=True)
@optgroup.group("Execution")
@optgroup.option(
    "--provider",
    required=True,
    help="Provider on which the trigger is deployed (e.g. 'local', 'modal').",
)
@add_common_options
@click.pass_context
def schedule_run(ctx: click.Context, **kwargs: Any) -> None:
    """Run a scheduled trigger immediately.

    Executes the specified trigger's command right now, regardless of its
    cron schedule. The trigger is invoked through the exact same code path
    as a normal scheduled execution:

    \b
    - Local triggers: executes the run.sh wrapper script (same as cron)
    - Modal triggers: invokes the deployed function on Modal (same as Modal cron)

    \b
    Examples:
      mngr schedule run my-trigger --provider local
      mngr schedule run my-trigger --provider modal
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_run",
        command_class=ScheduleRunCliOptions,
    )

    provider = load_schedule_provider(opts.provider, mngr_ctx)

    if isinstance(provider, LocalProviderInstance):
        exit_code = run_local_trigger(mngr_ctx, opts.name, output_opts.output_format)
    elif isinstance(provider, ModalProviderInstance):
        output = run_modal_trigger(provider, opts.name)
        _emit_output(output, output_opts.output_format)
        exit_code = 0
    else:
        assert_never(provider)

    ctx.exit(exit_code)


def _emit_output(output: str, output_format: OutputFormat) -> None:
    """Emit trigger output in the requested format.

    JSONL tags the line with an ``event`` field, mirroring
    ``stream_or_accumulate_response`` and ``emit_event`` so consumers can
    dispatch on event type.

    JSON and JSONL always emit a parseable object (even when the output is
    empty) so that downstream consumers can rely on getting at least one
    JSON value on stdout. HUMAN mode suppresses empty output to avoid
    printing a bare newline when the trigger produced nothing.
    """
    match output_format:
        case OutputFormat.JSON:
            emit_final_json({"output": output})
        case OutputFormat.JSONL:
            emit_final_json({"event": "output", "output": output})
        case OutputFormat.HUMAN:
            if output:
                write_human_line("{}", output.rstrip("\n"))
        case _ as unreachable:
            assert_never(unreachable)


def run_local_trigger(mngr_ctx: MngrContext, trigger_name: str, output_format: OutputFormat) -> int:
    """Run a local trigger by executing its run.sh wrapper script.

    This is the exact same code path as cron: execute the run.sh script
    that was created by schedule add.

    Output behavior depends on ``output_format``:
    - HUMAN: the script runs with inherited stdout/stderr, so its output
      streams to the terminal live (matching cron-style execution).
    - JSON / JSONL: stdout and stderr are captured and emitted as a
      single parseable object via ``_emit_output``, matching the modal
      branch so downstream consumers see a consistent shape regardless
      of provider.
    """
    record = get_local_schedule_creation_record(mngr_ctx, trigger_name)
    if record is None:
        raise click.ClickException(
            f"No local schedule record found for trigger '{trigger_name}'. "
            "Use 'mngr schedule list --provider local' to see available triggers."
        )

    if not record.trigger.is_enabled:
        logger.warning("Trigger '{}' is disabled, but running it anyway", trigger_name)

    run_script = get_local_trigger_run_script(mngr_ctx, trigger_name)
    if not run_script.is_file():
        raise click.ClickException(
            f"Wrapper script not found at {run_script}. "
            f"The trigger '{trigger_name}' may need to be re-deployed with 'mngr schedule add'."
        )

    logger.info("Executing local trigger '{}' via {}", trigger_name, run_script)
    # Run via ConcurrencyGroup so the subprocess participates in the
    # standard cleanup protocol (per CLAUDE.md's direct-subprocess ratchet).
    # HUMAN mode writes each output chunk to stdout/stderr as it arrives to
    # preserve cron-style live streaming; JSON / JSONL mode stays silent
    # during the run and emits one structured envelope at the end.
    on_output = _stream_human_output if output_format is OutputFormat.HUMAN else None
    with ConcurrencyGroup(name=f"schedule-run-local-{trigger_name}") as cg:
        finished = cg.run_process_to_completion(
            [str(run_script)],
            is_checked_after=False,
            on_output=on_output,
        )

    # ConcurrencyGroup may surface a None returncode if the process was
    # terminated without a clean exit; default to 1 to signal failure.
    exit_code = finished.returncode if finished.returncode is not None else 1

    match output_format:
        case OutputFormat.HUMAN:
            # Output already streamed to stdout/stderr via _stream_human_output.
            return exit_code
        case OutputFormat.JSON | OutputFormat.JSONL:
            # Merge stderr into stdout so the emitted payload mirrors the
            # combined stream that the modal branch returns.
            _emit_output(finished.stdout + finished.stderr, output_format)
            return exit_code
        case _ as unreachable:
            assert_never(unreachable)


def _stream_human_output(chunk: str, is_stderr: bool) -> None:
    """Forward a ConcurrencyGroup output chunk to stdout or stderr.

    Used as the ``on_output`` callback for ``run_process_to_completion`` so
    the local trigger's output streams live to the terminal in HUMAN mode.
    """
    stream = sys.stderr if is_stderr else sys.stdout
    stream.write(chunk)
    stream.flush()


def run_modal_trigger(provider: ModalProviderInstance, trigger_name: str) -> str:
    """Run a modal trigger by invoking the deployed function on Modal.

    This is the exact same code path as Modal cron: invoke the
    run_scheduled_trigger() function that was deployed by schedule add.

    Returns the command output captured by run_scheduled_trigger().
    """
    record = get_modal_schedule_creation_record(provider, trigger_name)
    if record is None:
        raise click.ClickException(
            f"No modal schedule record found for trigger '{trigger_name}'. "
            "Use 'mngr schedule list --provider modal' to see available triggers."
        )

    if not record.trigger.is_enabled:
        logger.warning("Trigger '{}' is disabled, but running it anyway", trigger_name)

    logger.info(
        "Invoking modal trigger '{}' (app: {}, env: {})",
        trigger_name,
        record.app_name,
        record.environment,
    )

    try:
        return invoke_modal_trigger_function(record)
    except MngrError as exc:
        raise click.ClickException(f"Modal invocation failed for trigger '{trigger_name}': {exc}") from None
