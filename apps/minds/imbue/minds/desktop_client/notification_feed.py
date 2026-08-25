"""The durable in-memory notification feed behind the ``notifications`` channel frame.

The feed is a *derived* reconciliation over the request inbox, not an event
log: on every publish tick (and on every WS-connect snapshot build) the
notifications derive hands ``reconcile`` the currently-displayable pending
requests (with their display fields already resolved) plus the recorded
grant/deny responses, and the feed diffs that view against its entries:

- a displayable pending request it has never seen becomes a new unresolved
  entry (display fields snapshotted so the row still renders after the
  source request is gone);
- an entry whose request has a response resolves as approved/denied;
- an unresolved entry whose request vanished without a response resolves as
  "closed" (the desktop client does no request cleanup on workspace destroy;
  the id simply drops out of the displayable set);
- a "closed" entry whose request reappears (still without a response)
  reopens. Displayability flaps when a workspace transiently fails display
  resolution, and shortly after startup the gateway follow stream re-delivers
  request events, so "vanished" must never be a terminal state on its own.

Entries are stamped with the request event's own timestamp (``requested_at``
on the card), so ordering and relative times survive restarts instead of
resetting to reconcile time.

A *new* entry triggers at most one OS dispatch ever (creation happens exactly
once under the lock, only the creating call dispatches, and a dispatched
request id never dispatches again even if its evicted entry is recreated).
Dispatch additionally requires ALL of:

- the request was filed after the feed came up (startup backfill, where the
  gateway re-delivers every still-pending request, stays silent);
- the dispatcher's active channel is Electron (in browser mode the SPA
  renderer owns OS delivery via Web Notifications, so dispatching here too
  would double-deliver);
- the injected preferences allow it: master toggle on, style "os" or "both";
- no *focused* connected UI window is currently displaying the asking
  workspace (the in-app review popup already covers it there). Being
  displayed in an unfocused window does not count: the reader is not looking
  at that popup, so this is different from the in-app toast's own on-screen
  check, which does not require focus.

Thread-safety: ``reconcile`` is called from the publisher thread and from
WS-connect snapshot builds, so all entry state is guarded by one lock.
"""

import threading
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.ui_models import NotificationOutcome
from imbue.minds.desktop_client.ui_models import UiNotificationEntry
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.errors import NaiveTimestampError

# Entry cap. Eviction only ever removes resolved entries, so the feed can
# exceed this under pathological all-unresolved load.
_FEED_CAP: Final[int] = 50

# The styles that include an OS nudge (the remaining style, cards, is
# in-app only and rendered by the frontend from the feed frame itself).
_OS_DISPATCH_STYLES: Final[frozenset[NotificationStyle]] = frozenset({NotificationStyle.OS, NotificationStyle.BOTH})

# OS-notification body when the request carries no rationale line.
_FALLBACK_DISPATCH_MESSAGE: Final[str] = "Waiting on your review."


class NotificationDispatchPreferences(FrozenModel):
    """The stored user preferences the feed consults before nudging the OS."""

    is_enabled: bool = Field(description="Master notifications toggle")
    style: NotificationStyle = Field(description="Delivery style for feed-backed notifications")


class PendingNotificationCard(FrozenModel):
    """Display fields for one displayable pending request (the feed's per-request input).

    Derived by the notifications derive exactly the way the inbox builds its
    cards, so a feed entry and the inbox row it mirrors always agree.
    """

    request_id: str = Field(description="The request event id")
    requested_at: str = Field(description="The request event's own ISO-8601 timestamp (when the request was filed)")
    title: str = Field(description="Headline (the request's display name, as the inbox card shows it)")
    body: str = Field(description="The request's rationale line; '' when none")
    workspace_agent_id: str = Field(description="Origin workspace's primary agent id; '' when unresolvable")
    workspace_name: str = Field(description="Origin workspace's display name")
    workspace_accent: str = Field(description="Origin workspace's ``#rrggbb`` accent")
    service_name: str = Field(description="Catalog service for the brand mark; '' when none")


