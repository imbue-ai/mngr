"""Telling every window to rebuild a recovered machine's view, at a moment the reload can survive.

While a machine is down each window goes on painting whatever it last served,
and recovering does not change the address that view was loaded from, so the
dead page sits there until the user navigates away and back. The remedy is a
``workspace_refresh`` frame on the recovery edge, which every window applies to
its own frame.

The edge arrives at the worst possible instant when the outage was the device's
network rather than the machine. The first probe that can succeed is the first
moment the interface is back, and that is exactly when the browser invalidates
every in-flight socket: the reload commits its document and then loses the
scripts that would have booted the page, leaving a blank frame that reads as
healthy from every angle the app can see.

So a refresh raised while this device is (or has just been) unable to reach
anything is held until the network has been back for a settle interval, and
published then. Nothing is lost by being late -- the machine is already back,
and the frame is already stale -- while being early is what produces the blank
page. A refresh raised on a device with no network trouble is published
immediately, as it always was.

Only a machine the network can actually explain is ever held. A workspace on an
on-device backend answers over loopback with the wifi off, so its recovery had
nothing to do with the interface and its reload has nothing to lose to one --
and holding it would be unbounded in a way the remote case is not: the release
is the network coming back, which for a remote machine is the same event it
needed anyway, and for an on-device one may never come at all.

This narrows the window rather than closing it: the settle is a guess about how
long an interface takes to stop flapping, and a reload issued after it can still
lose a race. What makes a lost reload recoverable is the embedder noticing that
the frame it armed never came up, which is the frontend's job and not this
module's.
"""

import threading
import time
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.ui_models import UiWorkspaceRefreshMessage
from imbue.minds.desktop_client.ui_publisher import UiStatePublisher
from imbue.minds.desktop_client.workspace_recovery import is_network_dependent_workspace
from imbue.mngr.primitives import AgentId

# How long the network must have been back before a held refresh is published.
#
# Measured either side of this on one laptop, across two wifi cycles in one
# session: a refresh published 0.2s after the interface returned lost its
# scripts to ERR_NETWORK_CHANGED and left the frame blank, and one published
# 18.9s after it loaded cleanly. Five seconds sits in that bracket, past the
# burst of interface notifications a wifi reassociation emits, and is not a
# delay the user can distinguish from the ~15s of probe failures it already took
# to call the machine stuck.
_DEFAULT_REFRESH_SETTLE_SECONDS: Final[float] = 5.0


