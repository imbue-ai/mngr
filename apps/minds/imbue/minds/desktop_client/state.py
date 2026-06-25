"""Typed dependency holder for the Flask desktop-client app.

Replaces the FastAPI ``app.state`` namespace. A single
:class:`DesktopClientState` is stashed on the Flask app's
``extensions`` mapping at construction time and read back via
:func:`get_state` -- from request handlers (which default to
``current_app``) and from background threads (which pass the app
explicitly, since there is no app context off the request path).

Field names intentionally mirror the old ``app.state.<name>`` attribute
names so handler bodies read ``get_state().<name>`` unchanged.
"""

import threading
from pathlib import Path
from typing import Final

import httpx
from flask import Flask
from flask import current_app
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.auth import AuthStoreInterface
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.discovery_health import DiscoveryHealthWatchdog
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import PermissionRequestsConsumer
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import OutputFormat
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

_STATE_KEY: Final[str] = "minds_desktop_client_state"


class DesktopClientState(MutableModel):
    """All runtime dependencies the desktop-client request handlers read.

    Most fields are configuration set once at construction (``frozen=True``).
    ``http_client``, ``request_inbox``, and ``permission_requests_consumer``
    are mutated during the app's lifetime and are intentionally not frozen.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    auth_store: AuthStoreInterface = Field(frozen=True, description="Cookie/session auth store")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Agent/host discovery resolver")
    http_client: httpx.Client | None = Field(
        default=None, description="Shared sync HTTP client (created by the runtime; injected in tests)"
    )
    agent_creator: AgentCreator | None = Field(
        default=None, frozen=True, description="In-flight agent creation manager"
    )
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        default=None, frozen=True, description="imbue_cloud plugin CLI wrapper"
    )
    telegram_orchestrator: TelegramSetupOrchestrator | None = Field(
        default=None, frozen=True, description="Telegram bot setup orchestrator"
    )
    notification_dispatcher: NotificationDispatcher | None = Field(
        default=None, frozen=True, description="OS notification dispatcher"
    )
    api_v1_paths: WorkspacePaths | None = Field(
        default=None, frozen=True, description="Workspace data paths; gates the /api/v1 mount"
    )
    minds_config: MindsConfig | None = Field(default=None, frozen=True, description="Per-user minds config store")
    geo_location_cache: GeoLocationCache = Field(
        default_factory=GeoLocationCache, description="One-shot IP-geolocation cache for region defaults"
    )
    client_env_config: ClientEnvConfig | None = Field(
        default=None, frozen=True, description="Loaded per-env client config (connector URL, etc.)"
    )
    envelope_stream_consumer: EnvelopeStreamConsumer | None = Field(
        default=None, frozen=True, description="mngr forward envelope stream consumer"
    )
    session_store: MultiAccountSessionStore | None = Field(
        default=None, frozen=True, description="Multi-account session store"
    )
    request_inbox: RequestInbox | None = Field(
        default=None, description="Immutable pending-request inbox (reassigned)"
    )
    request_event_handlers: tuple[RequestEventHandler, ...] = Field(
        default=(), frozen=True, description="Registered request-event grant/deny handlers"
    )
    auth_server_port: int = Field(default=0, frozen=True, description="Bare-origin server port")
    mngr_forward_port: int = Field(default=0, frozen=True, description="mngr forward plugin port")
    mngr_forward_preauth_cookie: str | None = Field(
        default=None, frozen=True, description="Preauth cookie accepted by the mngr forward plugin"
    )
    auth_output_format: OutputFormat = Field(
        default=OutputFormat.JSONL, frozen=True, description="Output format for emitted JSONL events"
    )
    root_concurrency_group: ConcurrencyGroup | None = Field(
        default=None, frozen=True, description="Root concurrency group owning background strands"
    )
    system_interface_health_tracker: SystemInterfaceHealthTracker | None = Field(
        default=None, frozen=True, description="System-interface health tracker"
    )
    discovery_health_watchdog: DiscoveryHealthWatchdog | None = Field(
        default=None, frozen=True, description="App-global discovery-pipeline health watchdog"
    )
    mngr_binary: str = Field(default="mngr", frozen=True, description="Path/name of the mngr binary to shell out to")
    mngr_host_dir: Path = Field(
        default_factory=lambda: Path.home() / ".mngr", frozen=True, description="MNGR_HOST_DIR"
    )
    minds_api_key: str | None = Field(
        default=None, frozen=True, description="Central minds API key for /api/v1 + WebDAV"
    )
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None = Field(
        default=None, frozen=True, description="Detached mngr latchkey forward supervisor handle"
    )
    permission_requests_consumer: PermissionRequestsConsumer | None = Field(
        default=None, description="Streaming permission-requests consumer (wired post-construction)"
    )
    shutdown_event: threading.Event = Field(
        default_factory=threading.Event, description="Cross-thread flag SSE handlers poll to exit on shutdown"
    )


def set_state(app: Flask, state: DesktopClientState) -> None:
    """Stash the desktop-client state on the Flask app's extensions mapping."""
    app.extensions[_STATE_KEY] = state


def get_state(app: Flask | None = None) -> DesktopClientState:
    """Return the desktop-client state for ``app`` (or the current app).

    Pass ``app`` explicitly from background threads, where there is no
    request/app context for ``current_app`` to resolve.
    """
    target = app if app is not None else current_app
    # ``extensions`` values are typed ``Any``; the key is only ever populated by
    # ``set_state`` with a DesktopClientState. A missing key raises KeyError,
    # which is the right signal for "create_desktop_client() never ran".
    return target.extensions[_STATE_KEY]
