"""The durable in-memory notification feed behind the ``notifications`` channel frame.

The feed is the one pipeline every notification goes through: the bell, the
dock badge, the in-app toasts and the OS banners all read from it. It holds
entries of three kinds, each with its own lifecycle:

- Permission requests are a *derived* reconciliation over the request inbox,
  not an event log: on every publish tick (and on every WS-connect snapshot
  build) the notifications derive hands ``reconcile`` the currently-displayable
  pending requests (with their display fields already resolved) plus the
  recorded grant/deny responses, and the feed diffs that view against its
  entries. A displayable pending request it has never seen becomes a new
  unresolved entry (display fields snapshotted so the row still renders after
  the source request is gone); an entry whose request has a response resolves
  as approved/denied; an unresolved entry whose request vanished without a
  response resolves as "closed"; a "closed" entry whose request reappears
  (still without a response) reopens. Displayability flaps when a workspace
  transiently fails display resolution, and shortly after startup the gateway
  follow stream re-delivers request events, so "vanished" must never be a
  terminal state on its own. Clearing a request entry hides it for good, across
  restarts when the feed is given a file to remember cleared ids in (the
  request itself stays pending in the inbox).
- Agent messages are appended by the agent notifications route. They leave the
  feed when their workspace is navigated to or when they are cleared (which is
  what opening one does), and drop out when their workspace leaves the list;
  they never become receipts.
- System events are appended by the backup producers. Clearing removes them.

Entries are stamped with their event's own timestamp (``requested_at`` on a
request card, the time the gateway filed it), so ordering and relative times
survive restarts instead of resetting to reconcile time.

A *new* entry triggers at most one OS dispatch ever (creation happens exactly
once under the lock, only the creating call dispatches, and a dispatched
request id never dispatches again even if its evicted entry is recreated).
Dispatch additionally requires ALL of:

- for a request, that it was filed after the feed came up (startup backfill,
  where the gateway re-delivers every still-pending request, stays silent; a
  request with no filing time counts as backfill);
- the injected preferences allow it: master toggle on, style "os" or "both";
- for a workspace-scoped entry, no *focused* connected UI window is currently
  displaying that workspace (the surface on screen already shows it there).
  Being displayed in an unfocused window does not count: the reader is not
  looking at that window. Account-level entries always fire.

Thread-safety: ``reconcile`` is called from the publisher thread and from
WS-connect snapshot builds, and the append/clear paths from request threads,
so all entry state is guarded by one lock.
"""

import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import assert_never
from uuid import uuid4

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
from imbue.minds.desktop_client.ui_models import NotificationKind
from imbue.minds.desktop_client.ui_models import NotificationOutcome
from imbue.minds.desktop_client.ui_models import UiNotificationEntry
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.errors import NaiveTimestampError
from imbue.mngr.utils.file_utils import atomic_write

# Entry cap. Eviction only ever removes resolved entries, so the feed can
# exceed this under pathological all-unresolved load.
_FEED_CAP: Final[int] = 50

# The styles that include an OS nudge (the remaining style, cards, is
# in-app only and rendered by the frontend from the feed frame itself).
_OS_DISPATCH_STYLES: Final[frozenset[NotificationStyle]] = frozenset({NotificationStyle.OS, NotificationStyle.BOTH})

# How many cleared request ids the feed remembers; the oldest are forgotten first.
_CLEARED_REQUEST_ID_CAP: Final[int] = 1000

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
    requested_at: str | None = Field(
        description="When the request was filed (ISO-8601); None when unknown, which counts as filed before launch"
    )
    title: str = Field(description="Headline (the request's display name, as the inbox card shows it)")
    body: str = Field(description="The request's rationale line; '' when none")
    workspace_agent_id: str = Field(description="Origin workspace's primary agent id; '' when unresolvable")
    workspace_name: str = Field(description="Origin workspace's display name")
    workspace_accent: str = Field(description="Origin workspace's ``#rrggbb`` accent")
    service_name: str = Field(description="Catalog service for the brand mark; '' when none")


class AgentMessageCard(FrozenModel):
    """A chat agent's note to the user, as the agent notifications route resolved it."""

    chat_agent_id: str = Field(description="Stable chat id where a click lands; the sender's agent id for older chats")
    chat_name: str = Field(description="The chat's display name (the entry's headline)")
    body: str = Field(description="The message text")
    workspace_agent_id: str = Field(description="The chat's workspace (its primary agent id); '' when unresolvable")
    workspace_name: str = Field(description="The workspace's display name")
    workspace_accent: str = Field(description="The workspace's ``#rrggbb`` accent")


