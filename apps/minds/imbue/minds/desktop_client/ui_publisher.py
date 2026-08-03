"""Edge-driven publisher of chrome state onto the `/ui/ws` channel.

Replaces the legacy per-connection SSE derive loops: instead of every
connected window re-deriving the workspace list on its own thread each tick,
ONE background strand re-derives when a producer signals a change, diffs each
message against the last frame it broadcast, and fans only changed frames out
through the :class:`UiChannelBroadcaster`. New connections get the exact same
state via :meth:`build_snapshot`, which also feeds the bootstrap JSON inlined
into the SPA index page -- snapshot-on-connect is what lets the SPA drop the
legacy re-assert timers and redirect latches entirely.

Purely edge-driven by design: there is no periodic re-derive. A producer that
mutates observable state without firing its change callback is a bug to fix at
the producer, not to paper over with polling.

Derivation logic stays where it lives today (``app.py``'s ``_build_*``
helpers); the publisher receives them as injected callables that must be
callable from a bare background thread (``create_desktop_client`` wraps them
in an app context).
"""

import queue
import threading
from collections.abc import Callable

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.chrome_event_broadcast import ChromeEventBroadcaster
from imbue.minds.desktop_client.ui_channel import UiChannelBroadcaster
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiHelloMessage
from imbue.minds.desktop_client.ui_models import UiOpenHelpMessage
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiReloadMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiSnapshot
from imbue.minds.desktop_client.ui_models import UiWorkspaceStoppedMessage
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.errors import MindError
from imbue.mngr.errors import MngrError

# The one-shot message types a producer pushes directly (no diffing): health
# edges ride through publish_health so connect-time snapshots stay coherent.
UiOneShotMessage = UiWorkspaceStoppedMessage | UiOpenHelpMessage | UiReloadMessage


