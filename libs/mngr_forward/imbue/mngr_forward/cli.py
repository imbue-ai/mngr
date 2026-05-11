"""Click entry point for ``mngr forward``."""

import secrets
import signal
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from typing import Final

import click
import uvicorn
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr.utils.parent_process import start_parent_death_watcher
from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.data_types import ForwardListSnapshot
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.errors import ForwardManualConfigError
from imbue.mngr_forward.errors import ForwardSubprocessError
from imbue.mngr_forward.primitives import ForwardPort
from imbue.mngr_forward.primitives import OneTimeCode
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.reverse_handler import ReverseTunnelHandler
from imbue.mngr_forward.server import create_forward_app
from imbue.mngr_forward.snapshot import mngr_list_snapshot
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.stream_manager import ForwardStreamManager

_DEFAULT_HOST: Final[str] = "127.0.0.1"
_DEFAULT_PORT: Final[int] = 8421
_OTP_LENGTH: Final[int] = 32


class ForwardCliOptions(CommonCliOptions):
    """Options for ``mngr forward``. Backed by the click flags below."""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    service: str | None = None
    forward_port: int | None = None
    reverse: tuple[str, ...] = ()
    no_observe: bool = False
    agent_include: tuple[str, ...] = ()
    agent_exclude: tuple[str, ...] = ()
    event_include: tuple[str, ...] = ()
    event_exclude: tuple[str, ...] = ()
    preauth_cookie: str | None = None
    open_browser: bool = False
    allow_host_loopback: bool = False


def _parse_reverse_specs(raw: tuple[str, ...]) -> tuple[ReverseTunnelSpec, ...]:
    parsed: list[ReverseTunnelSpec] = []
    for entry in raw:
        if ":" not in entry:
            raise click.UsageError(f"--reverse expects REMOTE:LOCAL, got {entry!r}")
        remote_str, _, local_str = entry.partition(":")
        try:
            remote = int(remote_str)
            local = int(local_str)
        except ValueError as e:
            raise click.UsageError(f"--reverse {entry!r} contains non-integer ports") from e
        if remote < 0:
            raise click.UsageError(f"--reverse remote port must be >= 0, got {remote}")
        if local <= 0:
            raise click.UsageError(f"--reverse local port must be > 0, got {local}")
        parsed.append(
            ReverseTunnelSpec(
                remote_port=NonNegativeInt(remote),
                local_port=PositiveInt(local),
            )
        )
    return tuple(parsed)


def _resolve_plugin_state_dir(mngr_host_dir: Path) -> Path:
    return mngr_host_dir / "plugin" / "forward"