class WorkspaceViewRefresher(MutableModel):
    """Recovery callback that tells every window to rebuild a recovered machine's view.

    Hung on the recovery rather than on the unattended start that usually causes
    it, so it also covers a machine that came back some other way: a cold boot
    that finished on its own, or the user starting a machine they had stopped.

    Constructed without a detector, it publishes every refresh immediately --
    the behaviour without any environment signals at all, and the same
    convention the unattended-recovery gate uses.
    """

    publisher: UiStatePublisher = Field(
        frozen=True, description="Publishes the refresh frame onto the /ui/ws channel every window is on."
    )
    backend_resolver: BackendResolverInterface | None = Field(
        default=None,
        frozen=True,
        description=(
            "Answers which backend a machine is on, so an on-device one is never held for a "
            "network it does not use. None holds by the device's condition alone."
        ),
    )
    connectivity_detector: ConnectivityDetector | None = Field(
        default=None,
        frozen=True,
        description=(
            "Answers whether this device can reach anything, so a refresh raised at a network "
            "transition can be held until the transition is over. None publishes unconditionally."
        ),
    )
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Parent group for the worker that waits out the settle."
    )
    settle_seconds: float = Field(
        default=_DEFAULT_REFRESH_SETTLE_SECONDS,
        frozen=True,
        description="How long the network must have been back before a held refresh is published.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Machines whose refresh is waiting for the network to settle. Per-process
    # and deliberately small: it is the set of views the app owes a repaint
    # right now, not a history.
    _held_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _held_agent_ids: set[str] = PrivateAttr(default_factory=set)
    # When connectivity was last seen to come back, so a refresh arriving just
    # *after* the recovery edge is held too. The two edges are independent and
    # land in either order: in the incident this was written for the health edge
    # came 0.2s first, and nothing makes that the ordering.
    _recovered_at_monotonic: float | None = PrivateAttr(default=None)

    def __call__(self, agent_id: AgentId) -> None:
        if not self._is_refresh_held(agent_id):
            self.publisher.publish_one_shot(UiWorkspaceRefreshMessage(agent_id=str(agent_id)))
            return
        with self._held_lock:
            self._held_agent_ids.add(str(agent_id))
        logger.info("Holding the view refresh of {}: this device's network is still coming back", agent_id)
        # The settle may have ended while the lines above ran, and the worker
        # that would have drained us has already been and gone: the detector
        # fires on the bad -> good edge alone, so no second one is coming and
        # the window would keep its dead page. Draining again here is the
        # remedy, and is a no-op while the network is still settling.
        if not self._is_refresh_held(agent_id):
            self._publish_held()

    def on_connectivity_recovered(self) -> None:
        """Start the settle window (registered as the detector's recovery callback).

        Spawned unconditionally rather than only when something is held: a
        machine whose own recovery edge lands *during* the window is held by
        ``__call__`` after this ran, and this worker is what releases it. The
        window is opened before the spawn for the same reason, so a refresh
        arriving between the two is caught by the worker that does exist.
        """
        with self._held_lock:
            self._recovered_at_monotonic = time.monotonic()
        try:
            self.concurrency_group.start_new_thread(
                target=self._publish_held_after_settle,
                name="workspace-view-refresh-settle",
                daemon=True,
                is_checked=False,
            )
        # The same two families the unattended-recovery gate names: an exited
        # group raises ConcurrencyGroupError, one that is shutting down raises
        # ConcurrencyExceptionGroup. An escape here would kill the detector's
        # probe thread, which this callback runs on.
        except (OSError, RuntimeError, ConcurrencyGroupError, ConcurrencyExceptionGroup) as exc:
            logger.warning("Could not start the view-refresh settle worker: {}", exc)
            self._publish_held_without_a_settle()

    def _publish_held_without_a_settle(self) -> None:
        """Close the settle window, then publish. For a recovery whose worker never started.

        The window has to go with the worker that would have ended it. A window
        left open holds every refresh raised inside it, and the drain that
        releases them is this same recovery edge -- which has already fired, and
        fires only on the bad -> good transition. So the machine would keep its
        dead page until the network next went down and came back, which on a
        device whose network is now fine is never.
        """
        with self._held_lock:
            self._recovered_at_monotonic = None
        self._publish_held()

    def _publish_held_after_settle(self) -> None:
        """Worker body: wait out the settle, then publish whatever accumulated during it.

        Waits on the group's shutdown event rather than sleeping, so a quit
        inside the window exits at once -- and drops the held refreshes with it,
        which is correct: there is no window left to repaint.

        The condition is re-read at the end rather than assumed from the start.
        An interface that came back and went again inside the window -- a wifi
        reassociation, the thing this settle is here to outlast -- would
        otherwise be published into on the strength of a recovery that no longer
        holds, which is the blank frame this module exists to prevent. Nothing
        is stranded by declining: a reading that still blocks is one the detector
        is still watching, so the next recovery arms another worker, and a
        settle window newer than this one exists only because a newer recovery
        already did.
        """
        if self.concurrency_group.shutdown_event.wait(timeout=self.settle_seconds):
            return
        if self._is_network_settling():
            return
        self._publish_held()

    def _publish_held(self) -> None:
        with self._held_lock:
            held_agent_ids = sorted(self._held_agent_ids)
            self._held_agent_ids.clear()
        for aid_str in held_agent_ids:
            logger.info("Publishing the held view refresh of {}: the network has settled", aid_str)
            self.publisher.publish_one_shot(UiWorkspaceRefreshMessage(agent_id=aid_str))

    def _is_refresh_held(self, agent_id: AgentId) -> bool:
        """Whether *this machine's* refresh must wait for the network, rather than publish now.

        Both halves are required, the same pair the recovery card and the notice
        band ask: a network this refresh could be lost to, and a machine that
        reaches its own view over that network. A workspace on an on-device
        backend answers over loopback, so the wifi being off neither caused its
        outage nor threatens its reload -- and it is the half that cannot be left
        out, because the hold it would enter is released by the network coming
        back, which on a device that stays offline never happens.
        """
        if self.backend_resolver is not None and not is_network_dependent_workspace(self.backend_resolver, agent_id):
            return False
        return self._is_network_settling()

    def _is_network_settling(self) -> bool:
        """Whether a refresh published right now would be aimed at a transitioning network.

        True while a *confirmed* device-level block is in force, and for the
        settle interval after one lifts. An UNKNOWN reading -- none taken yet, or
        one a wake invalidated -- yields ``EnvironmentBlock.NONE`` and so holds
        nothing, which is the module-wide rule for a reading that knows nothing.
        """
        detector = self.connectivity_detector
        if detector is None:
            return False
        if detector.get_reading().environment_block is not EnvironmentBlock.NONE:
            return True
        with self._held_lock:
            recovered_at = self._recovered_at_monotonic
        if recovered_at is None:
            return False
        # Monotonic freezes across a macOS sleep, so a window straddling one
        # reads as shorter than it was. The consequence is a refresh held for up
        # to one settle longer than needed, which is the safe direction: this
        # can delay a repaint, never publish one early.
        return (time.monotonic() - recovered_at) < self.settle_seconds