class SystemEventCard(FrozenModel):
    """Something the app did on the user's behalf that they should hear about."""

    title: str = Field(description="The event name (the entry's headline)")
    body: str = Field(description="The outcome detail")
    workspace_agent_id: str = Field(description="The workspace it concerns; '' for an account-level event")
    workspace_name: str = Field(description="The workspace's display name, or the account for an account-level event")
    workspace_accent: str = Field(description="The workspace's ``#rrggbb`` accent")


class ClearedNotificationRequests(FrozenModel):
    """The on-disk record of the requests the user cleared from the feed."""

    model_config = ConfigDict(extra="ignore")

    request_ids: tuple[str, ...] = Field(description="Cleared request ids, oldest first")


class NotificationFeed(MutableModel):
    """Lock-guarded notification feed: reconciled requests plus appended messages and events."""

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
    cleared_request_ids_path: Path | None = Field(
        default=None,
        frozen=True,
        description="File remembering cleared requests across restarts; None remembers them for this process only",
    )
    on_change: Callable[[], None] | None = Field(
        default=None,
        description=(
            "Called after an append, clear or read changed the feed outside a reconcile, so the "
            "publisher re-derives the frame; bound once the publisher exists"
        ),
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _entry_by_id: dict[str, UiNotificationEntry] = PrivateAttr(default_factory=dict)
    # Request ids that have already OS-dispatched in this process. An entry is
    # created at most once, but a *resolved* entry can be evicted by the cap
    # and later recreated when its request reappears; this set keeps the
    # recreation from producing a second banner.
    _dispatched_request_ids: set[str] = PrivateAttr(default_factory=set)
    # Request ids the user cleared from the feed, oldest first. The request
    # stays pending in the inbox, so the reconcile keeps seeing it; this keeps
    # it from coming straight back as a new entry.
    _cleared_request_ids: dict[str, None] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        """Reject a naive ``constructed_at`` and load the cleared requests remembered on disk."""
        if self.constructed_at.tzinfo is None:
            raise NaiveTimestampError(
                f"NotificationFeed requires a timezone-aware constructed_at, got naive {self.constructed_at!r}"
            )
        self._cleared_request_ids = dict.fromkeys(self._read_cleared_request_ids())

    def _read_cleared_request_ids(self) -> tuple[str, ...]:
        if self.cleared_request_ids_path is None:
            return ()
        try:
            raw = self.cleared_request_ids_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as e:
            logger.warning("Could not read cleared notifications {}: {}", self.cleared_request_ids_path, e)
            return ()
        try:
            return ClearedNotificationRequests.model_validate_json(raw).request_ids
        except ValueError as e:
            logger.warning(
                "Cleared notifications {} are not valid; ignoring them: {}", self.cleared_request_ids_path, e
            )
            return ()

    def reconcile(
        self,
        pending_cards: tuple[PendingNotificationCard, ...],
        responses_by_request_id: Mapping[str, NotificationOutcome],
        known_workspace_agent_ids: AbstractSet[str] | None = None,
    ) -> UiNotificationsMessage:
        """Reconcile the feed against the current pending view and return the full frame.

        ``responses_by_request_id`` carries recorded grant/deny outcomes only
        (APPROVED/DENIED -- CLOSED is the feed's own verdict for vanished
        requests and never a recorded response). ``known_workspace_agent_ids``
        is the set of workspaces known to still exist; an agent message whose
        workspace is not among them drops out for good (there is nowhere for its
        click to land), so the caller passes a set whose absences are evidence,
        not a bare live listing. One whose workspace never resolved (``""``) had
        none to leave and stays until cleared. None skips that check.
        """
        with self._lock:
            newly_filed = self._create_missing_request_entries_locked(pending_cards)
            self._apply_request_resolutions_locked(pending_cards, responses_by_request_id)
            if known_workspace_agent_ids is not None:
                self._drop_orphaned_agent_messages_locked(known_workspace_agent_ids)
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
                for entry in newly_filed
                if (current := self._entry_by_id.get(entry.id)) is not None and not current.is_resolved
            ]
        # Dispatch outside the lock: only the call that created an entry ever
        # dispatches it, so a concurrent reconcile cannot double-nudge.
        for entry in dispatchable:
            self._dispatch_new_entry(entry)
        return message

    def append_agent_message(self, card: AgentMessageCard, sent_at: datetime | None = None) -> UiNotificationEntry:
        """Record a chat agent's message and nudge the OS for it."""
        entry = UiNotificationEntry(
            id=f"msg-{uuid4().hex}",
            kind=NotificationKind.AGENT_MESSAGE,
            created_at=(sent_at or datetime.now(timezone.utc)).isoformat(),
            is_resolved=False,
            outcome=None,
            title=card.chat_name,
            body=card.body,
            request_id="",
            chat_agent_id=card.chat_agent_id,
            workspace_agent_id=card.workspace_agent_id,
            workspace_name=card.workspace_name,
            workspace_accent=card.workspace_accent,
            service_name="",
        )
        self._append_entry(entry)
        return entry

    def append_system_event(self, card: SystemEventCard, happened_at: datetime | None = None) -> UiNotificationEntry:
        """Record something the app did and nudge the OS for it."""
        entry = UiNotificationEntry(
            id=f"sys-{uuid4().hex}",
            kind=NotificationKind.SYSTEM_EVENT,
            created_at=(happened_at or datetime.now(timezone.utc)).isoformat(),
            is_resolved=False,
            outcome=None,
            title=card.title,
            body=card.body,
            request_id="",
            chat_agent_id="",
            workspace_agent_id=card.workspace_agent_id,
            workspace_name=card.workspace_name,
            workspace_accent=card.workspace_accent,
            service_name="",
        )
        self._append_entry(entry)
        return entry

    def _append_entry(self, entry: UiNotificationEntry) -> None:
        with self._lock:
            self._entry_by_id[entry.id] = entry
            self._evict_beyond_cap_locked()
        self._notify_change()
        self._dispatch_new_entry(entry)

    def mark_workspace_read(self, workspace_agent_id: str) -> bool:
        """The user navigated to the workspace: its agent messages are read. Returns whether the feed changed."""
        if workspace_agent_id == "":
            return False
        with self._lock:
            read_ids = [
                entry.id
                for entry in self._entry_by_id.values()
                if entry.kind == NotificationKind.AGENT_MESSAGE and entry.workspace_agent_id == workspace_agent_id
            ]
            for entry_id in read_ids:
                del self._entry_by_id[entry_id]
        if len(read_ids) == 0:
            return False
        self._notify_change()
        return True

    def clear(self, entry_id: str) -> bool:
        """Remove one entry of any kind. A cleared request never re-enters. Returns whether the feed changed."""
        with self._lock:
            entry = self._entry_by_id.pop(entry_id, None)
            if entry is None:
                return False
            if entry.kind == NotificationKind.PERMISSION_REQUEST:
                self._remember_cleared_requests_locked((entry.request_id,))
        self._notify_change()
        return True

    def clear_all(self) -> bool:
        """Remove every entry, receipts included. Returns whether the feed changed."""
        with self._lock:
            if len(self._entry_by_id) == 0:
                return False
            self._remember_cleared_requests_locked(
                tuple(
                    entry.request_id
                    for entry in self._entry_by_id.values()
                    if entry.kind == NotificationKind.PERMISSION_REQUEST
                )
            )
            self._entry_by_id.clear()
        self._notify_change()
        return True

    def _remember_cleared_requests_locked(self, request_ids: tuple[str, ...]) -> None:
        if len(request_ids) == 0:
            return
        for request_id in request_ids:
            self._cleared_request_ids.pop(request_id, None)
            self._cleared_request_ids[request_id] = None
        while len(self._cleared_request_ids) > _CLEARED_REQUEST_ID_CAP:
            del self._cleared_request_ids[next(iter(self._cleared_request_ids))]
        if self.cleared_request_ids_path is None:
            return
        record = ClearedNotificationRequests(request_ids=tuple(self._cleared_request_ids))
        try:
            atomic_write(self.cleared_request_ids_path, record.model_dump_json(indent=2))
        except OSError as e:
            # The clear still holds for this run; only its survival across a restart is lost.
            logger.warning("Could not save cleared notifications to {}: {}", self.cleared_request_ids_path, e)

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _create_missing_request_entries_locked(
        self,
        pending_cards: tuple[PendingNotificationCard, ...],
    ) -> list[UiNotificationEntry]:
        """Create entries for requests the feed has not seen; return the ones filed after it came up."""
        newly_filed: list[UiNotificationEntry] = []
        for card in pending_cards:
            if card.request_id in self._entry_by_id or card.request_id in self._cleared_request_ids:
                continue
            entry = UiNotificationEntry(
                id=card.request_id,
                kind=NotificationKind.PERMISSION_REQUEST,
                # The request's own timestamp, not reconcile time: backfilled
                # entries keep their true order and relative ages.
                created_at=card.requested_at or datetime.now(timezone.utc).isoformat(),
                is_resolved=False,
                outcome=None,
                title=card.title,
                body=card.body,
                request_id=card.request_id,
                chat_agent_id="",
                workspace_agent_id=card.workspace_agent_id,
                workspace_name=card.workspace_name,
                workspace_accent=card.workspace_accent,
                service_name=card.service_name,
            )
            self._entry_by_id[card.request_id] = entry
            requested_at = None if card.requested_at is None else _parse_requested_at(card.requested_at)
            if requested_at is not None and requested_at > self.constructed_at:
                newly_filed.append(entry)
            else:
                # Startup backfill: the feed is in-memory and the gateway
                # re-delivers every still-pending request at launch, so entries
                # whose request predates the feed are records, not news. A
                # missing or unparseable timestamp counts as old (silent).
                logger.debug(
                    "notification {}: filed before this launch (requested_at={}, launched={}) -- backfill, staying silent",
                    entry.id,
                    card.requested_at,
                    self.constructed_at.isoformat(),
                )
        return newly_filed

    def _apply_request_resolutions_locked(
        self,
        pending_cards: tuple[PendingNotificationCard, ...],
        responses_by_request_id: Mapping[str, NotificationOutcome],
    ) -> None:
        """Move every request entry to its target resolution state for the current view.

        Computes the target outcome per entry (None = unresolved) and writes
        only when it differs, so an unchanged feed serializes identically and
        the publisher's frame diffing stays quiet.
        """
        pending_request_ids = {card.request_id for card in pending_cards}
        for entry_id, entry in self._entry_by_id.items():
            if entry.kind != NotificationKind.PERMISSION_REQUEST:
                continue
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

    def _drop_orphaned_agent_messages_locked(self, known_workspace_agent_ids: AbstractSet[str]) -> None:
        orphaned_ids = [
            entry.id
            for entry in self._entry_by_id.values()
            if entry.kind == NotificationKind.AGENT_MESSAGE
            and entry.workspace_agent_id != ""
            and entry.workspace_agent_id not in known_workspace_agent_ids
        ]
        for entry_id in orphaned_ids:
            del self._entry_by_id[entry_id]

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
            logger.debug("notification {}: no dispatcher configured", entry.id)
            return
        preferences = self.get_dispatch_preferences()
        if not preferences.is_enabled:
            logger.debug("notification {}: master notifications toggle is off", entry.id)
            return
        if preferences.style not in _OS_DISPATCH_STYLES:
            logger.debug(
                "notification {}: delivery style is {!r} (cards-only), no OS nudge",
                entry.id,
                preferences.style,
            )
            return
        if entry.workspace_agent_id and entry.workspace_agent_id in self.get_connected_focused_workspace_agent_ids():
            # A focused connected window is already showing this workspace,
            # and the surface there (the in-chat card, the chat itself) covers
            # it: stay silent. Being displayed in an unfocused window
            # (alt-tabbed away, behind another app) does not count -- the
            # reader is not looking at that window, so the OS banner is the
            # only nudge they will see. An account-level entry has no
            # workspace to be on screen, so it always fires.
            logger.debug(
                "notification {}: {} is already on screen in a focused window -- the surface there covers it",
                entry.id,
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
            current = self._entry_by_id.get(entry.id)
            if current is None or current.is_resolved:
                logger.debug(
                    "notification {}: resolved or evicted before dispatch -- staying silent",
                    entry.id,
                )
                return
            if entry.kind == NotificationKind.PERMISSION_REQUEST:
                if entry.request_id in self._dispatched_request_ids:
                    logger.debug("notification {}: already dispatched once this process", entry.id)
                    return
                self._dispatched_request_ids.add(entry.request_id)
        request = build_dispatch_request(entry)
        logger.info(
            "notification {}: dispatching an OS banner for {} ({})",
            entry.id,
            entry.workspace_name,
            entry.title,
        )
        self.notification_dispatcher.dispatch(request)


def build_dispatch_request(entry: UiNotificationEntry) -> NotificationRequest:
    """The OS banner for a feed entry: workspace as the title, headline as the subtitle, detail as the body.

    The click lands where the feed row's click lands: the review popup for a
    request, the chat for an agent message, the backups surface for a system
    event. No workspace agent id means nowhere sensible to land for a
    workspace-scoped entry, so no url in that case.
    """
    match entry.kind:
        case NotificationKind.PERMISSION_REQUEST:
            url = (
                f"/workspace/{entry.workspace_agent_id}?review={entry.request_id}"
                if entry.workspace_agent_id
                else None
            )
            body = entry.body or _FALLBACK_DISPATCH_MESSAGE
        case NotificationKind.AGENT_MESSAGE:
            url = (
                f"/workspace/{entry.workspace_agent_id}?chat={entry.chat_agent_id}"
                if entry.workspace_agent_id
                else None
            )
            body = entry.body
        case NotificationKind.SYSTEM_EVENT:
            url = f"/workspace/{entry.workspace_agent_id}/backups" if entry.workspace_agent_id else "/accounts"
            body = entry.body
        case _ as unreachable:
            assert_never(unreachable)
    return NotificationRequest(title=entry.workspace_name, subtitle=entry.title, body=body, url=url, entry=entry)


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