class UiStatePublisher(MutableModel):
    """One background strand deriving + diffing + broadcasting chrome state."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    broadcaster: UiChannelBroadcaster = Field(frozen=True, description="Fan-out target for every frame")
    derive_workspaces: Callable[[], UiWorkspacesMessage] = Field(frozen=True, description="Current workspace list")
    derive_accounts: Callable[[], UiAccountsMessage] = Field(frozen=True, description="Current account launcher state")
    derive_providers: Callable[[], UiProvidersMessage] = Field(
        frozen=True, description="Current providers panel state"
    )
    derive_requests: Callable[[], UiRequestsMessage] = Field(frozen=True, description="Current inbox summary")
    derive_discovery_health: Callable[[], UiDiscoveryHealthMessage] = Field(
        frozen=True, description="Current discovery pipeline health"
    )
    derive_health_states: Callable[[], tuple[UiHealthMessage, ...]] = Field(
        frozen=True, description="Per-workspace health snapshot for connect-time state"
    )

    _wake_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _last_frame_by_type: dict[str, str] = PrivateAttr(default_factory=dict)
    _is_started: bool = PrivateAttr(default=False)
    # One-shot payload dicts bridged from the legacy ChromeEventBroadcaster
    # (workspace_stopped / open_help producers keep their existing call sites).
    _legacy_event_queue: "queue.Queue[dict[str, str]]" = PrivateAttr(default_factory=queue.Queue)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the publish strand. Idempotent."""
        if self._is_started:
            return
        self._is_started = True
        concurrency_group.start_new_thread(
            target=self._run_publish_loop,
            name="ui-state-publisher",
            # The loop logs its own failures; a crash must not poison the root group.
            is_checked=False,
        )

    def stop(self) -> None:
        """Stop the publish strand (it exits at its next wake)."""
        self._stop_event.set()
        self._wake_event.set()

    def notify_change(self) -> None:
        """Producer signal: some derived state may have changed; re-derive and diff soon.

        Thread-safe and cheap; every producer change callback funnels here.
        """
        self._wake_event.set()

    def bridge_legacy_broker(self, broker: ChromeEventBroadcaster) -> None:
        """Receive the legacy chrome broker's one-shot events and wakes on this publisher.

        Subscribing with the publisher's own wake event means every existing
        ``broadcast``/``wake_subscribers`` call site in the legacy handlers
        reaches the channel without those handlers changing; the bridge is
        deleted with the broker in the final cleanup phase.
        """
        broker.subscribe(self._legacy_event_queue, self._wake_event)

    def _drain_bridged_legacy_events(self) -> None:
        is_drained = False
        while not is_drained:
            try:
                payload = self._legacy_event_queue.get_nowait()
            except queue.Empty:
                is_drained = True
                continue
            payload_type = payload.get("type", "")
            if payload_type == "workspace_stopped":
                self.publish_one_shot(UiWorkspaceStoppedMessage(agent_id=payload.get("agent_id", "")))
            elif payload_type == "open_help":
                self.publish_one_shot(
                    UiOpenHelpMessage(
                        description=payload.get("description", ""),
                        workspace_agent_id=payload.get("workspace_agent_id", ""),
                    )
                )
            else:
                logger.warning("Dropped an unrecognized bridged chrome event of type {}", payload_type)

    def publish_health(self, message: UiHealthMessage) -> None:
        """Broadcast one workspace's health edge immediately (no diffing)."""
        self.broadcaster.broadcast(message.model_dump_json())

    def publish_one_shot(self, message: UiOneShotMessage) -> None:
        """Broadcast a fire-and-forget event frame (workspace_stopped / open_help / reload_ui)."""
        self.broadcaster.broadcast(message.model_dump_json())

    def build_snapshot(self) -> UiSnapshot:
        """Derive the complete current state (shared by WS connect and bootstrap JSON)."""
        return UiSnapshot(
            workspaces=self.derive_workspaces(),
            accounts=self.derive_accounts(),
            providers=self.derive_providers(),
            requests=self.derive_requests(),
            health=self.derive_health_states(),
            discovery_health=self.derive_discovery_health(),
        )

    def build_snapshot_frames(self) -> list[str]:
        """Serialize the connect sequence: hello, then one frame per snapshot type."""
        snapshot = self.build_snapshot()
        frames = [UiHelloMessage(schema_version=UI_SCHEMA_VERSION).model_dump_json()]
        frames.append(snapshot.workspaces.model_dump_json())
        frames.append(snapshot.accounts.model_dump_json())
        frames.append(snapshot.providers.model_dump_json())
        frames.append(snapshot.requests.model_dump_json())
        for health_message in snapshot.health:
            frames.append(health_message.model_dump_json())
        frames.append(snapshot.discovery_health.model_dump_json())
        return frames

    def publish_now(self) -> None:
        """One synchronous derive+diff+broadcast pass (also the loop body; exposed for tests)."""
        self._drain_bridged_legacy_events()
        derives: tuple[
            Callable[
                [],
                UiWorkspacesMessage
                | UiAccountsMessage
                | UiProvidersMessage
                | UiRequestsMessage
                | UiDiscoveryHealthMessage,
            ],
            ...,
        ] = (
            self.derive_workspaces,
            self.derive_accounts,
            self.derive_providers,
            self.derive_requests,
            self.derive_discovery_health,
        )
        for derive in derives:
            try:
                message = derive()
            except (MindError, MngrError, OSError, RuntimeError) as e:
                # A single failing derive (e.g. a settings file mid-write) must
                # not stop the other message types from publishing this tick.
                logger.warning("Skipped one /ui/ws publish tick derive: {}", e)
                continue
            frame = message.model_dump_json()
            if self._last_frame_by_type.get(message.type) != frame:
                self._last_frame_by_type[message.type] = frame
                self.broadcaster.broadcast(frame)

    def _run_publish_loop(self) -> None:
        is_running = True
        while is_running:
            self._wake_event.wait()
            # Clear BEFORE deriving so a producer firing mid-derive re-sets the
            # event and the loop runs another pass rather than losing the wake.
            self._wake_event.clear()
            if self._stop_event.is_set():
                is_running = False
                continue
            try:
                self.publish_now()
            except Exception as e:
                # Main-loop guard: this strand is the ONLY publisher of chrome
                # state, so an unexpected exception (publish_now already
                # narrows the expected derive failures) must not kill it --
                # every window would silently stop updating for the process
                # lifetime. Log loudly and keep serving the next wake.
                logger.opt(exception=e).error("The ui-state-publisher pass failed; continuing with the next wake")
        logger.debug("Exited the ui-state-publisher loop")
