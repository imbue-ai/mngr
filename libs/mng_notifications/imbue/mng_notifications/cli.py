import sys
from contextlib import contextmanager
from typing import Iterator

import click
from loguru import logger

from imbue.mng.api.observe import ObserveLockError
from imbue.mng.api.observe import acquire_observe_lock
from imbue.mng.api.observe import get_default_events_base_dir
from imbue.mng.api.observe import release_observe_lock
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import PluginName
from imbue.mng_notifications.config import NotificationsPluginConfig
from imbue.mng_notifications.notifier import get_notifier
from imbue.mng_notifications.watcher import watch_for_waiting_agents


class WatchCliOptions(CommonCliOptions):
    pass


def _get_plugin_config(mng_ctx: MngContext) -> NotificationsPluginConfig:
    config = mng_ctx.config.plugins.get(PluginName("notifications"))
    if config is not None and isinstance(config, NotificationsPluginConfig):
        return config
    return NotificationsPluginConfig()


def _is_observe_running(mng_ctx: MngContext) -> bool:
    """Check if mng observe is already running by trying to acquire its lock."""
    try:
        fd = acquire_observe_lock(get_default_events_base_dir(mng_ctx.config))
        release_observe_lock(fd)
        return False
    except ObserveLockError:
        return True


@contextmanager
def _ensure_observe(mng_ctx: MngContext) -> Iterator[None]:
    """Start mng observe in the background if not already running. Stop it on exit."""
    if _is_observe_running(mng_ctx):
        write_human_line("Using existing mng observe process")
        yield
        return

    write_human_line("Starting mng observe in background...")
    process = mng_ctx.concurrency_group.run_process_in_background(
        [sys.executable, "-m", "imbue.mng.main", "observe", "--quiet"],
    )
    try:
        yield
    finally:
        process.terminate()


@click.command()
@add_common_options
@click.pass_context
def watch(ctx: click.Context, **kwargs: object) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="watch",
        command_class=WatchCliOptions,
    )

    plugin_config = _get_plugin_config(mng_ctx)

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

    write_human_line("Watching for agents transitioning to WAITING... (Ctrl+C to stop)")

    with _ensure_observe(mng_ctx):
        try:
            watch_for_waiting_agents(
                mng_ctx=mng_ctx,
                plugin_config=plugin_config,
                notifier=notifier,
            )
        except KeyboardInterrupt:
            logger.debug("Received keyboard interrupt")

    write_human_line("Stopped watching")


CommandHelpMetadata(
    key="watch",
    one_line_description="Watch agents and notify when they transition to WAITING",
    synopsis="mng watch",
    description="""Sends a desktop notification when any agent transitions from RUNNING to WAITING.

Automatically starts `mng observe` in the background if it is not already running.

On macOS, notifications are sent via terminal-notifier (install with:
brew install terminal-notifier). On Linux, via notify-send (libnotify).

To enable click-to-connect (opens a terminal tab running mng connect),
configure the plugin in settings.toml:

    [plugins.notifications]
    terminal_app = "iTerm"

Or use a custom command (MNG_AGENT_NAME is set in the environment):

    [plugins.notifications]
    custom_terminal_command = "my-terminal -e mng connect $MNG_AGENT_NAME"

Press Ctrl+C to stop watching.""",
    examples=(("Watch all agents", "mng watch"),),
    see_also=(
        ("observe", "Stream agent state changes to local event files"),
        ("list", "List agents to see their current state"),
    ),
).register()

add_pager_help_option(watch)
