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
the producer, not to paper over with polling. Passes are spaced at least
``min_seconds_between_passes`` apart, so a producer firing faster than that
costs one pass per gap rather than one per fire; a wake inside the gap is
folded into the next pass, never dropped.

Derivation logic stays where it lives today (``app.py``'s ``_build_*``
helpers); the publisher receives them as injected callables that must be
callable from a bare background thread (``create_desktop_client`` wraps them
in an app context).

Every window shows one account, so the install's state is derived once per
pass and projected for each account a connected window reports
(``ui_account_scope.py``); the diff is kept per account and frame type, and
each projection goes only to that account's connections. The frames that are
not about any account (discovery health, the device's condition, the one-shot
events) go to every connection as before.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from typing import Final
from typing import TypeVar

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.ui_account_scope import account_workspace_scope
from imbue.minds.desktop_client.ui_account_scope import project_accounts
from imbue.minds.desktop_client.ui_account_scope import project_notifications
from imbue.minds.desktop_client.ui_account_scope import project_providers
from imbue.minds.desktop_client.ui_account_scope import project_requests
from imbue.minds.desktop_client.ui_account_scope import project_snapshot
from imbue.minds.desktop_client.ui_account_scope import project_workspace_updates
from imbue.minds.desktop_client.ui_account_scope import project_workspaces
from imbue.minds.desktop_client.ui_channel import UiChannelBroadcaster
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiBringAppToFrontMessage
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiEnvironmentMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiHelloMessage
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.desktop_client.ui_models import UiOpenHelpMessage
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiReloadMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiSnapshot
from imbue.minds.desktop_client.ui_models import UiWorkspaceRefreshMessage
from imbue.minds.desktop_client.ui_models import UiWorkspaceStoppedMessage
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdatesMessage
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.errors import MindError
from imbue.mngr.errors import MngrError

# The one-shot message types a producer pushes directly (no diffing): health
# edges ride through publish_health so connect-time snapshots stay coherent.
UiOneShotMessage = (
    UiWorkspaceStoppedMessage
    | UiOpenHelpMessage
    | UiWorkspaceRefreshMessage
    | UiReloadMessage
    | UiBringAppToFrontMessage
)

# The message types a publish pass derives and diffs against the last frame it broadcast.
_UiDiffedMessage = (
    UiWorkspacesMessage
    | UiAccountsMessage
    | UiProvidersMessage
    | UiRequestsMessage
    | UiNotificationsMessage
    | UiDiscoveryHealthMessage
    | UiWorkspaceUpdatesMessage
    | UiEnvironmentMessage
)

DEFAULT_MIN_SECONDS_BETWEEN_UI_PASSES: Final[float] = 0.1

_DerivedT = TypeVar("_DerivedT")

# The diff key of the frames that belong to no account and go to every connection.
_INSTALL_WIDE_SCOPE: Final[str] = ""


class _DerivedInstallState(FrozenModel):
    """One pass's install-wide derivation; a frame whose derive failed this pass is None and is skipped."""

    workspaces: UiWorkspacesMessage | None = Field(description="The install's workspace list")
    accounts: UiAccountsMessage | None = Field(description="The switcher's account lists")
    providers: UiProvidersMessage | None = Field(description="Every account's providers section")
    requests: UiRequestsMessage | None = Field(description="Every pending request")
    notifications: UiNotificationsMessage | None = Field(description="The whole feed")
    discovery_health: UiDiscoveryHealthMessage | None = Field(description="Discovery pipeline health")
    workspace_updates: UiWorkspaceUpdatesMessage | None = Field(description="Every workspace's update state")
    environment: UiEnvironmentMessage | None = Field(description="This device's condition")


