"""``minds run``: spawn ``mngr forward`` and serve the bare-origin minds UI.

Replaces the deleted ``desktop_client/runner.py``. The auth + subdomain-
forwarding logic lives in the ``mngr_forward`` plugin now; this command:

1. Spawns ``mngr forward --service system_interface --preauth-cookie ...`` as
   a subprocess via ``EnvelopeStreamConsumer`` (which feeds the surviving
   ``MngrCliBackendResolver`` from the plugin's envelope stream).
2. Builds the slimmed minds-side bare-origin Flask app and runs it on
   ``--port`` (default 8420).
3. Emits a ``mngr_forward_started`` JSONL event on stdout carrying the
   preauth cookie value, so the Electron shell can pre-set
   ``mngr_forward_session=<value>`` on ``localhost:<mngr-forward-port>``
   before the first agent-subdomain navigation.

Agents reach the Minds API via the latchkey gateway's bundled
``minds-api-proxy`` extension rather than over a per-agent reverse SSH
tunnel; the supervisor handles the reverse SSH tunnel used to expose
the gateway itself into each agent's container.
"""

import os
import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Final

import click
from flask import Flask
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import reconcile_imbue_cloud_providers_from_sessions
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.build_info import resolve_git_sha
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_HOST
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_PORT
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.config.loader import load_client_config
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.app import start_discovery_health_watchdog_loop
from imbue.minds.desktop_client.app import start_system_interface_health_probe_loop
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.discovery_health import SupervisorProducerRemediator
from imbue.minds.desktop_client.forward_cli import ForwardSubprocessConfig
from imbue.minds.desktop_client.forward_cli import start_mngr_forward
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.laptop_agent_types_seed import seed_laptop_agent_types_for_minds
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.file_sharing import FileSharingGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import PermissionRequestsConsumer
from imbue.minds.desktop_client.latchkey_auto_register import LatchkeyAutoRegister
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import LatchkeyPredefinedPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.minds.desktop_client.server import desktop_client_runtime
from imbue.minds.desktop_client.server import serve_desktop_client
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.system_interface_health import should_enroll_suspect_for_backend_failure
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.minds.utils.output import emit_event
from imbue.minds.utils.sentry.core import resolve_sentry_environment
from imbue.minds.utils.sentry.core import setup_sentry
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.parent_process import start_grandparent_death_watcher
from imbue.mngr_latchkey.agent_setup import maybe_recover_host_permissions_for_agent
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyStoreError

# How long `minds run` waits for the spawned `mngr forward` plugin to report
# its bound port via a `listening` envelope before treating startup as failed.
# The plugin emits this from its own server's startup, so on a warm
# install the wait only needs to cover the subprocess's own interpreter
# start and imports. On a cold install (vanilla Mac, no `~/.minds/.venv`),
# uv has to download the python toolchain + install the venv + load
# plugins first; that can take 30-60s on a fresh machine. A 5s budget was
# tight enough to deterministically fail every first-time-user launch on
# a clean Mac (proven via Tart VM). Give it 120s to comfortably cover
# cold-install while still surfacing a real wedge before the user gives up.
_MNGR_FORWARD_LISTEN_TIMEOUT_SECONDS: Final[float] = 120.0

# Env var read by the bundled ``minds-api-proxy`` gateway extension to
# decide where to forward inbound proxy requests. Published to the
# detached ``mngr latchkey forward`` supervisor (and from there to the
# gateway, and from there to the extension) on every minds startup so
# the proxy always points at the current bare-origin port, even when
# minds re-binds to a different port across restarts.
MINDS_API_PROXY_URL_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_MINDS_API_URL"

# Env var read by the bundled ``minds-api-proxy`` gateway extension on
# each request; the proxy injects this value as ``Authorization: Bearer
# <key>`` on every forwarded request. Freshly generated per ``minds
# run`` and never persisted to disk -- the supervisor is restarted on
# every minds startup and gets the current value in its env, the bare-
# origin server sees the same in-memory value, and the agent itself
# never sees the key at all.
MINDS_API_PROXY_KEY_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_MINDS_API_KEY"


