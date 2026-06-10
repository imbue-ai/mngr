"""Click entry points for ``mngr latchkey``.

Three subcommands wire the standalone-CLI workflow:

* ``create-agent-env`` -- Wraps
  :func:`prepare_agent_latchkey` and emits the resulting env vars +
  opaque permissions handle path as a single JSON object on stdout.
* ``link-permissions`` -- one-shot. Wraps
  :func:`finalize_host_permissions` to swing the opaque handle's
  symlink to the canonical host-keyed permissions path.
* ``forward`` -- long-running. Drives ``mngr observe`` and wires every
  discovered agent into :class:`LatchkeyDiscoveryHandler` /
  :class:`LatchkeyDestructionHandler`. Stops the shared gateway on
  shutdown (coupled-lifetime semantics, per the spec).

All three resolve their per-invocation settings (the latchkey root
directory and the path to the upstream ``latchkey`` binary) via the
same documented precedence chain: CLI flag > env var > ``[plugins.latchkey]``
in ``settings.toml`` > built-in default. Any :class:`LatchkeyError` /
:class:`LatchkeyStoreError` raised by the underlying library is
surfaced as a non-zero exit (no ``--allow-degraded`` mode).
"""

import os
import signal
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import click
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import PluginName
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_latchkey.agent_setup import finalize_host_permissions
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.config import LatchkeyPluginConfig
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.discovery import LatchkeyDestructionHandler
from imbue.mngr_latchkey.discovery import LatchkeyDiscoveryHandler
from imbue.mngr_latchkey.discovery_stream import DiscoveryStreamConsumer
from imbue.mngr_latchkey.forward_supervisor import is_forward_info_alive
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import delete_forward_info
from imbue.mngr_latchkey.store import load_forward_info
from imbue.mngr_latchkey.store import save_forward_info
from imbue.mngr_latchkey.store import update_forward_info_gateway_port

# Env-var overrides for the two resolved settings; documented in the
# CLI help and in the spec.
ENV_LATCHKEY_DIRECTORY: Final[str] = "MNGR_LATCHKEY_DIRECTORY"
ENV_LATCHKEY_BINARY: Final[str] = "MNGR_LATCHKEY_BINARY"

# Built-in fallback for the latchkey root directory when no override is
# supplied via CLI flag, env var, or ``settings.toml``.
_DEFAULT_LATCHKEY_DIRECTORY: Final[Path] = Path("~/.mngr/latchkey")

# Plugin-config registry key. Must match the name passed to
# ``register_plugin_config`` in :mod:`imbue.mngr_latchkey.plugin`.
_LATCHKEY_PLUGIN_NAME: Final[str] = "latchkey"


class _LatchkeyCommonCliOptions(CommonCliOptions):
    """Shared command options for every ``mngr latchkey`` subcommand."""

    latchkey_directory: str | None = None
    latchkey_binary: str | None = None


class _CreateAgentEnvCliOptions(_LatchkeyCommonCliOptions):
    """Backing options object for ``mngr latchkey create-agent-env``."""


class _AdminJwtCliOptions(_LatchkeyCommonCliOptions):
    """Backing options object for ``mngr latchkey admin-jwt``."""


class _GatewayInfoCliOptions(_LatchkeyCommonCliOptions):
    """Backing options object for ``mngr latchkey gateway-info``."""


class _LinkPermissionsCliOptions(_LatchkeyCommonCliOptions):
    """Backing options object for ``mngr latchkey link-permissions``."""

    host_id: str
    opaque_path: str


class _RegisterAgentCliOptions(_LatchkeyCommonCliOptions):
    """Backing options object for ``mngr latchkey register-agent``."""

    host_id: str
    agent_id: str


class _ForwardCliOptions(_LatchkeyCommonCliOptions):
    """Backing options object for ``mngr latchkey forward``."""

    mngr_binary: str = "mngr"


