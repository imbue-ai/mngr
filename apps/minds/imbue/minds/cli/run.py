"""``minds run``: spawn ``mngr forward`` and serve the bare-origin minds UI.

Replaces the deleted ``desktop_client/runner.py``. The auth + subdomain-
forwarding logic lives in the ``mngr_forward`` plugin now; this command:

1. Spawns ``mngr forward --service system_interface --preauth-cookie ...`` as
   a subprocess via ``EnvelopeStreamConsumer`` (which feeds the surviving
   ``MngrCliBackendResolver`` from the plugin's envelope stream).
2. Registers minds' ``LocalAgentDiscoveryHandler`` on the consumer so local
   agents still get their ``minds_api_url`` files written and stored
   Cloudflare tunnel tokens get re-injected on (re-)discovery.
3. Registers ``MindsApiUrlWriter`` on the consumer's
   ``reverse_tunnel_established`` callback so remote agents get their
   ``minds_api_url`` written after each tunnel (re-)establishment.
4. Builds the slimmed minds-side bare-origin FastAPI app and runs it on
   ``--port`` (default 8420).
5. Emits a ``mngr_forward_started`` JSONL event on stdout carrying the
   preauth cookie value, so the Electron shell can pre-set
   ``mngr_forward_session=<value>`` on ``localhost:<mngr-forward-port>``
   before the first agent-subdomain navigation.
"""

import os
import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Final

import click
import uvicorn
from fastapi import FastAPI
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import disable_imbue_cloud_provider_for_account
from imbue.minds.bootstrap import imbue_cloud_provider_name_for_account
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_HOST
from imbue.minds.config.data_types import DEFAULT_DESKTOP_CLIENT_PORT
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.app import start_workspace_health_probe_loop
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.forward_cli import ForwardSubprocessConfig
from imbue.minds.desktop_client.forward_cli import LocalAgentDiscoveryHandler
from imbue.minds.desktop_client.forward_cli import MindsApiUrlWriter
from imbue.minds.desktop_client.forward_cli import start_mngr_forward
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import PermissionRequestsConsumer
from imbue.minds.desktop_client.latchkey.permissions import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.latchkey.services_catalog import LatchkeyServicesCatalogError
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.latchkey.services_catalog import load_services_catalog
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.request_events import LatchkeyPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_server_health import WorkspaceServerHealthTracker
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.utils.output import emit_event
from imbue.mngr.utils.parent_process import start_grandparent_death_watcher
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