@click.command()
@click.option(
    "--host",
    default=DEFAULT_DESKTOP_CLIENT_HOST,
    show_default=True,
    help="Host to bind the minds bare-origin server to",
)
@click.option(
    "--port",
    default=DEFAULT_DESKTOP_CLIENT_PORT,
    show_default=True,
    help="Port to bind the minds bare-origin server to",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Do not open the minds UI in the system browser",
)
@click.option(
    "--config-file",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    envvar="MINDS_CLIENT_CONFIG_PATH",
    help=(
        "Path to the per-env client config TOML. Falls back to the "
        "MINDS_CLIENT_CONFIG_PATH env var (set by `minds env activate <name>`); "
        "no implicit default beyond that. Refuses to start when neither is set "
        '-- run `eval "$(minds env activate <name>)"` first. Bundled Electron '
        "builds pass this flag explicitly from MINDS_CLIENT_CONFIG_BUNDLE."
    ),
)
@click.pass_context
def run(
    ctx: click.Context,
    host: str,
    port: int,
    no_browser: bool,
    config_file: Path | None,
) -> None:
    """Run the minds bare-origin server with `mngr forward` as a subprocess."""
    if config_file is None:
        raise click.ClickException(
            "No client config file is set. Activate an env first: "
            '`eval "$(uv run minds env activate <name>)"` (e.g. '
            "`dev-<your-user>`, `staging`, or `production`), then re-run."
        )
    root_name = resolve_minds_root_name()
    data_directory = minds_data_dir_for(root_name)
    minds_config = MindsConfig(data_dir=data_directory)
    paths = WorkspacePaths(data_dir=data_directory)

    # Initialize Sentry for the minds backend process. ``setup_logging`` already ran
    # in the CLI group callback, so the loguru sinks Sentry layers on top of exist.
    #
    # Sentry always initializes, but what it actually sends is gated live by per-machine user
    # settings (stored in MindsConfig, surfaced via the first-launch consent screen and account
    # settings): ``report_unexpected_errors`` gates automatic error sends, and ``include_error_logs``
    # gates log/traceback attachments. Both are read live, so toggling a setting takes effect without
    # restarting. Manual bug reports are always sent regardless of ``report_unexpected_errors``.
    #
    # The activated minds env (from `minds env activate`) selects the Sentry DSN and, for
    # production/staging, which S3 attachment bucket: production and staging each get their own, while
    # every other env (dev-*, ci-*, or no activated env) reports to the dev project. We treat "not
    # activated" as dev so an un-activated `minds run` never accidentally reports to the production
    # project; development never uploads attachments regardless. The release id (desktop app version)
    # and git sha come from the Electron launcher via env vars, falling back to the in-repo
    # package.json / "unknown" for bare source runs (see imbue.minds.build_info).
    setup_sentry(
        environment=resolve_sentry_environment(),
        release_id=resolve_release_id(),
        git_commit_sha=resolve_git_sha(),
        log_folder=paths.log_dir,
        is_error_reporting_enabled=minds_config.get_report_unexpected_errors,
        is_log_inclusion_enabled=minds_config.get_include_error_logs,
    )
    client_config_path = config_file
    client_env_config = load_client_config(client_config_path)
    connector_url_str = str(client_env_config.connector_url).rstrip("/")
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)

    logger.info("Starting `minds run`...")
    logger.info("  Bare-origin: http://{}:{}", host, port)
    logger.info("  MINDS_ROOT_NAME: {}", root_name)
    logger.info("  Data directory: {}", data_directory)
    logger.info("  Config file: {}", client_config_path)
    logger.info("  connector_url: {}", client_env_config.connector_url)
    logger.info("  litellm_proxy_url: {}", client_env_config.litellm_proxy_url)

    # Bootstrap couldn't write provider entries without the connector URL,
    # so the reconcile happens here once we've loaded the client config.
    reconcile_imbue_cloud_providers_from_sessions(connector_url_str, root_name=root_name)

    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    is_electron = os.getenv("MINDS_ELECTRON") == "1"
    notification_dispatcher = NotificationDispatcher(is_electron=is_electron)
    backend_resolver = MngrCliBackendResolver(
        last_good_agents_path=paths.data_dir / "last_good_agent_topology.json",
    )
    latchkey = _build_latchkey(data_directory=data_directory)
    latchkey.initialize()

    # Mint a fresh central minds API key for this process. The same
    # value is handed to the latchkey gateway's ``minds-api-proxy``
    # extension (via the supervisor restart below, so it can inject
    # ``Authorization: Bearer <key>`` on every forwarded request) and
    # to the desktop client's own bearer-auth gates (so they accept
    # the header the proxy just injected). Generated in memory rather
    # than persisted because the supervisor is always restarted on
    # minds startup and there is no other cross-process consumer.
    minds_api_key = generate_api_key()

    root_concurrency_group = ConcurrencyGroup(name="minds-run")
    root_concurrency_group.__enter__()

    # Spawn (or adopt) a detached ``mngr latchkey forward`` supervisor.
    # The supervisor owns the shared latchkey gateway + per-agent reverse
    # tunnels; running it as a detached subprocess (rather than the inline
    # ``LatchkeyDiscoveryHandler``/``SSHTunnelManager`` wiring that used to
    # live here) means a minds restart adopts the existing instance instead
    # of tearing every tunnel down and re-establishing it. We do *not*
    # terminate it on minds shutdown -- mirroring how minds already leaves
    # the gateway running detached so agents in containers/VMs keep working
    # across desktop-client restarts.
    gateway_client = LatchkeyGatewayClient.from_latchkey(latchkey)

    # Build the supervisor once and keep the handle: the startup restart runs on
    # the background thread below, and the same instance is held in the app state
    # so the provider-change request handlers can ``bounce()`` it mid-session
    # (mirroring the SIGHUP minds already sends its own ``mngr forward`` observe).
    latchkey_forward_supervisor = LatchkeyForwardSupervisor(
        mngr_binary=MNGR_BINARY,
        latchkey_binary=latchkey.latchkey_binary,
        latchkey_directory=latchkey.latchkey_directory,
        # Spawn the detached supervisor (and its `mngr observe` discovery
        # producer grandchild) from $HOME, like every other laptop-side mngr
        # invocation -- notably the `mngr forward` consumer below. Without this
        # it inherits minds' cwd, which in a dev checkout is the monorepo root:
        # its mngr children then load `<repo>/.mngr/settings.toml`, and under
        # the e2e test that trips mngr's pytest config guard so the supervisor
        # never starts. A dead producer means no discovery snapshots, which the
        # discovery-health watchdog escalates to a terminal BLOCKED takeover.
        cwd=Path.home(),
        extra_env={
            MINDS_API_PROXY_URL_ENV_VAR: f"http://127.0.0.1:{port}",
            MINDS_API_PROXY_KEY_ENV_VAR: minds_api_key,
        },
    )

    # Background thread: supervisor restart must complete before the
    # gateway-client pre-warm reads the on-disk forward record, or it
    # caches the previous supervisor's stale port for the rest of the
    # process lifetime.
    root_concurrency_group.start_new_thread(
        _restart_supervisor_then_prewarm_gateway_client,
        args=(latchkey_forward_supervisor, gateway_client),
        name="mngr-latchkey-supervisor-and-gateway-init",
    )

    # Watch our *grandparent* (typically Electron) rather than our immediate
    # parent (the ``uv run`` wrapper, which doesn't propagate Electron's
    # death). When Electron crashes or is killed without running its
    # ``child.on('exit')`` cleanup, this watcher SIGTERMs us so the
    # ``mngr forward`` plugin and its observe / event grandchildren can in
    # turn exit cleanly. Without it, a crashed Electron leaves the entire
    # orphan tree running across restarts.
    start_grandparent_death_watcher(root_concurrency_group)

    # Run ``mngr message`` (and, over time, other ``mngr`` CLI calls) in a
    # pre-warmed, single-use ``mngr`` process instead of spawning (and importing)
    # a fresh interpreter each time, so UI actions like Approve/Deny don't pay the
    # multi-second interpreter+import startup cost. ``prewarm`` is non-blocking:
    # it spawns the first warm process (which pays the import cost) on a
    # background thread, off the request path.
    mngr_caller = get_default_mngr_caller()
    mngr_caller.prewarm(root_concurrency_group)
    mngr_message_sender = MngrMessageSender(mngr_caller=mngr_caller, concurrency_group=root_concurrency_group)
    latchkey_permission_handler = LatchkeyPermissionGrantHandler(
        data_dir=data_directory,
        latchkey=latchkey,
        services_catalog=ServicesCatalog(),
        mngr_message_sender=mngr_message_sender,
        gateway_client=gateway_client,
    )
    file_sharing_handler = FileSharingGrantHandler(
        data_dir=data_directory,
        gateway_client=gateway_client,
        mngr_message_sender=mngr_message_sender,
    )
    imbue_cloud_cli = ImbueCloudCli(
        parent_concurrency_group=root_concurrency_group,
        connector_url=client_env_config.connector_url,
    )
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)
    session_store = MultiAccountSessionStore(data_dir=data_directory, cli=imbue_cloud_cli)
    response_events = load_response_events(data_directory)
    request_inbox = RequestInbox()
    for resp in response_events:
        request_inbox = request_inbox.add_response(resp)

    # Spawn the plugin and attach the envelope consumer that feeds the
    # surviving resolver from the plugin's stdout stream. We no longer
    # ask the plugin to set up a per-agent reverse SSH tunnel for the
    # Minds API: agents reach it through the latchkey gateway's bundled
    # ``minds-api-proxy`` extension instead, so no ``--reverse`` specs
    # are needed here.
    mngr_host_dir_str = os.environ.get("MNGR_HOST_DIR")
    mngr_host_dir = Path(mngr_host_dir_str).expanduser() if mngr_host_dir_str else (Path.home() / ".mngr")
    # `mngr forward` and every other laptop-side mngr invocation (including the
    # bundled mngr CLI when run from a Terminal under this MNGR_HOST_DIR) starts
    # with cwd=$HOME, so the FCT workspace's `[agent_types.main]` block in
    # `/code/.mngr/settings.toml` inside the lima VM is invisible to them.
    # Seed the mapping into user-scope settings.toml here so subsequent mngr
    # subprocesses resolve `type=main` -> ClaudeAgent without depending on cwd.
    seed_laptop_agent_types_for_minds(mngr_host_dir)
    forward_config = ForwardSubprocessConfig(
        mngr_host_dir=mngr_host_dir,
    )
    consumer, preauth_cookie = start_mngr_forward(
        config=forward_config,
        resolver=backend_resolver,
    )

    # App-global discovery-pipeline health watchdog. Detects a stalled pipeline
    # (a producer stall via the resolver's snapshot-freshness age; a consumer
    # death via the lifecycle watcher) and self-heals by re-kicking the producer
    # (supervisor bounce -> restart) before surfacing a terminal app-global
    # BLOCKED screen. The consumer-death callback is wired before
    # ``consumer.start()`` so an early exit is caught.
    discovery_health_watchdog = DiscoveryHealthWatchdog(
        remediator=SupervisorProducerRemediator(supervisor=latchkey_forward_supervisor),
    )
    consumer.add_on_unexpected_exit_callback(lambda _exit_code: discovery_health_watchdog.record_consumer_death())

    # System-interface health tracker: feeds on backend failures observed by
    # the plugin (registered as a callback below) and on the readiness-probe
    # success that ``_wait_for_workspace_ready`` reports through AgentCreator.
    # Constructed here (instead of inside create_desktop_client) so it can
    # be threaded into both AgentCreator (for record_probe_success) and the
    # consumer's failure callback (registered before consumer.start() below;
    # otherwise early failures would dispatch against an empty list).
    system_interface_health_tracker = SystemInterfaceHealthTracker()
    # The plugin reports every non-2xx response; minds decides which ones count.
    # Only connection-level failures and infrastructure 5xx enroll a suspect --
    # application errors are left for the background probe to adjudicate.
    consumer.add_on_system_interface_backend_failure_callback(
        lambda agent_id, _reason, status_code: system_interface_health_tracker.record_failure(agent_id)
        if should_enroll_suspect_for_backend_failure(status_code)
        else None
    )

    # All callbacks registered -- now safe to start the envelope reader
    # threads. Doing this earlier (e.g. inside ``start_mngr_forward``)
    # would open a race window where envelopes arriving before the
    # callbacks were registered would be dispatched against an empty
    # callback list and silently dropped.
    consumer.start(root_concurrency_group)

    # Block until the plugin reports the port it bound. The plugin owns its
    # port: it picks one (its default, or an OS-assigned fallback when the
    # default is taken) and reports it via a ``listening`` envelope.
    # Everything below that needs the port (AgentCreator, the desktop app,
    # the health probe, the Electron ``mngr_forward_started`` event) is
    # built only after this returns.
    mngr_forward_port = consumer.wait_for_listening(timeout=_MNGR_FORWARD_LISTEN_TIMEOUT_SECONDS)
    if mngr_forward_port is None:
        consumer.terminate()
        raise click.ClickException(
            "`mngr forward` did not report a listening port within "
            f"{_MNGR_FORWARD_LISTEN_TIMEOUT_SECONDS:.0f}s; the plugin likely failed to start. "
            "Check the logs above for its stderr and retry."
        )
    logger.info("  mngr forward: http://127.0.0.1:{}", mngr_forward_port)

    # AgentCreator is constructed *after* ``start_mngr_forward`` so the
    # readiness probe can use the same preauth cookie the plugin accepts and
    # Electron pre-sets, and after ``wait_for_listening`` so it has the
    # plugin's actual bound port.
    agent_creator = AgentCreator(
        paths=paths,
        server_port=port,
        imbue_cloud_cli=imbue_cloud_cli,
        latchkey=latchkey,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        system_interface_health_tracker=system_interface_health_tracker,
    )

    # Every newly-discovered agent on a minds-managed host gets
    # its id appended to the host's ``latchkey_permissions.json``
    # allowed-agent list.
    LatchkeyAutoRegister(backend_resolver=backend_resolver, latchkey=latchkey).start()

    # Emit the started event so Electron can pre-set the cookie before the
    # first navigation. ``minds run`` itself does not open the browser at
    # the agent subdomain — it opens the minds bare-origin URL.
    emit_event(
        "mngr_forward_started",
        {
            "preauth_cookie": preauth_cookie,
            "mngr_forward_port": mngr_forward_port,
        },
        output_format,
    )

    # Mint a one-time code for the minds bare-origin auth flow (the plugin
    # uses its own ``mngr_forward_session`` cookie on the agent subdomains).
    code = OneTimeCode(secrets.token_urlsafe(32))
    auth_store.add_one_time_code(code=code)
    minds_login_url = f"http://localhost:{port}/login?one_time_code={code}"
    logger.info("Minds login URL (one-time use): {}", minds_login_url)
    emit_event("login_url", {"login_url": minds_login_url, "message": minds_login_url}, output_format)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        agent_creator=agent_creator,
        imbue_cloud_cli=imbue_cloud_cli,
        telegram_orchestrator=telegram_orchestrator,
        notification_dispatcher=notification_dispatcher,
        paths=paths,
        envelope_stream_consumer=consumer,
        session_store=session_store,
        minds_config=minds_config,
        client_env_config=client_env_config,
        request_inbox=request_inbox,
        request_event_handlers=(latchkey_permission_handler, file_sharing_handler),
        server_port=port,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        output_format=output_format,
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=system_interface_health_tracker,
        mngr_binary=MNGR_BINARY,
        mngr_host_dir=mngr_host_dir,
        minds_api_key=minds_api_key,
        latchkey_forward_supervisor=latchkey_forward_supervisor,
        discovery_health_watchdog=discovery_health_watchdog,
    )

    # Background loop driving the discovery-pipeline watchdog: polls snapshot
    # freshness, runs the producer bounce -> restart remediations on a stall, and
    # transitions the app-global state. Started here (not inside
    # create_desktop_client) so test factories can skip the background thread.
    start_discovery_health_watchdog_loop(
        watchdog=discovery_health_watchdog,
        backend_resolver=backend_resolver,
        root_concurrency_group=root_concurrency_group,
    )

    # Background probe loop: flips STUCK/RESTARTING agents back to HEALTHY
    # once the plugin probe sees a 200. Started here (not inside
    # ``create_desktop_client``) so test factories that build the app can
    # skip the probe thread by simply not calling this function.
    start_system_interface_health_probe_loop(
        tracker=system_interface_health_tracker,
        backend_resolver=backend_resolver,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        root_concurrency_group=root_concurrency_group,
    )

    # Wire the permission-requests streaming consumer once the Flask
    # app is built so the on_request callback can mutate the app state
    # directly. The consumer thread runs for the lifetime of
    # ``root_concurrency_group``.
    permission_requests_consumer = PermissionRequestsConsumer(
        gateway_client=gateway_client,
        on_request=_StreamedPermissionRequestHandler(app=app, backend_resolver=backend_resolver, latchkey=latchkey),
    )
    permission_requests_consumer.start(root_concurrency_group)
    # Stash on the app state so the shutdown teardown can stop() the consumer
    # before draining the root concurrency group; without this the
    # consumer thread stays blocked on its follow-stream read=None socket
    # for the full CG shutdown timeout and the group surfaces a "1 strand
    # did not finish in time" warning on every clean exit.
    get_state(app).permission_requests_consumer = permission_requests_consumer

    if not no_browser:
        # Open the URL that carries the one-time code rather than the bare
        # origin. The bare origin lands on the unauthenticated landing page
        # ("Use the login URL printed in the terminal"), which is useless
        # for the user when we already know the code; navigating to
        # /login?one_time_code=... drops directly into the authenticated
        # session. If the user already has a valid session cookie, the
        # /login handler 307-redirects to / instead of consuming the code,
        # so this is safe across restarts.
        thread = threading.Thread(target=_sleep_then_open, args=(minds_login_url,), daemon=True)
        thread.start()

    # ``desktop_client_runtime`` owns the shared HTTP client + geo-detection
    # startup and the ordered shutdown teardown (close client, terminate
    # consumers, stop the mngr caller, drain the root concurrency group).
    # ``serve_desktop_client`` runs the graceful cheroot server until
    # SIGINT/SIGTERM, flipping ``shutdown_event`` + waking the SSE handlers
    # before the server drains so streams end cleanly with no tracebacks.
    with desktop_client_runtime(get_state(app), is_externally_managed_client=False):
        serve_desktop_client(app, get_state(app), host=host, port=port)