@click.command(name="forward")
@click.option("--host", default=_DEFAULT_HOST, show_default=True, help="Bind host")
@click.option("--port", default=_DEFAULT_PORT, show_default=True, help="Bind port")
@click.option("--service", default=None, help="Service name to forward (e.g. 'system_interface')")
@click.option(
    "--forward-port",
    "forward_port",
    type=int,
    default=None,
    help="Forward to a fixed remote port on the agent's host (manual mode). Mutually exclusive with --service.",
)
@click.option(
    "--reverse",
    multiple=True,
    help="Reverse tunnel pair REMOTE:LOCAL. Repeatable. REMOTE may be 0 (sshd-assigned).",
)
@click.option(
    "--no-observe",
    is_flag=True,
    default=False,
    help="Do not spawn `mngr observe` / `mngr event`; take a single `mngr list` snapshot instead. Requires --forward-port.",
)
@click.option(
    "--agent-include",
    multiple=True,
    help="CEL expression to include agents (repeatable). Default: include every discovered agent.",
)
@click.option(
    "--agent-exclude",
    multiple=True,
    help="CEL expression to exclude agents (repeatable).",
)
@click.option(
    "--event-include",
    multiple=True,
    help="CEL expression to include `mngr event` source streams (repeatable).",
)
@click.option(
    "--event-exclude",
    multiple=True,
    help="CEL expression to exclude `mngr event` source streams (repeatable).",
)
@click.option(
    "--preauth-cookie",
    default=None,
    envvar="MNGR_FORWARD_PREAUTH_COOKIE",
    help="Pre-shared cookie value accepted in lieu of an OTP-issued cookie.",
)
@click.option(
    "--open-browser/--no-open-browser",
    default=False,
    show_default=True,
    help="Open the printed login URL in the system browser.",
)
@click.option(
    "--allow-host-loopback",
    is_flag=True,
    default=False,
    help=(
        "Permit dialing host loopback (localhost / 127.0.0.0/8 / ::1) when an agent's registered URL "
        "is loopback and no SSH tunnel exists. Off by default: any agent whose SSH info hasn't been "
        "published returns a 502 instead of silently serving whatever else is bound to that port on "
        "the host. Pass this flag only for setups that intentionally run agents directly on the host."
    ),
)
@add_common_options
@click.pass_context
def forward(ctx: click.Context, **kwargs: Any) -> None:
    """Forward web traffic to agents via <agent>.localhost subdomains [experimental]."""
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="forward",
        command_class=ForwardCliOptions,
        is_format_template_supported=False,
    )

    _validate_options(opts)

    start_parent_death_watcher(mngr_ctx.concurrency_group)

    envelope_writer = EnvelopeWriter()

    strategy = _build_strategy(opts)
    resolver = ForwardResolver(strategy=strategy)
    tunnel_manager = SSHTunnelManager()

    reverse_specs = _parse_reverse_specs(opts.reverse)
    reverse_handler = ReverseTunnelHandler(
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        specs=reverse_specs,
    )

    if opts.no_observe:
        kept = _seed_resolver_from_snapshot(
            resolver=resolver,
            reverse_handler=reverse_handler,
            opts=opts,
            require_non_empty=True,
        )
        del kept  # used internally by the helper
        stream_manager: ForwardStreamManager | None = None
    else:
        stream_manager = ForwardStreamManager(
            resolver=resolver,
            envelope_writer=envelope_writer,
            agent_include=tuple(opts.agent_include),
            agent_exclude=tuple(opts.agent_exclude),
            event_include=tuple(opts.event_include),
            event_exclude=tuple(opts.event_exclude),
        )
        if reverse_specs:
            stream_manager.add_on_agent_discovered_callback(reverse_handler)
        stream_manager.start()

    if reverse_specs:
        tunnel_manager.start_reverse_tunnel_health_check()

    plugin_state_dir = _resolve_plugin_state_dir(_resolve_mngr_host_dir(mngr_ctx))
    auth_store = FileAuthStore(data_directory=plugin_state_dir)

    one_time_code = OneTimeCode(secrets.token_urlsafe(_OTP_LENGTH))
    auth_store.add_one_time_code(code=one_time_code)
    login_host = "localhost" if opts.host in {"127.0.0.1", "0.0.0.0", "::1", "::"} else opts.host
    login_url = f"http://{login_host}:{opts.port}/login?one_time_code={one_time_code}"

    logger.info("Login URL (one-time use): {}", login_url)
    envelope_writer.emit_login_url(login_url)

    if opts.open_browser:
        threading.Thread(
            target=_sleep_then_open_browser,
            args=(login_url,),
            daemon=True,
            name="open-browser",
        ).start()

    _install_sighup_handler(stream_manager, opts, resolver, reverse_handler, mngr_ctx.concurrency_group)

    listen_port = ForwardPort(opts.port)

    def _on_listening() -> None:
        envelope_writer.emit_listening(host=opts.host, port=listen_port)

    app = create_forward_app(
        auth_store=auth_store,
        resolver=resolver,
        tunnel_manager=tunnel_manager,
        envelope_writer=envelope_writer,
        listen_host=opts.host,
        listen_port=listen_port,
        preauth_cookie_value=opts.preauth_cookie,
        on_listening=_on_listening,
        allow_host_loopback=opts.allow_host_loopback,
    )

    try:
        uvicorn.run(
            app,
            host=opts.host,
            port=opts.port,
            timeout_graceful_shutdown=1,
            log_level="warning",
        )
    finally:
        if stream_manager is not None:
            stream_manager.stop()
        tunnel_manager.cleanup()
        envelope_writer.close()