def _add_common_latchkey_options(command: click.Command) -> click.Command:
    """Attach the two shared ``--latchkey-*`` flags to a subcommand.

    Centralized so the precedence chain (CLI > env > settings > default)
    has exactly one definition site. Both flags accept ``None`` and we
    do the env-var / settings.toml fallback in :func:`_resolve_latchkey_settings`.
    """
    command = click.option(
        "--latchkey-binary",
        "latchkey_binary",
        default=None,
        help=(
            "Path to the upstream ``latchkey`` CLI. "
            f"Falls back to ${ENV_LATCHKEY_BINARY}, then ``[plugins.latchkey].latchkey_binary`` "
            f"in settings.toml, then {LATCHKEY_BINARY!r} on PATH."
        ),
    )(command)
    command = click.option(
        "--latchkey-directory",
        "latchkey_directory",
        default=None,
        type=click.Path(),
        help=(
            "Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` "
            f"metadata subtree. Falls back to ${ENV_LATCHKEY_DIRECTORY}, then "
            f"``[plugins.latchkey].directory`` in settings.toml, then {str(_DEFAULT_LATCHKEY_DIRECTORY)!r}."
        ),
    )(command)
    return command


def _resolve_latchkey_settings(
    mngr_ctx: MngrContext,
    cli_directory: str | None,
    cli_binary: str | None,
) -> tuple[Path, str]:
    """Resolve the (directory, binary) pair using the documented precedence chain.

    Order, highest to lowest: CLI flag > env var > ``[plugins.latchkey]``
    in ``settings.toml`` > built-in default. The plugin config is read
    off ``mngr_ctx.config.plugins[PluginName("latchkey")]`` and is
    expected to be a :class:`LatchkeyPluginConfig` (the registry
    parses it as one); when the user has no such section, the entry is
    either absent or a base :class:`PluginConfig`, both of which we
    treat as "no settings.toml value".
    """
    plugin_config = mngr_ctx.config.plugins.get(PluginName(_LATCHKEY_PLUGIN_NAME))
    settings_directory: Path | None = None
    settings_binary: str | None = None
    if isinstance(plugin_config, LatchkeyPluginConfig):
        settings_directory = plugin_config.directory
        settings_binary = plugin_config.latchkey_binary

    env_directory = os.environ.get(ENV_LATCHKEY_DIRECTORY)
    env_binary = os.environ.get(ENV_LATCHKEY_BINARY)

    if cli_directory is not None:
        resolved_directory = Path(cli_directory).expanduser()
    elif env_directory:
        resolved_directory = Path(env_directory).expanduser()
    elif settings_directory is not None:
        resolved_directory = settings_directory.expanduser()
    else:
        resolved_directory = _DEFAULT_LATCHKEY_DIRECTORY.expanduser()

    if cli_binary is not None:
        resolved_binary = cli_binary
    elif env_binary:
        resolved_binary = env_binary
    elif settings_binary is not None:
        resolved_binary = settings_binary
    else:
        resolved_binary = LATCHKEY_BINARY

    return resolved_directory, resolved_binary


def _build_initialized_latchkey(
    mngr_ctx: MngrContext,
    cli_directory: str | None,
    cli_binary: str | None,
) -> Latchkey:
    """Build a :class:`Latchkey`, run ``initialize()``, translate failures to ``ClickException``.

    The per-directory encryption key is loaded lazily by ``Latchkey``
    itself on every subprocess spawn (see :meth:`Latchkey._load_encryption_key`).
    ``initialize()`` triggers the first such load via ``latchkey --version``,
    so a missing-or-corrupt key file surfaces here as a
    :class:`LatchkeyError` and is translated to ``ClickException`` below.
    """
    directory, binary = _resolve_latchkey_settings(mngr_ctx, cli_directory, cli_binary)
    latchkey = Latchkey(latchkey_binary=binary, latchkey_directory=directory)
    try:
        latchkey.initialize()
    except LatchkeyError as e:
        # Surface the underlying failure verbatim. ``ClickException``
        # turns this into a clean non-zero exit with the message on
        # stderr, matching the spec's hard-fail policy.
        raise click.ClickException(f"latchkey initialization failed: {e}") from e
    return latchkey


# =============================================================================
# Subcommand: create-agent-env
# =============================================================================