class _StreamedPermissionRequestHandler(FrozenModel):
    """Callable that appends a streamed permission request to the app inbox.

    The handler runs on the permission-requests consumer thread (not a
    request thread), so it only does thread-safe work: appending to the
    immutable :class:`RequestInbox` produces a new instance per mutation
    and Python attribute assignment is atomic for our purposes here. Same
    trick the legacy JSONL ``_handle_request_event_callback`` already uses.
    """

    app: Flask = Field(
        frozen=True,
        description="Desktop-client Flask instance whose state ``request_inbox`` is mutated on receipt.",
    )
    backend_resolver: MngrCliBackendResolver = Field(
        frozen=True,
        description="Resolver whose ``notify_change()`` wakes the chrome SSE so the panel updates promptly.",
    )
    latchkey: Latchkey = Field(
        frozen=True,
        description=(
            "Latchkey instance used to repair a host whose canonical "
            "``latchkey_permissions.json`` is missing when a fresh request arrives."
        ),
    )

    # ``Flask``, ``MngrCliBackendResolver`` and ``Latchkey`` are not
    # pydantic natives; tolerate them with ``arbitrary_types_allowed``.
    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}

    def __call__(self, event: RequestEvent) -> None:
        current: RequestInbox | None = get_state(self.app).request_inbox
        if current is None:
            return
        # The gateway re-emits every still-pending request on each
        # stream reconnect (and the consumer reconnects every couple of
        # seconds when idle, see ``_FOLLOW_READ_TIMEOUT``). Once we've
        # ingested a given ``event_id`` the redeliveries carry no new
        # information, so we no-op rather than append a duplicate to
        # the requests list (it would grow unbounded), log again, and
        # wake the SSE for nothing.
        if current.get_request_by_id(str(event.event_id)) is not None:
            return
        # Repair a host whose canonical permissions file was never
        # materialized *before* surfacing the request, so the user's
        # eventual approval actually takes effect. Best-effort: failures
        # are logged and the request is still surfaced.
        self._maybe_recover_host_permissions(event)
        get_state(self.app).request_inbox = current.add_request(event)
        if isinstance(event, LatchkeyPredefinedPermissionRequestEvent):
            logger.info(
                "Streamed latchkey permission request for agent {} (scope={}, request_id={})",
                event.agent_id,
                event.scope,
                event.event_id,
            )
        elif isinstance(event, LatchkeyFileSharingPermissionRequestEvent):
            logger.info(
                "Streamed file-sharing permission request for agent {} (path={}, request_id={})",
                event.agent_id,
                event.path,
                event.event_id,
            )
        else:
            logger.info(
                "Streamed permission request for agent {} (request_type={}, request_id={})",
                event.agent_id,
                event.request_type,
                event.event_id,
            )
        self.backend_resolver.notify_change()

    def _maybe_recover_host_permissions(self, event: RequestEvent) -> None:
        """Recreate a missing per-host permissions file for the request's agent.

        The streamed request carries ``permissions_target_path`` -- the
        agent's opaque permissions handle (what its gateway JWT resolves
        to). :func:`maybe_recover_host_permissions_for_agent` swings that
        handle into the canonical host path when the latter is missing (so grants
        written by the approval flow are visible to the agent) and
        idempotently re-registers the agent in the host's allowlist. No-op
        when the target is absent (non-latchkey request) or the host is
        not yet known to discovery.
        """
        if not isinstance(
            event,
            (LatchkeyPredefinedPermissionRequestEvent, LatchkeyFileSharingPermissionRequestEvent),
        ):
            return
        target = event.permissions_target_path
        if target is None:
            return
        agent_id = AgentId(event.agent_id)
        host_id = self._resolve_host_id(agent_id)
        if host_id is None:
            return
        try:
            did_recover = maybe_recover_host_permissions_for_agent(
                latchkey=self.latchkey,
                host_id=host_id,
                agent_id=agent_id,
                opaque_permissions_path=Path(target),
            )
        except LatchkeyStoreError as e:
            logger.opt(exception=e).error(
                "Could not recover missing latchkey permissions file for host {} (agent {}): {}",
                host_id,
                event.agent_id,
                e,
            )
            return
        if did_recover:
            logger.info(
                "Recovered missing latchkey permissions file for host {} (agent {}) from opaque handle {}",
                host_id,
                event.agent_id,
                target,
            )

    def _resolve_host_id(self, agent_id: AgentId) -> HostId | None:
        """Resolve the host an agent runs on, or ``None`` when discovery hasn't caught up.

        Mirrors the resolution the permission-grant handler does: the
        backend resolver maps the agent id to its host id, and the
        placeholder ``"localhost"`` string (used by static / in-memory
        resolvers) is treated as "unknown host".
        """
        info = self.backend_resolver.get_agent_display_info(agent_id)
        if info is None:
            return None
        try:
            return HostId(info.host_id)
        except ValueError:
            return None