_DEFAULT_MNGR_FORWARD_PORT: Final[int] = 8421
_AUTH_ERROR_TYPE: Final[str] = "ImbueCloudAuthError"


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
    "--mngr-forward-port",
    default=_DEFAULT_MNGR_FORWARD_PORT,
    show_default=True,
    help="Port to bind the spawned `mngr forward` subprocess to",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Do not open the minds UI in the system browser",
)
@click.pass_context
def run(
    ctx: click.Context,
    host: str,
    port: int,
    mngr_forward_port: int,
    no_browser: bool,
) -> None:
    # noqa: PLR0913 — flag count matches the legacy `minds forward` interface
    """Run the minds bare-origin server with `mngr forward` as a subprocess."""
    root_name = resolve_minds_root_name()
    data_directory = minds_data_dir_for(root_name)
    minds_config = MindsConfig(data_dir=data_directory)
    output_format: OutputFormat = ctx.obj.get("output_format", OutputFormat.HUMAN)

    logger.info("Starting `minds run`...")
    logger.info("  Bare-origin: http://{}:{}", host, port)
    logger.info("  mngr forward: http://127.0.0.1:{}", mngr_forward_port)
    logger.info("  MINDS_ROOT_NAME: {}", root_name)
    logger.info("  Data directory: {}", data_directory)
    logger.info("  remote_service_connector_url: {}", minds_config.remote_service_connector_url)

    paths = WorkspacePaths(data_dir=data_directory)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    is_electron = os.getenv("MINDS_ELECTRON") == "1"
    notification_dispatcher = NotificationDispatcher(is_electron=is_electron)
    backend_resolver = MngrCliBackendResolver()
    latchkey = _build_latchkey(data_directory=data_directory)
    latchkey.initialize()

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
    root_concurrency_group.start_new_thread(
        _ensure_mngr_latchkey_forward_supervisor, args=(latchkey,), name="mngr-latchkey-forward-supervisor-setup"
    )

    # Watch our *grandparent* (typically Electron) rather than our immediate
    # parent (the ``uv run`` wrapper, which doesn't propagate Electron's
    # death). When Electron crashes or is killed without running its
    # ``child.on('exit')`` cleanup, this watcher SIGTERMs us so the
    # ``mngr forward`` plugin and its observe / event grandchildren can in
    # turn exit cleanly. Without it, a crashed Electron leaves the entire
    # orphan tree running across restarts.
    start_grandparent_death_watcher(root_concurrency_group)

    gateway_client = LatchkeyGatewayClient.from_latchkey(latchkey)
    # Pre-warm the gateway client in a background thread.
    root_concurrency_group.start_new_thread(gateway_client.ensure_initialized, name="latchkey-gateway-init")

    latchkey_permission_handler = LatchkeyPermissionGrantHandler(
        data_dir=data_directory,
        latchkey=latchkey,
        services_catalog=_try_load_latchkey_services_catalog(),
        mngr_message_sender=MngrMessageSender(),
        gateway_client=gateway_client,
    )
    imbue_cloud_cli = ImbueCloudCli(parent_concurrency_group=root_concurrency_group)
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)
    session_store = MultiAccountSessionStore(data_dir=data_directory, cli=imbue_cloud_cli)
    response_events = load_response_events(data_directory)
    request_inbox = RequestInbox()
    for resp in response_events:
        request_inbox = request_inbox.add_response(resp)

    # Spawn the plugin and attach the envelope consumer that feeds the
    # surviving resolver from the plugin's stdout stream.
    mngr_host_dir_str = os.environ.get("MNGR_HOST_DIR")
    mngr_host_dir = Path(mngr_host_dir_str).expanduser() if mngr_host_dir_str else (Path.home() / ".mngr")
    forward_config = ForwardSubprocessConfig(
        port=mngr_forward_port,
        reverse_specs=(f"0:{port}",),
        mngr_host_dir=mngr_host_dir,
    )
    consumer, preauth_cookie = start_mngr_forward(
        config=forward_config,
        resolver=backend_resolver,
        notification_dispatcher=notification_dispatcher,
    )

    # AgentCreator is constructed *after* ``start_mngr_forward`` so the
    # readiness probe can use the same preauth cookie the plugin accepts and
    # Electron pre-sets. Building it earlier would force us to either pre-mint
    # the cookie out of band or expose a setter on AgentCreator, both of which
    # are worse than just keeping the construction order linear.
    agent_creator = AgentCreator(
        paths=paths,
        server_port=port,
        imbue_cloud_cli=imbue_cloud_cli,
        latchkey=latchkey,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
    )

    # Local-agent ``minds_api_url`` writes (Cloudflare-token re-injection
    # has moved into the agent's own container; see commit 97f40d02d).
    consumer.add_on_agent_discovered_callback(
        LocalAgentDiscoveryHandler(
            minds_api_port=port,
            mngr_host_dir=mngr_host_dir,
        )
    )
    # Remote-agent ``minds_api_url`` writes happen via the plugin's
    # reverse_tunnel_established envelope.
    consumer.add_on_reverse_tunnel_established_callback(MindsApiUrlWriter(resolver=backend_resolver))
    # Latchkey discovery / destruction wiring now lives in the detached
    # ``mngr latchkey forward`` subprocess started above; no callbacks to
    # register here.

    # Auto-disable an ``imbue_cloud_<slug>`` provider if its session is
    # revoked server-side, so the rest of the user's accounts keep working
    # instead of every observe poll re-trying the dead session.
    consumer.add_on_provider_error_callback(
        _ImbueCloudAuthErrorDisabler(consumer=consumer, session_store=session_store)
    )

    # Workspace-server health tracker: feeds on backend failures observed by
    # the plugin. Constructed here (instead of inside create_desktop_client)
    # so the envelope-failure callback is registered before consumer.start()
    # below; otherwise early failures would dispatch against an empty list.
    workspace_health_tracker = WorkspaceServerHealthTracker()
    consumer.add_on_workspace_backend_failure_callback(
        lambda agent_id, _reason, _status: workspace_health_tracker.record_failure(agent_id)
    )

    # All callbacks registered -- now safe to start the envelope reader
    # threads. Doing this earlier (e.g. inside ``start_mngr_forward``)
    # would open a race window where envelopes arriving before the
    # callbacks were registered would be dispatched against an empty
    # callback list and silently dropped.
    consumer.start(root_concurrency_group)

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
        request_inbox=request_inbox,
        request_event_handlers=(latchkey_permission_handler,),
        server_port=port,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        output_format=output_format,
        root_concurrency_group=root_concurrency_group,
        workspace_health_tracker=workspace_health_tracker,
        mngr_binary=MNGR_BINARY,
        mngr_host_dir=mngr_host_dir,
    )

    # Background probe loop: flips STUCK/RESTARTING agents back to HEALTHY
    # once the plugin probe sees a 200. Started here (not inside
    # ``create_desktop_client``) so test factories that build the app can
    # skip the probe thread by simply not calling this function.
    start_workspace_health_probe_loop(
        tracker=workspace_health_tracker,
        backend_resolver=backend_resolver,
        mngr_forward_port=mngr_forward_port,
        mngr_forward_preauth_cookie=preauth_cookie,
        root_concurrency_group=root_concurrency_group,
    )

    # Wire the permission-requests streaming consumer once the FastAPI
    # app is built so the on_request callback can mutate ``app.state``
    # directly. The consumer thread runs for the lifetime of
    # ``root_concurrency_group``.
    permission_requests_consumer = PermissionRequestsConsumer(
        gateway_client=gateway_client,
        on_request=_StreamedPermissionRequestHandler(app=app, backend_resolver=backend_resolver),
    )
    permission_requests_consumer.start(root_concurrency_group)

    if not no_browser:
        thread = threading.Thread(target=_sleep_then_open, args=(f"http://{host}:{port}/",), daemon=True)
        thread.start()

    try:
        uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=1)
    finally:
        consumer.terminate()