class UiStatePublisher(MutableModel):
    """One background strand deriving + diffing + broadcasting chrome state."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    broadcaster: UiChannelBroadcaster = Field(frozen=True, description="Fan-out target for every frame")
    derive_workspaces: Callable[[], UiWorkspacesMessage] = Field(frozen=True, description="Current workspace list")
    derive_accounts: Callable[[], UiAccountsMessage] = Field(
        frozen=True, description="Current signed-in accounts (the launcher label and Manage Accounts' list)"
    )
    derive_providers: Callable[[], UiProvidersMessage] = Field(
        frozen=True, description="Current providers panel state"
    )
    derive_requests_and_notifications: Callable[[], tuple[UiRequestsMessage, UiNotificationsMessage]] = Field(
        frozen=True,
        description="Current inbox summary and notification feed, derived together from one read of the pending set",
    )
    derive_discovery_health: Callable[[], UiDiscoveryHealthMessage] = Field(
        frozen=True, description="Current discovery pipeline health"
    )
    derive_environment: Callable[[], UiEnvironmentMessage] = Field(
        frozen=True, description="Current device-level connectivity condition"
    )
    derive_health_states: Callable[[], tuple[UiHealthMessage, ...]] = Field(
        frozen=True, description="Per-workspace health snapshot for connect-time state"
    )
    derive_workspace_updates: Callable[[], UiWorkspaceUpdatesMessage] = Field(
        frozen=True, description="Current per-workspace update state"
    )
    resolve_workspace_account_id: Callable[[str], str] = Field(
        frozen=True,
        description="The account id a workspace's health edge belongs to ('local' for one associated with none)",
    )
    min_seconds_between_passes: float = Field(
        default=DEFAULT_MIN_SECONDS_BETWEEN_UI_PASSES,
        frozen=True,
        description="Shortest gap between the starts of two background publish passes",
    )

    _wake_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    # (account id, frame type) -> the last frame broadcast to that account's connections.
    _last_frame_by_scope_and_type: dict[tuple[str, str], str] = PrivateAttr(default_factory=dict)
    _is_started: bool = PrivateAttr(default=False)
    _last_pass_started_at_monotonic: float | None = PrivateAttr(default=None)

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

    def publish_health(self, message: UiHealthMessage) -> None:
        """Send one workspace's health edge to its account's windows immediately (no diffing)."""
        self.broadcaster.broadcast_to_account(
            self.resolve_workspace_account_id(message.agent_id), message.model_dump_json()
        )

    def publish_one_shot(self, message: UiOneShotMessage) -> None:
        """Broadcast a fire-and-forget event frame."""
        self.broadcaster.broadcast(message.model_dump_json())

    def build_snapshot(self, account_id: str) -> UiSnapshot:
        """Derive the complete current state as ``account_id``'s windows see it (WS connect and bootstrap JSON)."""
        requests, notifications = self.derive_requests_and_notifications()
        install_snapshot = UiSnapshot(
            workspaces=self.derive_workspaces(),
            accounts=self.derive_accounts(),
            providers=self.derive_providers(),
            requests=requests,
            notifications=notifications,
            health=self.derive_health_states(),
            discovery_health=self.derive_discovery_health(),
            workspace_updates=self.derive_workspace_updates(),
            environment=self.derive_environment(),
        )
        return project_snapshot(install_snapshot, account_id)

    def build_snapshot_frames(self, account_id: str) -> list[str]:
        """Serialize one account's connect sequence: hello, then one frame per snapshot type."""
        snapshot = self.build_snapshot(account_id)
        frames = [UiHelloMessage(schema_version=UI_SCHEMA_VERSION).model_dump_json()]
        frames.append(snapshot.workspaces.model_dump_json())
        frames.append(snapshot.accounts.model_dump_json())
        frames.append(snapshot.providers.model_dump_json())
        frames.append(snapshot.requests.model_dump_json())
        for health_message in snapshot.health:
            # Marked here rather than at the derive: the same derive backs the
            # inlined bootstrap JSON, and these frames are the only ones a
            # client sees as a replay.
            marked = health_message.model_copy_update(to_update(health_message.field_ref().is_snapshot, True))
            frames.append(marked.model_dump_json())
        frames.append(snapshot.discovery_health.model_dump_json())
        marked_notifications = snapshot.notifications.model_copy_update(
            to_update(snapshot.notifications.field_ref().is_snapshot, True)
        )
        frames.append(marked_notifications.model_dump_json())
        frames.append(snapshot.workspace_updates.model_dump_json())
        frames.append(snapshot.environment.model_dump_json())
        return frames

    def publish_now(self) -> None:
        """One synchronous derive+project+diff+broadcast pass (also the loop body; exposed for tests)."""
        derived = self._derive_install_state()
        account_ids = self.broadcaster.connected_account_ids()
        self._forget_departed_scopes(account_ids)
        for install_wide in (derived.discovery_health, derived.environment):
            if install_wide is not None:
                self._broadcast_if_changed(_INSTALL_WIDE_SCOPE, install_wide)
        for account_id in sorted(account_ids):
            self._publish_account_projection(account_id, derived)

    def _publish_account_projection(self, account_id: str, derived: _DerivedInstallState) -> None:
        if derived.accounts is not None:
            self._broadcast_if_changed(account_id, project_accounts(derived.accounts, account_id))
        if derived.providers is not None:
            self._broadcast_if_changed(account_id, project_providers(derived.providers, account_id))
        # The workspace-keyed frames are projected through the account's rows, so a
        # pass whose workspace derive failed says nothing about them this tick.
        if derived.workspaces is None:
            return
        scope = account_workspace_scope(derived.workspaces, account_id)
        self._broadcast_if_changed(account_id, project_workspaces(derived.workspaces, account_id))
        if derived.requests is not None:
            self._broadcast_if_changed(account_id, project_requests(derived.requests, scope))
        if derived.notifications is not None:
            self._broadcast_if_changed(account_id, project_notifications(derived.notifications, scope))
        if derived.workspace_updates is not None:
            self._broadcast_if_changed(account_id, project_workspace_updates(derived.workspace_updates, scope))

    def _broadcast_if_changed(self, scope: str, message: _UiDiffedMessage) -> None:
        """Send the frame to the scope's windows (every window for the install-wide scope) unless it is unchanged."""
        frame = message.model_dump_json()
        key = (scope, message.type)
        if self._last_frame_by_scope_and_type.get(key) == frame:
            return
        self._last_frame_by_scope_and_type[key] = frame
        if scope == _INSTALL_WIDE_SCOPE:
            self.broadcaster.broadcast(frame)
        else:
            self.broadcaster.broadcast_to_account(scope, frame)

    def _forget_departed_scopes(self, account_ids: AbstractSet[str]) -> None:
        """Drop the diff state of accounts no window shows: their next window gets a snapshot anyway."""
        for key in list(self._last_frame_by_scope_and_type):
            if key[0] != _INSTALL_WIDE_SCOPE and key[0] not in account_ids:
                del self._last_frame_by_scope_and_type[key]

    def _derive_install_state(self) -> _DerivedInstallState:
        """Run every derive once, each on its own so one failing (a settings file mid-write) skips only its frames."""
        workspaces = self._derive_or_none("workspaces", self.derive_workspaces)
        accounts = self._derive_or_none("accounts", self.derive_accounts)
        providers = self._derive_or_none("providers", self.derive_providers)
        requests_and_notifications = self._derive_or_none(
            "requests and notifications", self.derive_requests_and_notifications
        )
        discovery_health = self._derive_or_none("discovery_health", self.derive_discovery_health)
        workspace_updates = self._derive_or_none("workspace_updates", self.derive_workspace_updates)
        environment = self._derive_or_none("environment", self.derive_environment)
        return _DerivedInstallState(
            workspaces=workspaces,
            accounts=accounts,
            providers=providers,
            requests=requests_and_notifications[0] if requests_and_notifications is not None else None,
            notifications=requests_and_notifications[1] if requests_and_notifications is not None else None,
            discovery_health=discovery_health,
            workspace_updates=workspace_updates,
            environment=environment,
        )

    def _derive_or_none(self, name: str, derive: Callable[[], _DerivedT]) -> _DerivedT | None:
        try:
            return derive()
        except (MindError, MngrError, OSError, RuntimeError) as e:
            logger.warning("Skipped the {} derive of one /ui/ws publish tick: {}", name, e)
            return None

    def _wait_out_min_gap_since_last_pass(self) -> None:
        if self._last_pass_started_at_monotonic is None:
            return
        remaining_seconds = self._last_pass_started_at_monotonic + self.min_seconds_between_passes - time.monotonic()
        if remaining_seconds > 0:
            # Waits on the stop event so a shutdown is not held up by the gap.
            self._stop_event.wait(remaining_seconds)

    def _run_publish_loop(self) -> None:
        is_running = True
        while is_running:
            self._wake_event.wait()
            self._wait_out_min_gap_since_last_pass()
            # Clear after the gap so every wake that arrived during it is
            # served by this pass, and BEFORE deriving so a producer firing
            # mid-derive re-sets the event and the loop runs another pass
            # rather than losing the wake.
            self._wake_event.clear()
            if self._stop_event.is_set():
                is_running = False
                continue
            self._last_pass_started_at_monotonic = time.monotonic()
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
