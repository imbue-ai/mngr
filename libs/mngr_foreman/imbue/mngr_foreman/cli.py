"""Click entry point for ``mngr foreman`` -- the always-on web remote-control server."""

import sys
from pathlib import Path
from typing import Any
from typing import Final

import click
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_stderr_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.parent_process import start_parent_death_watcher
from imbue.mngr_foreman import daemon
from imbue.mngr_foreman.assets import ensure_assets
from imbue.mngr_foreman.assets import get_asset_dir
from imbue.mngr_foreman.assets import get_state_dir
from imbue.mngr_foreman.config import ForemanPluginConfig
from imbue.mngr_foreman.mngr_bin import resolve_mngr_binary
from imbue.mngr_foreman.server import run_server
from imbue.mngr_foreman.shortcuts import ShortcutStore
from imbue.mngr_foreman.systemd_service import SERVICE_NAME
from imbue.mngr_foreman.systemd_service import ServiceInstallError
from imbue.mngr_foreman.systemd_service import install_service
from imbue.mngr_foreman.systemd_service import uninstall_service

# ``--log-file`` is already a common mngr option (mngr's own structured JSON log),
# so the backgrounded server's stdout+stderr capture uses ``--foreman-log-file``.
# Keep the tilde literal (not Path.home()) so the value is machine-independent in
# the generated CLI docs; it is expanded with .expanduser() at use time.
_DEFAULT_LOG_FILE: Final[Path] = Path("~/.mngr/foreman.log")
_DEFAULT_PID_FILE: Final[Path] = Path("~/.mngr/foreman.pid")


class ForemanCliOptions(CommonCliOptions):
    """Options for ``mngr foreman``. Backed by the click flags below."""

    host: str | None = None
    port: int | None = None
    background: bool = False
    foreman_log_file: Path = _DEFAULT_LOG_FILE
    pid_file: Path = _DEFAULT_PID_FILE


class ForemanInstallCliOptions(CommonCliOptions):
    """Options for ``mngr foreman install``. Backed by the click flags below."""

    host: str | None = None
    port: int | None = None


def _resolve_foreman_config(mngr_ctx: Any) -> ForemanPluginConfig:
    """Pull the merged ``[plugins.foreman]`` config, falling back to defaults."""
    plugins = getattr(mngr_ctx.config, "plugins", {}) or {}
    raw = plugins.get("foreman")
    if isinstance(raw, ForemanPluginConfig):
        return raw
    if isinstance(raw, dict):
        return ForemanPluginConfig.model_validate(raw)
    return ForemanPluginConfig()


def _run_in_background(
    ctx: click.Context,
    mngr_ctx: MngrContext,
    host: str,
    port: int,
    log_file: Path,
    pid_file: Path,
) -> None:
    """Launch the foreman server as a detached daemon and return immediately.

    Guards against a second copy via ``pid_file``, ensures the frontend asset
    cache in *this* (foreground) process so a slow or failing first-run fetch is
    visible on the terminal rather than buried in the daemon's log, then re-execs
    ``mngr foreman`` fully detached (see ``daemon.spawn_detached_foreman``).
    """
    running_pid = daemon.read_running_daemon_pid(pid_file)
    if running_pid is not None:
        write_stderr_line(f"foreman already running (PID {running_pid})")
        ctx.exit(1)

    # Fetch the pinned frontend libs here (parent) so first-run download failures
    # surface on the terminal. The daemon's own ensure_assets is then a cache hit.
    ensure_assets(get_asset_dir(mngr_ctx))

    child_pid = daemon.spawn_detached_foreman(
        mngr_binary=resolve_mngr_binary(),
        forwarded_args=tuple(sys.argv[1:]),
        log_file=log_file,
    )
    daemon.write_pid_file(pid_file, child_pid)
    write_human_line(
        f"foreman started (PID {child_pid}) on http://{host}:{port}/  — logs: {log_file}  stop: kill {child_pid}"
    )