@click.command(name="create-agent-env")
@add_common_options
@click.pass_context
def _create_agent_env_command(ctx: click.Context, **kwargs: Any) -> None:
    """Emit the LATCHKEY_* env vars for a new agent as JSON on stdout."""
    del kwargs
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="latchkey.create-agent-env",
        command_class=_CreateAgentEnvCliOptions,
        is_format_template_supported=False,
    )

    latchkey = _build_initialized_latchkey(mngr_ctx, opts.latchkey_directory, opts.latchkey_binary)

    try:
        setup = prepare_agent_latchkey(latchkey, is_tunneled=True)
    except (LatchkeyError, LatchkeyStoreError) as e:
        raise click.ClickException(f"prepare_agent_latchkey failed: {e}") from e

    # ``is_tunneled=True`` with a real Latchkey always produces a
    # non-None opaque path (the library only returns None in the
    # no-Latchkey degraded mode, which we don't reach here). Assert it
    # so the JSON output is always shaped the way the spec promises.
    if setup.opaque_permissions_path is None:
        raise click.ClickException(
            "prepare_agent_latchkey returned no opaque permissions path; this should be unreachable"
        )

    payload = {
        "env": dict(setup.env),
        "opaque_permissions_path": str(setup.opaque_permissions_path),
    }
    # ``write_json_line`` is the project's standard helper for emitting
    # a single JSON object on stdout (used by every ``mngr`` command
    # that supports ``--format=json`` final output).
    write_json_line(payload)


_add_common_latchkey_options(_create_agent_env_command)

CommandHelpMetadata(
    key="latchkey.create-agent-env",
    one_line_description="Emit LATCHKEY_* env vars (+ opaque permissions handle) for a new agent",
    synopsis="mngr latchkey create-agent-env [OPTIONS]",
    description="""Wraps :func:`imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey`
in tunneled mode and emits its result as a single JSON object on stdout:

```
{
  "env": {
    "LATCHKEY_GATEWAY": "...",
    "LATCHKEY_GATEWAY_SECONDARY": "...",
    "LATCHKEY_GATEWAY_PASSWORD": "...",
    "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "...",
    "LATCHKEY_DISABLE_COUNTING": "1"
  },
  "opaque_permissions_path": "..."
}
```

Pipe the ``env`` values into ``mngr create --host-env KEY=VALUE``
so every agent on the host inherits the same gateway wiring, then
call ``mngr latchkey link-permissions`` with the
``opaque_permissions_path`` and the canonical host id once
``mngr create`` returns it. The gateway URL is always the constant
agent-side loopback URL (``http://127.0.0.1:1989``); there is no
on-host (DEV) mode -- a running ``mngr latchkey forward`` is
expected to bridge the agent's loopback port back to the shared
gateway on the desktop.""",
    examples=(
        (
            "Wire env vars into mngr create",
            'eval "$(mngr latchkey create-agent-env | jq -r \'.env | to_entries[] | "--host-env \\(.key)=\\(.value)"\')"',
        ),
    ),
).register()

add_pager_help_option(_create_agent_env_command)


# =============================================================================
# Subcommand: link-permissions
# =============================================================================


@click.command(name="link-permissions")
@click.option(
    "--host-id",
    "host_id",
    required=True,
    help="Canonical host ID returned by ``mngr create``.",
)
@click.option(
    "--opaque-path",
    "opaque_path",
    required=True,
    type=click.Path(),
    help="Opaque permissions handle emitted by ``mngr latchkey create-agent-env``.",
)
@add_common_options
@click.pass_context
def _link_permissions_command(ctx: click.Context, **kwargs: Any) -> None:
    """Replace the opaque permissions handle with a symlink to the canonical host path."""
    del kwargs
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="latchkey.link-permissions",
        command_class=_LinkPermissionsCliOptions,
        is_format_template_supported=False,
    )

    latchkey = _build_initialized_latchkey(mngr_ctx, opts.latchkey_directory, opts.latchkey_binary)

    try:
        host_id = HostId(opts.host_id)
    except ValueError as e:
        raise click.UsageError(f"--host-id is not a valid host ID: {e}") from e

    opaque_path = Path(opts.opaque_path).expanduser()
    if not opaque_path.exists() and not opaque_path.is_symlink():
        raise click.UsageError(f"--opaque-path does not exist: {opaque_path}")

    try:
        finalize_host_permissions(latchkey, opaque_path, host_id)
    except LatchkeyStoreError as e:
        raise click.ClickException(f"finalize_host_permissions failed: {e}") from e

    logger.info("Linked opaque latchkey permissions handle {} to host {}", opaque_path, host_id)