class NotificationFeed(MutableModel):
    """Lock-guarded notification feed reconciled from the pending-request view."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    notification_dispatcher: NotificationDispatcher | None = Field(
        frozen=True, description="OS notification dispatcher; None disables OS dispatch entirely"
    )
    get_dispatch_preferences: Callable[[], NotificationDispatchPreferences] = Field(
        frozen=True, description="Live reader of the stored notification preferences"
    )
    get_connected_focused_workspace_agent_ids: Callable[[], tuple[str, ...]] = Field(
        frozen=True,
        description="Live reader of the workspace agent ids a focused connected UI window is currently displaying",
    )
    constructed_at: datetime = Field(
        frozen=True,
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the feed came up; requests filed before this never OS-dispatch (startup backfill is silent)",
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _entry_by_id: dict[str, UiNotificationEntry] = PrivateAttr(default_factory=dict)
    # Request ids that have already OS-dispatched in this process. An entry is
    # created at most once, but a *resolved* entry can be evicted by the cap
    # and later recreated when its request reappears; this set keeps the
    # recreation from producing a second banner.
    _dispatched_request_ids: set[str] = PrivateAttr(default_factory=set)

    def model_post_init(self, __context: object) -> None:
        """Reject a naive ``constructed_at`` so the backfill cutoff comparison is unambiguous."""
        if self.constructed_at.tzinfo is None:
            raise NaiveTimestampError(
                f"NotificationFeed requires a timezone-aware constructed_at, got naive {self.constructed_at!r}"
            )

    def reconcile(
        self,
        pending_cards: tuple[PendingNotificationCard, ...],
        responses_by_request_id: Mapping[str, NotificationOutcome],
    ) -> UiNotificationsMessage:
        """Reconcile the feed against the current pending view and return the full frame.

        ``responses_by_request_id`` carries recorded grant/deny outcomes only
        (APPROVED/DENIED -- CLOSED is the feed's own verdict for vanished
        requests and never a recorded response).
        """
        with self._lock:
            newly_created = self._create_missing_entries_locked(pending_cards)
            self._apply_resolutions_locked(pending_cards, responses_by_request_id)
            self._evict_beyond_cap_locked()
            message = self._build_message_locked()
            # A request can be created and resolved by this very pass (its
            # response landed within one publish interval): the banner would
            # announce an ask the feed already knows is settled, so only
            # entries still unresolved at the end of the pass dispatch. An
            # unresolved entry is never evicted, so a missing id means it
            # resolved and was evicted -- one check covers both.
            dispatchable = [
                entry
                for entry in newly_created
                if (current := self._entry_by_id.get(entry.id)) is not None and not current.is_resolved
            ]
        # Dispatch outside the lock: only the call that created an entry ever
        # dispatches it, so a concurrent reconcile cannot double-nudge.
        for entry in dispatchable:
            self._dispatch_new_entry(entry)
        return message

    def _create_missing_entries_locked(
        self,
        pending_cards: tuple[PendingNotificationCard, ...],
    ) -> list[UiNotificationEntry]:
        newly_created: list[UiNotificationEntry] = []
        for card in pending_cards:
            if card.request_id in self._entry_by_id:
                continue
            entry = UiNotificationEntry(
                id=card.request_id,
                # The request's own timestamp, not reconcile time: backfilled
                # entries keep their true order and relative ages.
                created_at=card.requested_at,
                is_resolved=False,
                outcome=None,
                title=card.title,
                body=card.body,
                request_id=card.request_id,
                workspace_agent_id=card.workspace_agent_id,
                workspace_name=card.workspace_name,
                workspace_accent=card.workspace_accent,
                service_name=card.service_name,
            )
            self._entry_by_id[card.request_id] = entry
            newly_created.append(entry)
        return newly_created

    def _apply_resolutions_locked(
        self,
        pending_cards: tuple[PendingNotificationCard, ...],
        responses_by_request_id: Mapping[str, NotificationOutcome],
    ) -> None:
        """Move every entry to its target resolution state for the current view.

        Computes the target outcome per entry (None = unresolved) and writes
        only when it differs, so an unchanged feed serializes identically and
        the publisher's frame diffing stays quiet.
        """
        pending_request_ids = {card.request_id for card in pending_cards}
        for entry_id, entry in self._entry_by_id.items():
            response_outcome = responses_by_request_id.get(entry.request_id)
            if response_outcome is not None:
                # A recorded response is authoritative, forever.
                target_outcome: NotificationOutcome | None = response_outcome
            elif entry.request_id in pending_request_ids:
                # Self-healing: a "closed" entry whose request reappeared
                # reopens; approved/denied entries stay receipts even if
                # their id flaps back into the displayable set.
                target_outcome = None if entry.outcome == NotificationOutcome.CLOSED else entry.outcome
            else:
                # Vanished without a response (workspace destroyed/stopped):
                # close, keeping any outcome already recorded.
                target_outcome = entry.outcome if entry.is_resolved else NotificationOutcome.CLOSED
            target_is_resolved = target_outcome is not None
            if (entry.is_resolved, entry.outcome) != (target_is_resolved, target_outcome):
                self._entry_by_id[entry_id] = entry.model_copy_update(
                    to_update(entry.field_ref().is_resolved, target_is_resolved),
                    to_update(entry.field_ref().outcome, target_outcome),
                )

    def _evict_beyond_cap_locked(self) -> None:
        overflow = len(self._entry_by_id) - _FEED_CAP
        if overflow <= 0:
            return
        resolved_oldest_first = sorted(
            (entry for entry in self._entry_by_id.values() if entry.is_resolved),
            key=_recency_sort_key,
        )
        for entry in resolved_oldest_first[:overflow]:
            del self._entry_by_id[entry.id]

    def _build_message_locked(self) -> UiNotificationsMessage:
        unresolved = sorted(
            (entry for entry in self._entry_by_id.values() if not entry.is_resolved),
            key=_recency_sort_key,
            reverse=True,
        )
        resolved = sorted(
            (entry for entry in self._entry_by_id.values() if entry.is_resolved),
            key=_recency_sort_key,
            reverse=True,
        )
        return UiNotificationsMessage(entries=tuple(unresolved + resolved), unresolved_count=len(unresolved))

    def _dispatch_new_entry(self, entry: UiNotificationEntry) -> None:
        """Dispatch one newly-created entry to the OS, or log exactly why not.

        Every early return here is a legitimate, intentional "stay silent"
        case (see each comment) -- but silence and a bug both LOOK like
        "notifications aren't working", so each gate logs the reason at
        debug level. ``uv run minds`` writes debug lines to minds.log even
        when the console only shows info-and-up, so `grep _dispatch_new_entry
        minds.log` (or the surrounding gate names) answers "why didn't this
        fire" without adding any noise to the normal console.
        """
        if self.notification_dispatcher is None:
            logger.debug("notification {}: no dispatcher configured", entry.request_id)
            return
        if not self.notification_dispatcher.is_electron:
            # In browser mode the SPA renderer owns OS delivery (a Web
            # Notification fired from the feed frame), so dispatching here too
            # would double-deliver. The dispatcher's osascript/tkinter
            # fallbacks remain for the non-feed producers (agent-sent
            # notifications, backup failures), which have no renderer path.
            logger.debug(
                "notification {}: not the Electron channel (browser mode owns OS delivery there)",
                entry.request_id,
            )
            return
        requested_at = _parse_requested_at(entry.created_at)
        if requested_at is None or requested_at <= self.constructed_at:
            # Startup backfill: the feed is in-memory and the gateway
            # re-delivers every still-pending request at launch, so entries
            # whose request predates the feed are records, not news. An
            # unparseable timestamp counts as old (silent).
            logger.debug(
                "notification {}: filed before this launch (requested_at={}, launched={}) -- backfill, staying silent",
                entry.request_id,
                entry.created_at,
                self.constructed_at.isoformat(),
            )
            return
        preferences = self.get_dispatch_preferences()
        if not preferences.is_enabled:
            logger.debug("notification {}: master notifications toggle is off", entry.request_id)
            return
        if preferences.style not in _OS_DISPATCH_STYLES:
            logger.debug(
                "notification {}: delivery style is {!r} (cards-only), no OS nudge",
                entry.request_id,
                preferences.style,
            )
            return
        if entry.workspace_agent_id and entry.workspace_agent_id in self.get_connected_focused_workspace_agent_ids():
            # A focused connected window is already showing the asking
            # workspace, and the in-app review popup covers it there: stay
            # silent. Being displayed in an unfocused window (alt-tabbed away,
            # behind another app) does not count -- the reader is not looking
            # at that popup, so the OS banner is the only nudge they will see.
            logger.debug(
                "notification {}: {} is already on screen in a focused window -- the in-chat card covers it",
                entry.request_id,
                entry.workspace_name,
            )
            return
        with self._lock:
            # Re-verify against the CURRENT entry state, not the possibly-stale
            # `entry` parameter: reconcile() is called from multiple threads
            # (the publisher and WS-connect snapshot builds -- see the module
            # docstring), so a concurrent reconcile() could have resolved this
            # very entry while the lock-free gates above ran (they include a
            # live preferences read from disk). Checked atomically with the
            # dedup-set write so the go/no-go decision and the dedup marker
            # can never disagree.
            current = self._entry_by_id.get(entry.request_id)
            if current is None or current.is_resolved:
                logger.debug(
                    "notification {}: resolved or evicted before dispatch -- staying silent",
                    entry.request_id,
                )
                return
            if entry.request_id in self._dispatched_request_ids:
                logger.debug("notification {}: already dispatched once this process", entry.request_id)
                return
            self._dispatched_request_ids.add(entry.request_id)
        # The deep link the Electron click handler navigates to: the asking
        # workspace, with the review popup auto-opened. No workspace agent id
        # means nowhere sensible to land, so no url in that case.
        url = f"/workspace/{entry.workspace_agent_id}?review={entry.request_id}" if entry.workspace_agent_id else None
        request = NotificationRequest(
            message=entry.body or _FALLBACK_DISPATCH_MESSAGE,
            title=f"{entry.workspace_name} asks — {entry.title}",
            url=url,
        )
        logger.info(
            "notification {}: dispatching an OS banner for {} ({})",
            entry.request_id,
            entry.workspace_name,
            entry.title,
        )
        self.notification_dispatcher.dispatch(request, agent_display_name=entry.workspace_name)


def _parse_requested_at(value: str) -> datetime | None:
    """Parse an entry's request timestamp; naive values are assumed UTC, unparseable ones yield None."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _recency_sort_key(entry: UiNotificationEntry) -> tuple[str, str]:
    """Sort key ordering entries oldest-first (reverse for newest-first), id-tie-broken for determinism."""
    return (entry.created_at, entry.id)