class _StreamedPermissionRequestHandler(FrozenModel):
    """Callable that appends a streamed permission request to the app inbox.

    The handler runs on the permission-requests consumer thread (not
    the FastAPI event loop), so it only does thread-safe work:
    appending to the immutable :class:`RequestInbox` produces a new
    instance per mutation and Python attribute assignment is atomic for
    our purposes here. Same trick the legacy JSONL
    ``_handle_request_event_callback`` already uses.
    """

    app: FastAPI = Field(
        frozen=True,
        description="Desktop-client FastAPI instance whose ``state.request_inbox`` is mutated on receipt.",
    )
    backend_resolver: MngrCliBackendResolver = Field(
        frozen=True,
        description="Resolver whose ``notify_change()`` wakes the chrome SSE so the panel updates promptly.",
    )

    # ``FastAPI`` and ``MngrCliBackendResolver`` are not pydantic
    # natives; tolerate them with ``arbitrary_types_allowed``.
    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}

    def __call__(self, event: LatchkeyPermissionRequestEvent) -> None:
        current: RequestInbox | None = self.app.state.request_inbox
        if current is None:
            return
        self.app.state.request_inbox = current.add_request(event)
        logger.info(
            "Streamed latchkey permission request for agent {} (service={}, request_id={})",
            event.agent_id,
            event.service_name,
            event.event_id,
        )
        self.backend_resolver.notify_change()


def _try_load_latchkey_services_catalog() -> dict[str, ServicePermissionInfo]:
    try:
        return load_services_catalog()
    except LatchkeyServicesCatalogError as e:
        logger.warning("Could not load latchkey services catalog; permission dialogs disabled: {}", e)
        return {}


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
    return Latchkey(latchkey_binary=latchkey_binary, latchkey_directory=latchkey_directory)


def _sleep_then_open(url: str, delay: float = 1.0) -> None:
    """Wait ``delay`` seconds before opening ``url`` in the system browser.

    Uses ``threading.Event().wait`` instead of ``time.sleep`` so we honor
    the project ratchet against ``time.sleep``.
    """
    threading.Event().wait(timeout=delay)
    webbrowser.open(url)


