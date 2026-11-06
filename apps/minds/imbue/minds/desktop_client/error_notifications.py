"""Notification-feed entries for error states the user would otherwise miss while the app is out of sight.

Each reporter turns an edge some existing detector already fires into a
system event on the notification feed, so the entry gets everything a feed
entry gets: the bell, the in-app toast, the OS banner when no focused main
window can show the toast, the master toggle and delivery style from
Settings, and the click routing. Nothing here polls.

A detector can re-report a condition it is still in, and a flapping one can
leave and re-enter it, so a reporter for such a condition passes each report
through an :class:`ErrorNotificationGate` first: one entry per episode of a
condition, and none for the same condition again within a cooldown.
"""

import threading
import time
from collections.abc import Callable
from typing import Final
from typing import assert_never

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.notification_feed import SystemEventCard
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.mngr.primitives import AgentId

# The shortest time between two entries about the same condition.
ERROR_NOTIFICATION_COOLDOWN_SECONDS: Final[float] = 30 * 60.0

ACCOUNT_SIGNED_OUT_TITLE: Final[str] = "Signed out"

WORKSPACE_RECOVERY_FAILED_TITLE: Final[str] = "Workspace isn't responding"

_WORKSPACE_RECOVERY_FAILED_BODY: Final[str] = "Studio couldn't start it again. Open it to restart it."


def build_workspace_system_event_card(
    agent_id: AgentId, title: str, body: str, link_path: str, backend_resolver: BackendResolverInterface
) -> SystemEventCard:
    """A system event about one workspace, headed by its display name and accent as the resolver knows them now."""
    return SystemEventCard(
        title=title,
        body=body,
        workspace_agent_id=str(agent_id),
        workspace_name=backend_resolver.get_workspace_name(agent_id) or str(agent_id)[:8],
        workspace_accent=backend_resolver.get_workspace_color(agent_id) or DEFAULT_WORKSPACE_COLOR,
        link_path=link_path,
    )


class ErrorNotificationGate(MutableModel):
    """Decides which reports of an error condition become feed entries.

    A condition is named by a key. An *episode* is the span from the condition
    being reported to it ending; only its first report is admitted, and not
    even that one when the same key was admitted less than
    ``cooldown_seconds`` ago.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    cooldown_seconds: float = Field(
        default=ERROR_NOTIFICATION_COOLDOWN_SECONDS,
        frozen=True,
        description="Shortest time between two admitted reports of the same key",
    )
    get_now_seconds: Callable[[], float] = Field(
        default=time.time,
        frozen=True,
        description=(
            "Clock the cooldown is measured on. The wall one, because a suspend does not advance the "
            "monotonic clock, so a cooldown measured on it would outlast its length by every sleep it spans"
        ),
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _open_episode_keys: set[str] = PrivateAttr(default_factory=set)
    _last_admitted_at_by_key: dict[str, float] = PrivateAttr(default_factory=dict)

    def admit_episode_start(self, key: str) -> bool:
        """Report the condition ``key`` names; True only for the first report of an episode outside the cooldown."""
        with self._lock:
            if key in self._open_episode_keys:
                return False
            self._open_episode_keys.add(key)
            now = self.get_now_seconds()
            last_admitted_at = self._last_admitted_at_by_key.get(key)
            if last_admitted_at is not None and now - last_admitted_at < self.cooldown_seconds:
                return False
            self._last_admitted_at_by_key[key] = now
            return True

    def end_episode(self, key: str) -> None:
        """The condition ``key`` names is over; its next report starts a new episode."""
        with self._lock:
            self._open_episode_keys.discard(key)


class AccountSignedOutReporter(FrozenModel):
    """The sync loop's report that it signed out an account whose session could not be refreshed.

    Account-level: the entry reaches that account's windows and lands on the
    accounts page, where the account can sign in again. Every report is news:
    the loop stops refreshing an account it signed out, so it can only report
    the same account again after the user has signed it back in.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, extra="forbid")

    notification_feed: NotificationFeed = Field(frozen=True, description="The feed the entry is appended to")

    def __call__(self, user_id: str, account_email: str) -> None:
        self.notification_feed.append_system_event(
            SystemEventCard(
                title=ACCOUNT_SIGNED_OUT_TITLE,
                body=(
                    f"Your session for {account_email} expired. Sign in again to keep its workspaces syncing "
                    "and reachable."
                ),
                workspace_agent_id="",
                account_id=user_id,
                workspace_name=account_email,
                workspace_accent=DEFAULT_WORKSPACE_COLOR,
            )
        )


class WorkspaceRecoveryFailureReporter(FrozenModel):
    """Health-tracker on-change callback that reports a workspace whose recovery failed.

    One entry per outage: the tracker re-fires RECOVERY_FAILED for every
    further failed attempt, and only a return to HEALTHY ends the episode.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, extra="forbid")

    notification_feed: NotificationFeed = Field(frozen=True, description="The feed the entry is appended to")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Resolves the workspace's display")
    gate: ErrorNotificationGate = Field(frozen=True, description="Dedupes repeated failures of one workspace")

    def __call__(self, agent_id: AgentId, health: AgentHealth) -> None:
        key = str(agent_id)
        match health:
            case AgentHealth.HEALTHY:
                self.gate.end_episode(key)
            case AgentHealth.STUCK | AgentHealth.RECOVERING:
                pass
            case AgentHealth.RECOVERY_FAILED:
                if self.gate.admit_episode_start(key):
                    self._report(agent_id)
            case _ as unreachable:
                assert_never(unreachable)

    def _report(self, agent_id: AgentId) -> None:
        self.notification_feed.append_system_event(
            build_workspace_system_event_card(
                agent_id=agent_id,
                title=WORKSPACE_RECOVERY_FAILED_TITLE,
                body=_WORKSPACE_RECOVERY_FAILED_BODY,
                link_path=f"/workspace/{agent_id}",
                backend_resolver=self.backend_resolver,
            )
        )
