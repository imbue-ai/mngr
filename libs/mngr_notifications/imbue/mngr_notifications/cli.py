import sys
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.observe import ObserveLockError
from imbue.mngr.api.observe import acquire_observe_lock
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.api.observe import release_observe_lock
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import PluginName
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.notification_verifier import DEFAULT_CLICK_TIMEOUT
from imbue.mngr_notifications.notification_verifier import check_notifier_binary
from imbue.mngr_notifications.notification_verifier import run_test_notification
from imbue.mngr_notifications.notifier import Notifier
from imbue.mngr_notifications.notifier import get_notifier
from imbue.mngr_notifications.watcher import watch_for_waiting_agents


class NotifyCliOptions(CommonCliOptions):
    verify: bool = True


def _get_plugin_config(mngr_ctx: MngrContext) -> NotificationsPluginConfig:
    config = mngr_ctx.config.plugins.get(PluginName("notifications"))
    if config is not None and isinstance(config, NotificationsPluginConfig):
        return config
    return NotificationsPluginConfig()


def _is_observe_running(mngr_ctx: MngrContext) -> bool:
    """Check if mngr observe is already running by trying to acquire its lock."""
    try:
        fd = acquire_observe_lock(get_default_events_base_dir(mngr_ctx.config))
        release_observe_lock(fd)
        return False
    except ObserveLockError:
        return True


@contextmanager
def _ensure_observe(mngr_ctx: MngrContext) -> Iterator[None]:
    """Start mngr observe in the background if not already running. Stop it on exit."""
    if _is_observe_running(mngr_ctx):
        write_human_line("Using existing mngr observe process")
        yield
        return

    write_human_line("Starting mngr observe in background...")
    process = mngr_ctx.concurrency_group.run_process_in_background(
        [sys.executable, "-m", "imbue.mngr.main", "observe", "--quiet"],
    )
    try:
        yield
    finally:
        process.terminate()


def _run_verification(
    notifier: Notifier,
    cg: ConcurrencyGroup,
    binary_checker: Callable[[Notifier], str | None] = check_notifier_binary,
    click_timeout: float = DEFAULT_CLICK_TIMEOUT,
) -> bool:
    """Send a test notification and verify delivery. Returns True if verified."""
    write_human_line("Sending a test notification -- you should see it now. Please click it to confirm.")
    write_human_line("(use --no-verify to skip this check)")
    result = run_test_notification(notifier, cg, click_timeout=click_timeout, binary_checker=binary_checker)

    if not result.is_sent:
        write_human_line("FAILED: {}", result.error_message)
        return False

    if result.is_clicked is True:
        write_human_line("Notification delivery verified.")
        return True

    if result.is_clicked is False:
        write_human_line(
            "Test notification was sent but not clicked within {} seconds.",
            int(click_timeout),
        )
        write_human_line("If you did not see the notification, check your notification")
        write_human_line("permissions in System Settings > Notifications.")
        return False

    # is_clicked is None: click detection not supported (e.g. Linux)
    is_confirmed = click.confirm("Did you see the test notification?", default=False)
    if is_confirmed:
        write_human_line("Notification delivery verified.")
        return True

    write_human_line("If notifications are not appearing, check your notification settings.")
    return False


@click.command()
@click.option(
    "--verify/--no-verify",
    default=True,
    help="Verify notification delivery on startup by sending a test notification.",
)
@add_common_options
@click.pass_context
def notify(ctx: click.Context, **kwargs: object) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="notify",
        command_class=NotifyCliOptions,
    )

    plugin_config = _get_plugin_config(mngr_ctx)

    if plugin_config.notification_only:
        write_human_line("Notification-only mode (no click-to-connect)")
    elif plugin_config.terminal_app is not None:
        write_human_line("Click-to-connect enabled (terminal: {})", plugin_config.terminal_app)
    elif plugin_config.custom_terminal_command is not None:
        write_human_line("Click-to-connect enabled (custom command)")
    else:
        write_human_line("No terminal configured -- notifications will not have click-to-connect.")
        write_human_line(
            "Set plugins.notifications.terminal_app, custom_terminal_command, or notification_only in settings.toml."
        )

    notifier = get_notifier()
    if notifier is None:
        return

    if opts.verify:
        if not _run_verification(notifier, mngr_ctx.concurrency_group):
            return

    write_human_line("Watching for agents transitioning to WAITING... (Ctrl+C to stop)")

    with _ensure_observe(mngr_ctx):
        try:
            watch_for_waiting_agents(
                mngr_ctx=mngr_ctx,
                plugin_config=plugin_config,
                notifier=notifier,
            )
        except KeyboardInterrupt:
            logger.debug("Received keyboard interrupt")

    write_human_line("Stopped watching")


CommandHelpMetadata(
    key="notify",
    one_line_description="Notify when agents transition to WAITING",
    synopsis="mngr notify [--no-verify]",
    description="""Sends a desktop notification when any agent transitions from RUNNING to WAITING.

On startup, sends a test notification to verify delivery is working.
On macOS, you will be asked to click the notification to confirm;
on Linux, you will be prompted to confirm you saw it. Use --no-verify
to skip this check.

Automatically starts `mngr observe` in the background if it is not already running.

On macOS, notifications are sent via terminal-notifier (install with:
brew install terminal-notifier). On Linux, via notify-send (libnotify).

To enable click-to-connect (opens a terminal tab running mngr connect),
configure the plugin in settings.toml:

    [plugins.notifications]
    terminal_app = "iTerm"

Or use a custom command (MNGR_AGENT_NAME is set in the environment):

    [plugins.notifications]
    custom_terminal_command = "my-terminal -e mngr connect $MNGR_AGENT_NAME"

Press Ctrl+C to stop.""",
    examples=(
        ("Notify on all agents", "mngr notify"),
        ("Skip notification verification", "mngr notify --no-verify"),
    ),
    see_also=(
        ("observe", "Stream agent state changes to local event files"),
        ("list", "List agents to see their current state"),
    ),
).register()

add_pager_help_option(notify)
