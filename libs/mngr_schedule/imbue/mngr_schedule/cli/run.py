import subprocess
import sys
from typing import Any
from typing import assert_never

import click
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
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
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="schedule_run",
        command_class=ScheduleRunCliOptions,
    )

    provider = load_schedule_provider(opts.provider, mngr_ctx)

    if isinstance(provider, LocalProviderInstance):
        exit_code = run_local_trigger(mngr_ctx, opts.name)
    elif isinstance(provider, ModalProviderInstance):
        exit_code = run_modal_trigger(provider, opts.name)
    else:
        assert_never(provider)

    ctx.exit(exit_code)


def run_local_trigger(mngr_ctx: MngrContext, trigger_name: str) -> int:
    """Run a local trigger by executing its run.sh wrapper script.

    This is the exact same code path as cron: execute the run.sh script
    that was created by schedule add.
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
    result = subprocess.run([str(run_script)])
    return result.returncode


def run_modal_trigger(provider: ModalProviderInstance, trigger_name: str) -> int:
    """Run a modal trigger by invoking the deployed function on Modal.

    This is the exact same code path as Modal cron: invoke the
    run_scheduled_trigger() function that was deployed by schedule add.
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
        output = invoke_modal_trigger_function(record)
    except MngrError as exc:
        raise click.ClickException(f"Modal invocation failed for trigger '{trigger_name}': {exc}") from None

    if output:
        sys.stdout.write(output)

    return 0