_add_common_latchkey_options(_link_permissions_command)

CommandHelpMetadata(
    key="latchkey.link-permissions",
    one_line_description="Link an opaque permissions handle to a canonical host ID",
    synopsis="mngr latchkey link-permissions --host-id ID --opaque-path PATH [OPTIONS]",
    description="""Wraps :func:`imbue.mngr_latchkey.agent_setup.finalize_host_permissions`.
Idempotent: re-running for the same host preserves prior grants and
discards the freshly-created baseline.""",
    examples=(
        (
            "Finalize permissions for a freshly-created host",
            "mngr latchkey link-permissions --host-id $HOST_ID --opaque-path /path/from/create-agent-env.json",
        ),
    ),
).register()

add_pager_help_option(_link_permissions_command)


# =============================================================================
# Subcommand: register-agent
# =============================================================================


@click.command(name="register-agent")
@click.option(
    "--host-id",
    "host_id",
    required=True,
    help="Canonical host ID the agent runs on. Identifies which per-host permissions file to edit.",
)
@click.option(
    "--agent-id",
    "agent_id",
    required=True,
    help="Canonical agent ID to add to the host's allowed-agent enum.",
)
@add_common_options
@click.pass_context
def _register_agent_command(ctx: click.Context, **kwargs: Any) -> None:
    """Register ``agent_id`` for the host, granting access to ``/minds-api-proxy/api/v1/agents/<agent_id>/...``.

    Wraps :func:`imbue.mngr_latchkey.agent_setup.register_agent_for_host`.
    The default per-host permissions baseline rejects every Minds API
    proxy request with an empty allowed-agent enum; this command
    appends the supplied ``agent_id`` to the enum so the gateway will
    let that agent through to its own ``/api/v1/agents/<id>/...``
    subtree. Idempotent: re-running for an already-registered agent is a
    no-op.
    """
    del kwargs
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="latchkey.register-agent",
        command_class=_RegisterAgentCliOptions,
        is_format_template_supported=False,
    )

    latchkey = _build_initialized_latchkey(mngr_ctx, opts.latchkey_directory, opts.latchkey_binary)

    try:
        host_id = HostId(opts.host_id)
    except ValueError as e:
        raise click.UsageError(f"--host-id is not a valid host ID: {e}") from e
    try:
        agent_id = AgentId(opts.agent_id)
    except ValueError as e:
        raise click.UsageError(f"--agent-id is not a valid agent ID: {e}") from e

    try:
        register_agent_for_host(latchkey.plugin_data_dir, host_id, agent_id)
    except LatchkeyStoreError as e:
        raise click.ClickException(f"register_agent_for_host failed: {e}") from e

    logger.info("Registered agent {} on host {}; access to the Minds API proxy granted", agent_id, host_id)


_add_common_latchkey_options(_register_agent_command)

CommandHelpMetadata(
    key="latchkey.register-agent",
    one_line_description="Register an agent on a host, granting it access to the Minds API proxy",
    synopsis="mngr latchkey register-agent --host-id ID --agent-id ID [OPTIONS]",
    description="""Wraps :func:`imbue.mngr_latchkey.agent_setup.register_agent_for_host`.
The per-host ``latchkey_permissions.json`` ships with an empty
allowed-agent enum on the first rule (the one that gates
``/minds-api-proxy/api/v1/agents/<id>/...``); this command appends
the supplied agent ID to that enum so the gateway will let that
agent through to its own ``/api/v1/agents/<id>/...`` subtree.
Idempotent: re-running for an already-registered agent is a no-op.""",
    examples=(
        (
            "Register an agent for the Minds API proxy",
            "mngr latchkey register-agent --host-id $HOST_ID --agent-id $AGENT_ID",
        ),
    ),
).register()

add_pager_help_option(_register_agent_command)


# =============================================================================
# Subcommand: forward
# =============================================================================