def _build_latchkey(data_directory: Path) -> Latchkey:
    # The latchkey-binary path is supplied by the Electron shell (which
    # bundles its own copy of latchkey under the app resources) via
    # ``MINDS_LATCHKEY_BINARY``. We fall back to ``"latchkey"`` on PATH
    # when the env var is not set, e.g. when minds is invoked outside
    # the Electron shell.
    binary_override = os.environ.get("MINDS_LATCHKEY_BINARY")
    latchkey_binary = binary_override if binary_override else LATCHKEY_BINARY
    # Single rooted directory for both upstream latchkey's credential
    # store (passed as ``LATCHKEY_DIRECTORY``) and the plugin's own
    # ``mngr_latchkey/`` metadata subdir. ``MINDS_LATCHKEY_DIRECTORY``
    # is honored as an override for users who want to share credentials
    # across multiple ``MINDS_ROOT_NAME``s.
    directory_override = os.environ.get("MINDS_LATCHKEY_DIRECTORY")
    latchkey_directory: Path
    if directory_override:
        latchkey_directory = Path(directory_override).expanduser()
    else:
        latchkey_directory = data_directory / "latchkey"
    # The per-env encryption key is loaded lazily on every subprocess
    # spawn inside ``Latchkey`` itself (via ``_load_encryption_key``)
    # so the secret only lives in parent-process memory for the
    # duration of a single env-builder + process-spawn call, never
    # cached as a long-lived attribute on this object.
    return Latchkey(
        latchkey_binary=latchkey_binary,
        latchkey_directory=latchkey_directory,
    )