@click.group(name="foreman", invoke_without_command=True)
@click.option("--host", default=None, help="Bind host (default from config, else 0.0.0.0).")
@click.option("--port", type=int, default=None, help="Bind port (default from config, else 8700).")
@click.option(
    "--background",
    "-d",
    is_flag=True,
    default=False,
    help="Run the server detached in the background (daemonize) and return immediately.",
)
@click.option(
    "--foreman-log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_LOG_FILE,
    show_default=True,
    help="With --background: file the daemon's stdout+stderr are redirected to.",
)
@click.option(
    "--pid-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_PID_FILE,
    show_default=True,
    help="With --background: pid-file for the daemon (guards against a second copy).",
)
@add_common_options
@click.pass_context
def foreman(ctx: click.Context, **kwargs: Any) -> None:
    """Always-on web remote control for your mngr agents [experimental].

    Runs a single Flask server on this box, over every agent in mngr's view:
    a chat UI for claude agents (live transcript + send) and a web terminal for
    any agent type, drivable from any device (including a phone). No code is
    deployed to target boxes and there is no auth -- bind to a tailnet IP or
    firewall the port. Create agents with plain ``mngr create``.

    Bare ``mngr foreman`` runs the server in the foreground. ``mngr foreman
    install`` registers it as a systemd service (recommended for a box);
    ``mngr foreman uninstall`` removes that service. Pass ``-d``/``--background``
    to detach a one-off server without systemd; it prints its PID and returns.

    \b
    Examples:
      mngr foreman --port 8700
      mngr foreman install --host 0.0.0.0 --port 8700
      mngr foreman -d --port 8700
    """
    # With invoke_without_command the group body runs for `install`/`uninstall` too;
    # those subcommands set up their own context and do the work, so return early.
    if ctx.invoked_subcommand is not None:
        return

    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="foreman",
        command_class=ForemanCliOptions,
        is_format_template_supported=False,
    )

    config = _resolve_foreman_config(mngr_ctx)
    host = opts.host if opts.host is not None else config.host
    port = opts.port if opts.port is not None else config.port

    # The daemon child re-runs this same command (with -d still present) under the
    # MNGR_FOREMAN_DAEMONIZED marker: it must NOT re-daemonize, and it must skip
    # the parent-death watcher (a daemon is meant to outlive its launcher, whose
    # exit would otherwise trip the getppid()-change kill).
    if opts.background and not daemon.is_daemon_child():
        _run_in_background(
            ctx=ctx,
            mngr_ctx=mngr_ctx,
            host=host,
            port=port,
            log_file=opts.foreman_log_file.expanduser(),
            pid_file=opts.pid_file.expanduser(),
        )
        return

    if not daemon.is_daemon_child():
        start_parent_death_watcher(mngr_ctx.concurrency_group)

    logger.info(
        "Starting foreman server on http://{}:{} (max_tool_output_chars={})",
        host,
        port,
        config.max_tool_output_chars,
    )

    run_server(
        mngr_ctx=mngr_ctx,
        host=host,
        port=port,
        max_tool_output_chars=config.max_tool_output_chars,
    )


@foreman.command(name="install")
@click.option("--host", default=None, help="Bind host for the service (default from config, else 0.0.0.0).")
@click.option("--port", type=int, default=None, help="Bind port for the service (default from config, else 8700).")
@add_common_options
@click.pass_context
def foreman_install(ctx: click.Context, **kwargs: Any) -> None:
    """Install foreman as a systemd service and enable+start it.

    Writes ``/etc/systemd/system/foreman.service`` (Type=simple, Restart=always,
    ExecStart pointing at this mngr binary), then ``systemctl daemon-reload`` and
    ``systemctl enable --now foreman``. Needs root: run as root or as a sudo-capable
    user (the privileged steps are re-invoked via ``sudo``). Idempotent -- re-running
    rewrites the unit and restarts cleanly. After this, control foreman with
    ``systemctl {start,stop,restart,status} foreman``.
    """
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="foreman install",
        command_class=ForemanInstallCliOptions,
        is_format_template_supported=False,
    )
    config = _resolve_foreman_config(mngr_ctx)
    host = opts.host if opts.host is not None else config.host
    port = opts.port if opts.port is not None else config.port
    try:
        install_service(host, port)
    except ServiceInstallError as e:
        write_stderr_line(f"foreman install failed: {e}")
        ctx.exit(1)
    write_human_line(f"foreman installed as a systemd service ({SERVICE_NAME}).")
    write_human_line(f"  URL:      http://{host}:{port}/")
    write_human_line(f"  control:  sudo systemctl status|start|stop|restart {SERVICE_NAME}")
    write_human_line(f"  logs:     sudo journalctl -u {SERVICE_NAME} -f")