@click.command(name="forward")
@click.option(
    "--mngr-binary",
    "mngr_binary",
    default="mngr",
    show_default=True,
    help="Path to the mngr binary used to spawn the underlying ``mngr observe`` subprocess.",
)
@add_common_options
@click.pass_context
def _forward_command(ctx: click.Context, **kwargs: Any) -> None:
    """Run the shared Latchkey gateway and reverse-tunnel it into every discovered agent."""
    del kwargs
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="latchkey.forward",
        command_class=_ForwardCliOptions,
        is_format_template_supported=False,
    )

    # No parent-death watcher: ``LatchkeyForwardSupervisor`` spawns us
    # detached via ``start_new_session=True`` so we survive embedder
    # restarts (the whole point of the supervisor pattern). Polling
    # ``getppid()`` and SIGTERM'ing ourselves on reparent-to-init would
    # actively defeat that. SIGINT/SIGTERM trigger shutdown; SIGHUP instead
    # bounces only the ``mngr observe`` child (see the signal handlers below)
    # so an embedder can refresh our provider set mid-session without
    # dropping the gateway or any reverse tunnels.
    latchkey = _build_initialized_latchkey(mngr_ctx, opts.latchkey_directory, opts.latchkey_binary)

    # Refuse to start if another forward is already alive for this
    # latchkey directory; two forwards would fight over the same
    # reverse tunnels and produce a confusing stream of failures.
    existing = load_forward_info(latchkey.plugin_data_dir)
    if existing is not None and is_forward_info_alive(existing):
        raise click.ClickException(
            f"Another ``mngr latchkey forward`` is already running for this latchkey directory "
            f"(pid={existing.pid}); refusing to start a second supervisor.",
        )
    if existing is not None:
        logger.info(
            "Discarding stale forward record (pid={}); the previous supervisor is no longer running.",
            existing.pid,
        )

    save_forward_info(
        latchkey.plugin_data_dir,
        LatchkeyForwardInfo(pid=os.getpid(), started_at=datetime.now(timezone.utc)),
    )

    # Eagerly ensure the gateway is up so users see startup failures
    # immediately, not on the first agent discovery. The discovery
    # handler will idempotently re-ensure on every fire. The gateway
    # subprocess is owned by ``mngr_ctx.concurrency_group`` and gets
    # terminated when this command's click context exits.
    try:
        gateway_port = latchkey.start_gateway(mngr_ctx.concurrency_group)
    except LatchkeyError as e:
        raise click.ClickException(f"Failed to start shared Latchkey gateway: {e}") from e
    logger.info(
        "Started shared Latchkey gateway at http://{}:{}",
        latchkey.listen_host,
        gateway_port,
    )

    try:
        update_forward_info_gateway_port(latchkey.plugin_data_dir, gateway_port)
    except LatchkeyStoreError as e:
        raise click.ClickException(f"Failed to publish gateway port: {e}") from e

    tunnel_manager = SSHTunnelManager()
    tunnel_manager.start_reverse_tunnel_health_check()

    discovery_handler = LatchkeyDiscoveryHandler(
        latchkey=latchkey,
        tunnel_manager=tunnel_manager,
        concurrency_group=mngr_ctx.concurrency_group,
        mngr_ctx=mngr_ctx,
    )
    destruction_handler = LatchkeyDestructionHandler(tunnel_manager=tunnel_manager)

    # This forward's `mngr observe --discovery-only` is the single discovery
    # observer for the host dir; it writes the shared default discovery log that
    # minds' `mngr forward --observe-via-file` tails. There is no second observer
    # to isolate from, so it uses the standard event location.
    consumer = DiscoveryStreamConsumer(
        concurrency_group=mngr_ctx.concurrency_group,
        mngr_binary=opts.mngr_binary,
    )
    consumer.add_on_agent_discovered_callback(discovery_handler)
    consumer.add_on_agent_destroyed_callback(destruction_handler)

    shutdown_event = threading.Event()
    bounce_event = threading.Event()
    _install_signal_handlers(shutdown_event, bounce_event)

    # Keep every remote host's VPS credentials/permissions in sync in the
    # background for the lifetime of this supervisor. Passed the shutdown event
    # so the watcher stops cleanly on shutdown -- and, if the watcher itself
    # dies unexpectedly, signals a loud teardown rather than running on with a
    # silently-dead watcher.
    discovery_handler.start_remote_state_sync(mngr_ctx.concurrency_group, shutdown_event)

    consumer.start()
    # Dispatch SIGHUP-driven observe bounces off the signal-handler thread so
    # slow subprocess teardown/respawn never runs in signal context.
    mngr_ctx.concurrency_group.start_new_thread(
        target=_run_sighup_bounce_watcher,
        args=(bounce_event, shutdown_event, consumer),
        name="latchkey-forward-sighup-watcher",
        daemon=True,
        is_checked=False,
    )
    logger.info("Waiting for discovery events; send SIGINT or SIGTERM to shut down, SIGHUP to refresh providers.")
    try:
        # Block until shutdown is signalled. The signal handler sets the
        # event from any thread, including the main thread itself.
        shutdown_event.wait()
    finally:
        logger.info("Shutting down: terminating mngr observe, reverse tunnels, and shared gateway.")
        # Wake the SIGHUP watcher so it observes ``shutdown_event`` and exits;
        # it blocks on ``bounce_event`` and the concurrency group joins it on
        # teardown, so without this nudge a clean shutdown would hang there.
        bounce_event.set()
        consumer.stop()
        tunnel_manager.cleanup()
        try:
            latchkey.stop_gateway()
        except LatchkeyError as e:
            logger.warning("Failed to stop shared Latchkey gateway during shutdown: {}", e)
        delete_forward_info(latchkey.plugin_data_dir)