def _sleep_then_open(url: str, delay: float = 1.0) -> None:
    """Wait ``delay`` seconds before opening ``url`` in the system browser.

    Uses ``threading.Event().wait`` instead of ``time.sleep`` so we honor
    the project ratchet against ``time.sleep``.
    """
    threading.Event().wait(timeout=delay)
    webbrowser.open(url)


def _restart_supervisor_then_prewarm_gateway_client(
    supervisor: LatchkeyForwardSupervisor,
    gateway_client: LatchkeyGatewayClient,
) -> None:
    """Restart the latchkey supervisor, then pre-warm the gateway client.

    Order matters: the gateway client's ``ensure_initialized`` reads
    the bound port from the supervisor's on-disk record, so it must
    run after the supervisor restart has stamped the fresh port.

    The supervisor was constructed in ``run`` with the bare-origin port
    baked into its ``extra_env``; it's threaded through to the
    supervisor as ``LATCHKEY_EXTENSION_MINDS_API_URL`` so the gateway's
    bundled ``minds-api-proxy`` extension knows where to forward agent
    traffic. Restarting the supervisor on every minds start is what
    makes this work across port changes: the env var is re-read at
    spawn time, not cached anywhere. ``minds_api_key`` is published
    alongside as ``LATCHKEY_EXTENSION_MINDS_API_KEY`` so the proxy
    can inject ``Authorization: Bearer <key>`` on every forwarded
    request.
    """
    _restart_mngr_latchkey_forward_supervisor(supervisor)
    try:
        gateway_client.ensure_initialized()
    except LatchkeyGatewayClientError as e:
        logger.warning(
            "Could not pre-warm the latchkey gateway client; first request will retry: {}",
            e,
        )