def _ensure_mngr_latchkey_forward_supervisor(latchkey: Latchkey) -> None:
    """Restart the detached ``mngr latchkey forward`` supervisor on minds startup.

    Reuses ``latchkey``'s already-resolved binary + directory paths so
    the supervisor sees exactly the same latchkey state minds itself
    works against. Bare-name ``mngr`` is used; bundled minds builds
    rely on the Electron shell having put ``mngr`` on the child's
    PATH alongside the bundled ``latchkey`` (the
    :class:`MngrMessageSender` and ``mngr forward`` subprocess paths
    already make the same assumption).

    Uses :meth:`LatchkeyForwardSupervisor.restart` rather than
    ``ensure_running`` so that minds upgrades always run with a
    freshly-spawned supervisor: an older supervisor running stale
    code from a previous minds version is terminated and replaced
    on every minds start. A running supervisor that minds is happy
    to adopt does not exist in practice -- the supervisor's lifetime
    is tied to the gateway it owns, and the gateway is a minds-only
    consumer today.

    Failures are logged as warnings rather than raised: a broken
    supervisor degrades latchkey to "unreachable from inside agents"
    but should not prevent minds itself from starting.
    """
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=MNGR_BINARY,
        latchkey_binary=latchkey.latchkey_binary,
        latchkey_directory=latchkey.latchkey_directory,
    )
    try:
        info = supervisor.restart()
    except LatchkeyError as e:
        logger.warning("Could not start detached mngr latchkey forward supervisor: {}", e)
        return
    logger.info("mngr latchkey forward supervisor running (pid={})", info.pid)


class _ImbueCloudAuthErrorDisabler(FrozenModel):
    """Auto-disables an ``imbue_cloud_<slug>`` provider on session-revoke errors.

    Discovery surfaces ``ImbueCloudAuthError`` whenever the connector
    rejects a refresh (token theft detected, refresh token expired past
    the family lifetime, etc.). Without intervention every subsequent
    ``mngr observe`` poll re-tries the same dead session and the whole
    discovery stream errors out, blocking the rest of the user's
    accounts. ``__call__`` walks ``session_store`` to map the offending
    provider name back to an email, flips ``is_enabled = false`` on the
    block in settings.toml, and bounces the plugin's observe child so the
    change takes effect within the same minds session. Re-enabling
    happens only on an explicit signin
    (``set_imbue_cloud_provider_for_account(..., force_enable=True)``).

    The bounce is dispatched onto a daemon thread because ``__call__``
    runs on ``EnvelopeStreamConsumer``'s envelope-stream reader thread --
    sending ``SIGHUP`` is itself non-blocking, but keeping the off-thread
    dispatch matches the prior ``MngrStreamManager.restart_observe()``
    behaviour and isolates any future expansion of the bounce path from
    the reader thread.
    """

    consumer: EnvelopeStreamConsumer = Field(
        frozen=True, description="Envelope consumer to bounce observe on after disable"
    )
    session_store: MultiAccountSessionStore = Field(
        frozen=True, description="Session mirror used to map provider name to account email"
    )

    def __call__(self, provider_name: str, error_type: str, error_message: str) -> None:
        if error_type != _AUTH_ERROR_TYPE:
            return
        offending_email: str | None = None
        for account in self.session_store.list_accounts():
            try:
                if imbue_cloud_provider_name_for_account(str(account.email)) == provider_name:
                    offending_email = str(account.email)
                    break
            except ValueError:
                continue
        if offending_email is None:
            logger.warning(
                "Auth error from provider {} but no matching minds session found; skipping auto-disable",
                provider_name,
            )
            return
        if disable_imbue_cloud_provider_for_account(offending_email):
            logger.warning(
                "Auto-disabled imbue_cloud provider for {} after auth error: {}",
                offending_email,
                error_message,
            )
            threading.Thread(
                target=self.consumer.bounce_observe,
                name=f"bounce-observe-after-disable-{provider_name}",
                daemon=True,
            ).start()