@foreman.command(name="uninstall")
@add_common_options
@click.pass_context
def foreman_uninstall(ctx: click.Context, **kwargs: Any) -> None:
    """Stop, disable, and remove the foreman systemd service (idempotent)."""
    setup_command_context(
        ctx=ctx,
        command_name="foreman uninstall",
        command_class=CommonCliOptions,
        is_format_template_supported=False,
    )
    try:
        removed = uninstall_service()
    except ServiceInstallError as e:
        write_stderr_line(f"foreman uninstall failed: {e}")
        ctx.exit(1)
    if removed:
        write_human_line(f"foreman systemd service ({SERVICE_NAME}) stopped, disabled, and removed.")
    else:
        write_human_line(f"foreman systemd service ({SERVICE_NAME}) was not installed; nothing to do.")


def _shortcut_store(ctx: click.Context, command_name: str) -> ShortcutStore:
    """Open the foreman-local shortcut store for a CLI command."""
    mngr_ctx, _output_opts, _opts = setup_command_context(
        ctx=ctx,
        command_name=command_name,
        command_class=CommonCliOptions,
        is_format_template_supported=False,
    )
    return ShortcutStore(get_state_dir(mngr_ctx) / "shortcuts.json")


@foreman.command(name="set-shortcut")
@click.argument("name")
@click.argument("cmd")
@add_common_options
@click.pass_context
def foreman_set_shortcut(ctx: click.Context, name: str, cmd: str, **kwargs: Any) -> None:
    """Add / update a front-page shortcut button that runs CMD.

    Clicking the button on the home page opens a terminal tab in ``~`` running CMD,
    with whatever you type into the shortcut's args box appended. Quote CMD so the
    shell passes it as one argument, e.g.

    \b
      mngr foreman set-shortcut minds 'bash ~/create-minds-agent.sh'
    """
    _shortcut_store(ctx, "foreman set-shortcut").set(name, cmd)
    write_human_line(f"shortcut {name!r} -> {cmd}")


@foreman.command(name="rm-shortcut")
@click.argument("name")
@add_common_options
@click.pass_context
def foreman_rm_shortcut(ctx: click.Context, name: str, **kwargs: Any) -> None:
    """Remove a front-page shortcut by NAME."""
    if _shortcut_store(ctx, "foreman rm-shortcut").remove(name):
        write_human_line(f"removed shortcut {name!r}")
    else:
        write_human_line(f"no shortcut named {name!r}")


@foreman.command(name="list-shortcuts")
@add_common_options
@click.pass_context
def foreman_list_shortcuts(ctx: click.Context, **kwargs: Any) -> None:
    """List the front-page shortcuts."""
    entries = _shortcut_store(ctx, "foreman list-shortcuts").list()
    if not entries:
        write_human_line("no shortcuts set")
        return
    for entry in entries:
        write_human_line(f"{entry['name']}\t{entry['cmd']}")


CommandHelpMetadata(
    key="foreman",
    one_line_description="Always-on web remote control for your mngr agents [experimental]",
    synopsis="mngr foreman [--host HOST] [--port PORT] [-d] [OPTIONS] | mngr foreman install|uninstall",
    description="""Runs a single Flask server on this box, over every agent in
mngr's view: a mobile-friendly chat UI for claude agents (live transcript with
markdown, syntax highlighting, KaTeX, mermaid, inline images and file uploads;
send messages; interrupt) and a web terminal (xterm.js over a pty bridge) for
any agent type.

No code is deployed to target boxes and there is no auth by design -- bind to a
tailnet IP or firewall the port. Create agents with plain ``mngr create``;
there is no foreman-specific create command or label filter.

Pass ``-d``/``--background`` to run detached in the background: the command
prints the server's PID and returns immediately while the server keeps serving.
Stop it with ``kill <pid>`` (also written to ``--pid-file``); its stdout+stderr
go to ``--foreman-log-file``.""",
    examples=(
        ("Serve on the default port", "mngr foreman --port 8700"),
        ("Bind to a specific tailnet IP", "mngr foreman --host 100.64.0.1"),
        ("Run detached in the background", "mngr foreman -d --port 8700"),
    ),
).register()

add_pager_help_option(foreman)