def _restart_mngr_latchkey_forward_supervisor(supervisor: LatchkeyForwardSupervisor) -> None:
    """Restart the detached ``mngr latchkey forward`` supervisor on minds startup.

    Uses :meth:`LatchkeyForwardSupervisor.restart` rather than
    ``ensure_running`` so that minds upgrades always run with a
    freshly-spawned supervisor: an older supervisor running stale
    code from a previous minds version is terminated and replaced
    on every minds start. A running supervisor that minds is happy
    to adopt does not exist in practice -- the supervisor's lifetime
    is tied to the gateway it owns, and the gateway is a minds-only
    consumer today. Restarting on every minds start is also what
    keeps ``LATCHKEY_EXTENSION_MINDS_API_URL`` in sync with the
    current bare-origin port -- minds re-binds its server on every
    start, and the supervisor restart re-publishes the env var (baked
    into the supervisor's ``extra_env`` at construction time in ``run``).

    Failures are logged as warnings rather than raised: a broken
    supervisor degrades latchkey to "unreachable from inside agents"
    but should not prevent minds itself from starting.
    """
    try:
        info = supervisor.restart()
    except LatchkeyError as e:
        logger.warning("Could not start detached mngr latchkey forward supervisor: {}", e)
        return
    logger.info("mngr latchkey forward supervisor running (pid={})", info.pid)