def _validate_options(opts: ForwardCliOptions) -> None:
    if opts.service is None and opts.forward_port is None:
        raise click.UsageError("Exactly one of --service NAME or --forward-port REMOTE_PORT is required.")
    if opts.service is not None and opts.forward_port is not None:
        raise click.UsageError("--service and --forward-port are mutually exclusive.")
    if opts.no_observe and opts.service is not None:
        # Spec calls this a "CLI usage error" — use click.UsageError for
        # consistency with the other mutex checks above.
        raise click.UsageError(
            "--no-observe is only valid with --forward-port REMOTE_PORT (service URLs are not in `mngr list` output)."
        )


def _build_strategy(opts: ForwardCliOptions) -> ForwardServiceStrategy | ForwardPortStrategy:
    if opts.service is not None:
        return ForwardServiceStrategy(service_name=opts.service)
    assert opts.forward_port is not None  # validated above
    return ForwardPortStrategy(remote_port=PositiveInt(opts.forward_port))


def _filter_snapshot(
    snapshot: ForwardListSnapshot,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> ForwardListSnapshot:
    """Apply CEL include/exclude filters to a `mngr list` snapshot.

    The CEL context shape matches ``ForwardStreamManager._agent_passes_filter``
    so the same ``--agent-include`` / ``--agent-exclude`` expressions evaluate
    identically in both observe and ``--no-observe`` modes.
    """
    if not include and not exclude:
        return snapshot
    compiled_includes, compiled_excludes = compile_cel_filters(list(include), list(exclude))
    kept = []
    for entry in snapshot.agents:
        context = {
            "agent": {
                "id": str(entry.agent_id),
                "name": entry.agent_name,
                "host_id": entry.host_id,
                "provider_name": entry.provider_name,
                "labels": dict(entry.labels),
            }
        }
        if apply_cel_filters_to_context(
            context=context,
            include_filters=compiled_includes,
            exclude_filters=compiled_excludes,
            error_context_description=f"agent {entry.agent_id}",
        ):
            kept.append(entry)
    return ForwardListSnapshot(agents=tuple(kept))


def _resolve_mngr_host_dir(mngr_ctx: MngrContext) -> Path:
    """Resolve the mngr host dir from the CLI context.

    ``MngrContext.config.default_host_dir`` is always populated by
    ``setup_command_context``; we just expand ``~`` if present.
    """
    return mngr_ctx.config.default_host_dir.expanduser()


def _seed_resolver_from_snapshot(
    resolver: ForwardResolver,
    reverse_handler: ReverseTunnelHandler,
    opts: "ForwardCliOptions",
    require_non_empty: bool,
) -> ForwardListSnapshot:
    """Run ``mngr list`` once and seed the resolver + reverse handler.

    Used both at startup (``require_non_empty=True``: raises
    ``ForwardManualConfigError`` if the post-filter snapshot is empty) and
    on ``SIGHUP`` (``require_non_empty=False``: keeps the previous set on an
    empty snapshot, treating it as a transient).
    """
    snapshot = mngr_list_snapshot()
    kept = _filter_snapshot(snapshot, opts.agent_include, opts.agent_exclude)
    if not kept.agents:
        if require_non_empty:
            raise ForwardManualConfigError(
                "`mngr list` returned no matching agents in --no-observe mode; nothing to forward."
            )
        logger.warning("SIGHUP re-snapshot returned no agents; keeping previous set rather than emptying.")
        return kept
    agent_ids = tuple(entry.agent_id for entry in kept.agents)
    resolver.update_known_agents(agent_ids)
    for entry in kept.agents:
        if entry.ssh_info is not None:
            resolver.update_ssh_info(entry.agent_id, entry.ssh_info)
    reverse_handler.setup_for_snapshot(
        tuple((entry.agent_id, entry.ssh_info) for entry in kept.agents if entry.ssh_info is not None)
    )
    return kept


def _install_sighup_handler(
    stream_manager: ForwardStreamManager | None,
    opts: ForwardCliOptions,
    resolver: ForwardResolver,
    reverse_handler: ReverseTunnelHandler,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Install a SIGHUP handler that bounces observe (or re-snapshots in --no-observe mode).

    The signal handler itself just sets a threading.Event; a watcher thread
    consumes it and dispatches off the signal-handling thread (paramiko /
    FastAPI state are not re-entrant safe).
    """
    bounce_event = threading.Event()

    def _on_sighup(signum: int, frame: object) -> None:
        del signum, frame
        bounce_event.set()

    try:
        signal.signal(signal.SIGHUP, _on_sighup)
    except (ValueError, OSError) as e:
        logger.debug("Could not install SIGHUP handler: {}", e)
        return

    def _watcher() -> None:
        while True:
            bounce_event.wait()
            bounce_event.clear()
            try:
                if stream_manager is not None:
                    stream_manager.bounce_observe()
                else:
                    _resnapshot_no_observe(resolver, reverse_handler, opts)
            except (OSError, RuntimeError) as e:
                logger.warning("SIGHUP dispatch failed: {}", e)

    concurrency_group.start_new_thread(
        target=_watcher,
        daemon=True,
        name="mngr-forward-sighup-watcher",
        is_checked=False,
    )


def _resnapshot_no_observe(
    resolver: ForwardResolver,
    reverse_handler: ReverseTunnelHandler,
    opts: ForwardCliOptions,
) -> None:
    """Re-run `mngr list` snapshot in --no-observe mode after SIGHUP.

    Delegates to ``_seed_resolver_from_snapshot`` with
    ``require_non_empty=False`` so an empty snapshot is treated as transient
    (the spec says startup-empty is fatal but mid-flight-empty is not).
    """
    try:
        _seed_resolver_from_snapshot(
            resolver=resolver,
            reverse_handler=reverse_handler,
            opts=opts,
            require_non_empty=False,
        )
    except (ForwardSubprocessError, OSError, subprocess.SubprocessError) as e:
        logger.warning("SIGHUP re-snapshot failed: {}", e)


def _sleep_then_open_browser(url: str, delay: float = 1.0) -> None:
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except (OSError, RuntimeError) as e:
        logger.debug("Could not open browser: {}", e)


CommandHelpMetadata(
    key="forward",
    one_line_description="Forward web traffic to agents via <agent>.localhost subdomains [experimental]",
    synopsis="mngr forward [--service NAME | --forward-port REMOTE_PORT] [OPTIONS]",
    description="""Runs a local HTTP/WS proxy that serves
``<agent-id>.localhost:<port>/*`` and byte-forwards each request to the
configured backend (a service URL discovered via ``mngr observe``/``mngr event``,
or a fixed remote port). Remote agents are reached via SSH tunnels.

Authentication uses a one-time login URL printed on stderr; in subprocess
mode the same URL is also emitted on stdout as a JSONL ``login_url`` event.
Browser sessions survive SIGHUP-driven observe restarts because the cookie
signing key is persisted to disk under ``$MNGR_HOST_DIR/plugin/forward/``.""",
    examples=(
        ("Forward system_interface for every workspace agent", "mngr forward --service system_interface"),
        ("Manual mode against a fixed port", "mngr forward --no-observe --forward-port 8080"),
        ("Set up reverse tunnels", "mngr forward --service system_interface --reverse 8420:8420"),
        (
            "Filter to a single label set",
            "mngr forward --service system_interface --agent-include 'has(agent.labels.workspace)'",
        ),
    ),
).register()

add_pager_help_option(forward)