class _ShutdownSignalHandler(FrozenModel):
    """Callable that flips a shutdown :class:`threading.Event` when a signal fires.

    Defined as a module-level class so the per-process ``shutdown_event``
    binding can be supplied as a frozen field, avoiding both closure-style
    inner functions and partial-application helpers that the project
    ratchets forbid.
    """

    # ``threading.Event`` is not pydantic-native; opt in for this one
    # field. Marked frozen so the binding is fixed at construction time.
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    shutdown_event: threading.Event = Field(
        description="Event flipped to indicate the forward command should shut down.",
    )

    def __call__(self, signum: int, frame: object) -> None:
        del frame
        logger.info("Received signal {}; initiating shutdown.", signum)
        self.shutdown_event.set()


class _BounceSignalHandler(FrozenModel):
    """Callable that flips a bounce :class:`threading.Event` when SIGHUP fires.

    Mirrors :class:`_ShutdownSignalHandler` (module-level frozen callable, no
    closures) but requests an observe bounce rather than a shutdown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    bounce_event: threading.Event = Field(
        description="Event flipped to request a bounce of the mngr observe child.",
    )

    def __call__(self, signum: int, frame: object) -> None:
        del signum, frame
        self.bounce_event.set()


def _run_sighup_bounce_watcher(
    bounce_event: threading.Event,
    shutdown_event: threading.Event,
    consumer: DiscoveryStreamConsumer,
) -> None:
    """Loop until shutdown: on each SIGHUP, bounce the observe child off the signal thread.

    The signal handler only flips ``bounce_event``; this watcher consumes it and
    does the actual subprocess teardown/respawn, keeping that work out of
    signal-handler context (which must stay minimal and re-entrant-safe). The
    loop exits once ``shutdown_event`` is set (checked after each wake), so it
    does not outlive the forward command.
    """
    while not shutdown_event.is_set():
        bounce_event.wait()
        bounce_event.clear()
        if shutdown_event.is_set():
            return
        try:
            consumer.bounce_observe()
        except (OSError, RuntimeError) as e:
            logger.warning("SIGHUP observe bounce failed: {}", e)


def _install_signal_handlers(shutdown_event: threading.Event, bounce_event: threading.Event) -> None:
    """Wire SIGINT / SIGTERM to shutdown and SIGHUP to an observe bounce.

    SIGINT/SIGTERM trigger the coupled-lifetime shutdown path (terminate the
    gateway and reverse tunnels, exit cleanly). SIGHUP no longer shuts the
    forward down: matching ``mngr forward``, it requests a bounce of only the
    ``mngr observe`` child so an embedder (e.g. the minds desktop client, via
    :meth:`LatchkeyForwardSupervisor.bounce`) can refresh our provider set
    mid-session without dropping the gateway or any reverse tunnels.

    ``signal.signal`` only works from the main thread; in pytest's CliRunner /
    threaded harnesses it can raise, so each install is best-effort and logged.
    """
    shutdown_handler = _ShutdownSignalHandler(shutdown_event=shutdown_event)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown_handler)
        except (ValueError, OSError) as e:
            logger.debug("Could not install handler for signal {}: {}", sig, e)
    bounce_handler = _BounceSignalHandler(bounce_event=bounce_event)
    try:
        signal.signal(signal.SIGHUP, bounce_handler)
    except (ValueError, OSError) as e:
        logger.debug("Could not install SIGHUP bounce handler: {}", e)


CommandHelpMetadata(
    key="latchkey.forward",
    one_line_description="Run the shared Latchkey gateway and reverse-tunnel it into every discovered agent",
    synopsis="mngr latchkey forward [OPTIONS]",
    description="""Long-running foreground process that:

1. Initializes the configured ``Latchkey`` (version-checks the binary,
   adopts or discards any pre-existing detached gateway record).
2. Eagerly spawns the shared ``latchkey gateway`` subprocess.
3. Spawns ``mngr observe --discovery-only --quiet`` and, for every
   agent discovered, opens a reverse SSH tunnel that bridges the
   agent's ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` to the host-side
   gateway port. Agents discovered without SSH info are left to
   reach the gateway via whatever direct route exists.
4. On agent destruction, drops that agent's reverse tunnel.
5. On SIGINT/SIGTERM, terminates the observe subprocess, all reverse
   tunnels, *and* the shared gateway. The coupled-lifetime semantics
   are intentional: any agents still alive when this process exits
   will lose their gateway endpoint until the next ``mngr latchkey
   forward`` is started.
6. On SIGHUP, bounces only the ``mngr observe`` child (the gateway and
   every reverse tunnel stay up) so a provider-set change made by an
   embedder takes effect without a full restart.

No filtering flags: every discovered agent gets a tunnel. The plugin
emits stderr-only logs; stdout stays empty for the lifetime of the
process.""",
    examples=(
        ("Run with defaults", "mngr latchkey forward"),
        ("Use a bundled latchkey binary", "mngr latchkey forward --latchkey-binary /opt/latchkey/bin/latchkey"),
    ),
).register()

_add_common_latchkey_options(_forward_command)
add_pager_help_option(_forward_command)


# =============================================================================
# Group
# =============================================================================


@click.group(name="latchkey")
@click.pass_context
def latchkey(ctx: click.Context) -> None:
    """Latchkey gateway lifecycle and per-agent setup [experimental]."""
    del ctx


# =============================================================================
# Subcommand: admin-jwt
# =============================================================================


@click.command(name="admin-jwt")
@add_common_options
@click.pass_context
def _admin_jwt_command(ctx: click.Context, **kwargs: Any) -> None:
    """Print a JWT that grants the bearer wildcard access to the gateway."""
    del kwargs
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="latchkey.admin-jwt",
        command_class=_AdminJwtCliOptions,
        is_format_template_supported=False,
    )

    latchkey = _build_initialized_latchkey(mngr_ctx, opts.latchkey_directory, opts.latchkey_binary)

    try:
        jwt = latchkey.create_admin_permissions_jwt()
    except (LatchkeyError, LatchkeyStoreError) as e:
        raise click.ClickException(f"Failed to mint admin JWT: {e}") from e

    # Emit on stdout via the project-standard helper for user-facing
    # output.
    write_human_line(jwt)


_add_common_latchkey_options(_admin_jwt_command)

CommandHelpMetadata(
    key="latchkey.admin-jwt",
    one_line_description="Mint a wildcard ``permissions-override`` JWT for the shared gateway",
    synopsis="mngr latchkey admin-jwt [OPTIONS]",
    description="""Materializes the admin permissions file at
``<plugin_data_dir>/latchkey_admin_permissions.json`` (idempotent --
an existing file is reused as-is) and mints a JWT signed for that
path via ``latchkey gateway create-jwt --no-validate``. The JWT is
printed on stdout as a single line.

The returned token unlocks every service and every extension
endpoint reachable through the gateway, so treat it like a root
credential and pass it as the
``X-Latchkey-Gateway-Permissions-Override`` header to gateway
requests that need wildcard access (e.g. the minds desktop client
streaming pending permission requests from the
``permission-requests`` extension).""",
    examples=(("Capture into a shell variable", "ADMIN_JWT=$(mngr latchkey admin-jwt)"),),
).register()

add_pager_help_option(_admin_jwt_command)


# =============================================================================
# Subcommand: gateway-info
# =============================================================================


@click.command(name="gateway-info")
@add_common_options
@click.pass_context
def _gateway_info_command(ctx: click.Context, **kwargs: Any) -> None:
    """Print the running shared gateway's URL + password as a single JSON object."""
    del kwargs
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="latchkey.gateway-info",
        command_class=_GatewayInfoCliOptions,
        is_format_template_supported=False,
    )

    latchkey = _build_initialized_latchkey(mngr_ctx, opts.latchkey_directory, opts.latchkey_binary)

    info = load_forward_info(latchkey.plugin_data_dir)
    if info is None or not is_forward_info_alive(info):
        raise click.ClickException(
            "No ``mngr latchkey forward`` supervisor is running for this latchkey directory; "
            "start one with ``mngr latchkey forward`` before asking for its gateway info.",
        )
    if info.gateway_port is None:
        raise click.ClickException(
            "The supervisor is running but has not finished binding its gateway port yet; retry in a moment.",
        )

    try:
        password = latchkey.derive_gateway_password()
    except LatchkeyError as e:
        raise click.ClickException(f"Failed to derive gateway password: {e}") from e

    write_json_line(
        {
            "url": f"http://{latchkey.listen_host}:{info.gateway_port}",
            "password": password,
        },
    )


_add_common_latchkey_options(_gateway_info_command)

CommandHelpMetadata(
    key="latchkey.gateway-info",
    one_line_description="Print the running shared gateway's URL + password as JSON",
    synopsis="mngr latchkey gateway-info [OPTIONS]",
    description="""Reads the supervised ``mngr latchkey forward`` record
to discover the bound gateway port and derives the gateway password
locally (via ``latchkey gateway create-jwt`` against a sentinel path,
the same way :meth:`Latchkey.derive_gateway_password` does in
Python). Emits a single JSON object on stdout:

```
{
  "url": "http://127.0.0.1:32867",
  "password": "<sha256-of-derived-jwt>"
}
```

Fails with a non-zero exit when no supervisor is running for the
active latchkey directory, or when the supervisor has not yet
stamped its bound gateway port onto its on-disk record (i.e. the
supervisor is still in its startup window).

The password is intentionally NOT persisted on disk; this command
is the supported way to retrieve it without writing your own
Python integration.""",
    examples=(
        (
            "Capture into shell variables",
            'eval "$(mngr latchkey gateway-info | jq -r \'@text "GATEWAY_URL=\\(.url); GATEWAY_PASSWORD=\\(.password)"\')"',
        ),
    ),
).register()

add_pager_help_option(_gateway_info_command)


latchkey.add_command(_create_agent_env_command)
latchkey.add_command(_link_permissions_command)
latchkey.add_command(_register_agent_command)
latchkey.add_command(_forward_command)
latchkey.add_command(_admin_jwt_command)
latchkey.add_command(_gateway_info_command)


CommandHelpMetadata(
    key="latchkey",
    one_line_description="Latchkey gateway lifecycle and per-agent setup [experimental]",
    synopsis="mngr latchkey <subcommand> [OPTIONS]",
    description="""Wires the shared Latchkey gateway and per-agent permissions
without requiring the minds desktop app. Run ``mngr latchkey forward``
once at startup, then call ``mngr latchkey create-agent-env`` /
``mngr latchkey link-permissions`` per host.

Settings:

- ``[plugins.latchkey].directory`` (default ``~/.mngr/latchkey``)
- ``[plugins.latchkey].latchkey_binary`` (default ``latchkey`` on PATH)

Both are overridable via the matching ``MNGR_LATCHKEY_*`` env vars and
per-invocation ``--latchkey-directory`` / ``--latchkey-binary`` flags.""",
    examples=(
        ("Inspect available subcommands", "mngr latchkey --help"),
        ("Start the supervisor", "mngr latchkey forward"),
    ),
).register()
