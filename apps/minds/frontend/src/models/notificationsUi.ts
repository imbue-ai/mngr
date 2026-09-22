// Arrival behavior for the notification feed. The NotificationsStore stays a
// dumb wire mirror; everything an ARRIVING entry does beyond landing in the
// feed lives here: the flash decision (in-app toast), the live-toast set the
// ToastLayer renders, the transient non-feed toasts, the dock-badge relay,
// the per-kind open gesture, the clear/read actions, and the notification
// preferences that gate it all.
//
// Reconnect IS resync: every (re)connect replays the feed as a snapshot frame
// (``is_snapshot``), so newness is judged by diffing entry ids against the
// previously seen set -- a replay re-seeds that set silently and never flashes.
//
// OS delivery is the backend's alone (a native banner from the Electron main
// process); nothing here talks to the Web Notifications API.

import m from "mithril";
import type {
  UiNotificationEntry,
  UiNotificationsMessage,
} from "../channel/messages";
import { electronBridge } from "../electron-bridge";
import type { NotificationsStore } from "./notifications";

export type NotificationStyle = "cards" | "os" | "both";

/** Hand-written mirror of the pydantic prefs model served inside the
 * /ui/api/settings overview (the generated schema covers only channel
 * frames). ``version`` is the If-Match token for the prefs write. */
export interface NotificationPrefs {
  is_enabled: boolean;
  style: NotificationStyle;
  version: string;
  /** Absent on older backends; only an explicit false offers the first-use choice. */
  has_chosen?: boolean;
}

/** What gating assumes until real prefs load (and whenever the backend does
 * not serve the field yet): notifications on, delivered both ways. */
export const DEFAULT_NOTIFICATION_PREFS: NotificationPrefs = {
  is_enabled: true,
  style: "both",
  version: "",
};

// The one applied-prefs cell for this window, shared by the arrival
// controller and the settings panel (which pushes every load/write result
// through applyNotificationPrefs). A module-level cell like webLogin/help
// rather than per-consumer copies, so a prefs change in the settings modal
// gates the very next arrival without any re-wiring.
let appliedPrefs: NotificationPrefs = DEFAULT_NOTIFICATION_PREFS;
// Bumped on every application so an in-flight loadPrefs can tell whether a
// newer write landed while its response was on the wire (and discard itself).
let appliedPrefsGeneration = 0;

export function currentNotificationPrefs(): NotificationPrefs {
  return appliedPrefs;
}

/** Apply prefs from a settings load/write; tolerates an absent field (the
 * backend may not serve it yet), keeping whatever applied last. */
export function applyNotificationPrefs(
  prefs: NotificationPrefs | null | undefined,
): void {
  if (prefs === null || prefs === undefined) return;
  appliedPrefs = prefs;
  appliedPrefsGeneration += 1;
}

// Test-only: reset the module-level prefs cell between vitest cases.
export function resetNotificationPrefsForTests(): void {
  appliedPrefs = DEFAULT_NOTIFICATION_PREFS;
  appliedPrefsGeneration += 1;
}

/** What the open gestures need to know about the world before they move.
 * Wired once by index.ts (where the shell and stores exist); the models
 * layer deliberately holds no ShellState reference of its own. */
export interface NotificationGestureContext {
  /** Translate either workspace coordinate to the stable agent id. */
  toAgentScopedId(anyId: string): string;
  /** The entry's create_attempt_state ("" = live and enterable), or null
   * when the workspace list does not know the id at all. */
  createAttemptStateOf(agentScopedId: string): string | null;
  /** Agent-scoped id of the displayed workspace, or null on hub pages. */
  displayedWorkspaceAgentId(): string | null;
  /** Open the review popup over the CURRENT surface (the shell forwards the
   * displayed workspace so it stays mounted behind the popup). */
  openInPlace(requestId: string): void;
  currentRoutePath(): string;
  /** Ask the displayed workspace's frame to show one of its chats. */
  focusChat(workspaceAgentId: string, chatAgentId: string): void;
}

/** Where a notification's click lands, for every kind. Constructed with the
 * shell's view of the world by index.ts; constructed bare (no context) by a
 * surface rendered before that wiring ran, which falls back to navigation
 * alone. */
export class NotificationGestures {
  private readonly context: NotificationGestureContext | null;

  constructor(context: NotificationGestureContext | null = null) {
    this.context = context;
  }

  /** The mounted-workspace coordinate for an entry's workspace id: the stable
   * agent id when the wiring can translate it, else the id as it came. */
  agentScopedIdOf(workspaceAgentId: string): string {
    return this.context?.toAgentScopedId(workspaceAgentId) ?? workspaceAgentId;
  }

  /**
   * The uniform review gesture: every out-of-context entry point to a request
   * (an in-app toast, a feed row, an OS-notification deep link) navigates to
   * the asking workspace with ``?review=<request-id>``, which
   * ShellState.handleRouteChanged consumes exactly once -- stripping the param
   * and opening the review popup if the request is still pending.
   */
  openReview(workspaceAgentId: string, requestId: string): void {
    if (requestId === "") return;
    const context = this.context;
    if (context === null) {
      // Unwired (tests, or a surface rendered before index.ts ran): the only
      // safe move without workspace knowledge is the legacy navigate-first
      // gesture.
      if (workspaceAgentId === "") {
        m.route.set("/inbox", { selected: requestId });
        return;
      }
      m.route.set(`/workspace/${workspaceAgentId}`, { review: requestId });
      return;
    }
    if (workspaceAgentId === "") {
      // Snapshotted before its workspace resolved: nothing to hop to, so the
      // popup opens over whatever is on screen.
      context.openInPlace(requestId);
      return;
    }
    const agentScoped = context.toAgentScopedId(workspaceAgentId);
    const createState = context.createAttemptStateOf(agentScoped);
    if (createState === null) {
      // The workspace list does not know this machine (yet): navigating to
      // /workspace/<id> would render the Home-looking fallback page, so stay
      // put and open the popup over the current surface instead.
      context.openInPlace(requestId);
      return;
    }
    if (createState !== "") {
      // The machine is still setting up: its own creating page is the landing
      // (never the Home-looking workspace fallback). From anywhere else, hop
      // there -- the ask stays in the bell. Already watching it set up, the
      // click means "let me answer": open the popup in place.
      if (context.currentRoutePath() === `/creating/${agentScoped}`) {
        context.openInPlace(requestId);
        return;
      }
      m.route.set(`/creating/${agentScoped}`);
      return;
    }
    if (context.displayedWorkspaceAgentId() === agentScoped) {
      // Already looking at the asking workspace: no route churn, just the
      // popup over it.
      context.openInPlace(requestId);
      return;
    }
    m.route.set(`/workspace/${agentScoped}`, { review: requestId });
  }

  /** The chat gesture: land in the message's workspace and ask it to show the
   * chat. Already looking at that workspace, the ask goes straight to its
   * frame; from anywhere else the ``?chat=`` param rides the navigation and
   * ShellState.handleRouteChanged forwards it once the frame is up. */
  openChat(workspaceAgentId: string, chatAgentId: string): void {
    if (workspaceAgentId === "") return;
    const context = this.context;
    if (context !== null) {
      const agentScoped = context.toAgentScopedId(workspaceAgentId);
      if (context.displayedWorkspaceAgentId() === agentScoped) {
        context.focusChat(agentScoped, chatAgentId);
        return;
      }
      m.route.set(`/workspace/${agentScoped}`, { chat: chatAgentId });
      return;
    }
    m.route.set(`/workspace/${workspaceAgentId}`, { chat: chatAgentId });
  }

  /** The system-event gesture: the workspace's backups page, or the accounts
   * page (where the backup quota lives) for an account-level event. */
  openSystemEvent(workspaceAgentId: string): void {
    if (workspaceAgentId === "") {
      m.route.set("/accounts");
      return;
    }
    m.route.set(`/workspace/${this.agentScopedIdOf(workspaceAgentId)}/backups`);
  }
}

export interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

/** The one builder of the If-Match-guarded notification-prefs write, shared
 * by every writer so the endpoint and its version contract cannot drift
 * apart. Response handling stays with the caller -- each surface rebases
 * and surfaces failures its own way. */
export function postNotificationPrefsWrite(
  fetchImpl: FetchLike,
  ifMatchVersion: string,
  next: {
    is_enabled: boolean;
    style: NotificationStyle;
  },
): Promise<Response> {
  return fetchImpl("/ui/api/settings/notifications", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "If-Match": ifMatchVersion,
    },
    body: JSON.stringify(next),
  });
}

/** A toast that is not a feed entry: a one-off message from the shell (the
 * "couldn't open link" fallback). It flashes and retires; nothing records it. */
export interface TransientToast {
  id: string;
  title: string;
  body: string;
}

/** What the ToastLayer stacks: a feed entry's flash, or a transient message. */
export type ToastItem =
  | { id: string; entry: UiNotificationEntry }
  | { id: string; entry: null; title: string; body: string };

export interface NotificationsUiHooks {
  onEntryOpened?: () => void;
  /** Whether the /notifications feed overlay is the current route. */
  isFeedOverlayOpen: () => boolean;
  /** Injected in tests; defaults to electronBridge.sendShellEvent. */
  relayShellEvent?: (event: { type: string } & Record<string, unknown>) => void;
  /** Injected in tests; defaults to the global fetch. */
  fetchImpl?: FetchLike;
  /** Injected in tests; defaults to m.redraw. */
  redraw?: () => void;
  /** Where clicks land; unwired surfaces navigate without shell knowledge. */
  gestures?: NotificationGestures;
}

export class NotificationsUiController {
  private hasReceivedNotification = false;
  isSavingChoice = false;
  choiceError = "";

  get shouldChooseNotificationStyle(): boolean {
    return (
      this.hasReceivedNotification &&
      currentNotificationPrefs().has_chosen === false
    );
  }

  /** Null chooses the bell alone; every answer is saved through the settings API. */
  async chooseNotificationStyle(
    style: NotificationStyle | null,
  ): Promise<void> {
    if (this.isSavingChoice) return;
    this.isSavingChoice = true;
    this.choiceError = "";
    this.redraw();
    const current = currentNotificationPrefs();
    const next = { is_enabled: style !== null, style: style ?? current.style };
    try {
      const response = await postNotificationPrefsWrite(
        this.fetchImpl(),
        current.version,
        next,
      );
      if (response.status === 412) {
        await this.loadPrefs();
        if (this.shouldChooseNotificationStyle)
          this.choiceError =
            "Settings changed in another window. Please choose again.";
      } else if (response.ok) {
        const result = (await response.json()) as { version: string };
        applyNotificationPrefs({
          ...next,
          has_chosen: true,
          version: result.version,
        });
      } else {
        this.choiceError = "Could not save your choice. Please try again.";
      }
    } catch {
      this.choiceError = "Could not save your choice. Please try again.";
    } finally {
      this.isSavingChoice = false;
      this.redraw();
    }
  }

  /** Entry ids currently flashing as toasts, newest first. A transient view of
   * the feed: retiring one never touches the underlying entry. */
  liveToastIds: readonly string[] = [];
  /** Non-feed toasts currently flashing, newest first. */
  transientToasts: readonly TransientToast[] = [];

  private readonly hooks: NotificationsUiHooks;
  private readonly gestures: NotificationGestures;
  /** Ids of every entry the previous frame carried; null until seeded. */
  private seenEntryIds: Set<string> | null = null;
  private lastRelayedCount: number | null = null;
  private transientToastSequence = 0;

  constructor(hooks: NotificationsUiHooks) {
    this.hooks = hooks;
    this.gestures = hooks.gestures ?? new NotificationGestures();
  }

  private redraw(): void {
    (this.hooks.redraw ?? m.redraw)();
  }

  private fetchImpl(): FetchLike {
    return this.hooks.fetchImpl ?? ((url, init) => fetch(url, init));
  }

  private relayShellEvent(
    event: { type: string } & Record<string, unknown>,
  ): void {
    (
      this.hooks.relayShellEvent ??
      ((relayed) => electronBridge.sendShellEvent(relayed))
    )(event);
  }

  /** Seed arrival state from the bootstrap snapshot: everything already in
   * the feed is "seen" (no flashes for old news) and the dock badge is told
   * the starting count. */
  seedFromSnapshot(
    store: Pick<NotificationsStore, "entries" | "unresolvedCount">,
  ): void {
    this.seenEntryIds = new Set(store.entries.map((entry) => entry.id));
    this.hasReceivedNotification ||= store.entries.some(
      (entry) => !entry.is_resolved,
    );
    this.relayBadgeCount(store.unresolvedCount);
  }

  /** The channel's per-frame hook (wired in index.ts as onNotificationsChanged). */
  handleNotificationsMessage(message: UiNotificationsMessage): void {
    const isFirstNotification =
      !this.hasReceivedNotification &&
      message.entries.some((entry) => !entry.is_resolved);
    if (isFirstNotification) {
      this.hasReceivedNotification = true;
      void this.loadPrefs();
    }
    const previouslySeen = this.seenEntryIds;
    const currentIds = new Set(message.entries.map((entry) => entry.id));
    this.seenEntryIds = currentIds;
    // A toast whose entry left the feed entirely has nothing to render (or
    // open); one whose entry merely resolved stays up -- its click then
    // navigates without opening the popup, and its timer retires it anyway.
    this.liveToastIds = this.liveToastIds.filter((id) => currentIds.has(id));
    this.relayBadgeCount(message.unresolved_count);
    // A snapshot frame (connect-time replay) restates the world: seed the
    // seen set silently. Same for a first frame with nothing to diff against.
    if (message.is_snapshot === true || previouslySeen === null) return;
    const fresh = message.entries.filter(
      (entry) => !entry.is_resolved && !previouslySeen.has(entry.id),
    );
    if (fresh.length === 0) return;
    const prefs = currentNotificationPrefs();
    if (!prefs.is_enabled || prefs.style === "os") return;
    // Cards flash in every open window, focused or not, and for the
    // workspace already on screen too: the toast is its own nudge, and a
    // window the reader is not looking at right now still shows it when
    // they come back (or the bell does, once its timer has run). Never
    // while the feed overlay is open -- the arrival lands there in plain
    // sight, and a queued flash would ambush the reader when it closes.
    if (this.hooks.isFeedOverlayOpen()) return;
    this.liveToastIds = [
      ...fresh.map((entry) => entry.id),
      ...this.liveToastIds,
    ];
  }

  /** Retire one flash (its corner X or the auto-dismiss timer). */
  dismissToast(toastId: string): void {
    if (this.liveToastIds.includes(toastId)) {
      this.liveToastIds = this.liveToastIds.filter((id) => id !== toastId);
      this.redraw();
      return;
    }
    if (this.transientToasts.some((toast) => toast.id === toastId)) {
      this.transientToasts = this.transientToasts.filter(
        (toast) => toast.id !== toastId,
      );
      this.redraw();
    }
  }

  /** Opening the feed overlay acknowledges the flashes: retire them all. */
  clearLiveToasts(): void {
    if (this.liveToastIds.length === 0) return;
    this.liveToastIds = [];
    this.redraw();
  }

  /** Flash a one-off message that is not a feed entry. */
  showTransientToast(title: string, body: string): void {
    this.transientToastSequence += 1;
    this.transientToasts = [
      { id: `transient-${this.transientToastSequence}`, title, body },
      ...this.transientToasts,
    ];
    this.redraw();
  }

  /** The live toasts as stack items, newest first (the ToastLayer's list):
   * transient messages in front, then the flashing feed entries. */
  liveToastItems(entries: readonly UiNotificationEntry[]): ToastItem[] {
    const byId = new Map(entries.map((entry) => [entry.id, entry]));
    const items: ToastItem[] = this.transientToasts.map((toast) => ({
      id: toast.id,
      entry: null,
      title: toast.title,
      body: toast.body,
    }));
    for (const id of this.liveToastIds) {
      const entry = byId.get(id);
      if (entry !== undefined) items.push({ id, entry });
    }
    return items;
  }

  /** Pick the desktop window, then run the same entry action used by native banners. */
  openEntry(entry: UiNotificationEntry): void {
    const workspaceId = this.gestures.agentScopedIdOf(entry.workspace_agent_id);
    if (workspaceId !== "") {
      const base = `/workspace/${encodeURIComponent(workspaceId)}`;
      const route =
        entry.kind === "permission_request"
          ? `${base}?review=${encodeURIComponent(entry.request_id)}`
          : entry.kind === "agent_message"
            ? `${base}?chat=${encodeURIComponent(entry.chat_agent_id ?? "")}`
            : `${base}/backups`;
      const routed = electronBridge.openNotificationInExistingWindow(
        route,
        entry,
      );
      if (routed !== null) {
        void routed.then(
          (handled) => {
            if (handled) {
              this.dismissToast(entry.id);
              this.hooks.onEntryOpened?.();
              this.redraw();
            } else {
              this.openEntryHere(entry);
            }
          },
          () => this.openEntryHere(entry),
        );
        return;
      }
    }
    this.openEntryHere(entry);
  }

  /** Shared by native banners, toast cards and feed rows in the selected window.
   * Clearing the reminder does not grant, deny, or otherwise resolve a request. */
  openEntryHere(entry: UiNotificationEntry): void {
    this.clearEntry(entry.id);
    this.hooks.onEntryOpened?.();
    switch (entry.kind) {
      case "permission_request":
        this.gestures.openReview(entry.workspace_agent_id, entry.request_id);
        return;
      case "agent_message":
        this.gestures.openChat(
          entry.workspace_agent_id,
          entry.chat_agent_id ?? "",
        );
        return;
      case "system_event":
        this.gestures.openSystemEvent(entry.workspace_agent_id);
        return;
    }
  }

  /** The row's clear button: the entry leaves the feed (and its flash). */
  clearEntry(entryId: string): void {
    this.dismissToast(entryId);
    this.postFeedAction(`/ui/api/notifications/${entryId}/clear`);
  }

  /** The feed's clear-all: everything goes, receipts included. */
  clearAll(): void {
    this.clearLiveToasts();
    this.postFeedAction("/ui/api/notifications/clear-all");
  }

  /** The route landed on a workspace: its agent messages are read. */
  handleWorkspaceDisplayed(workspaceAgentId: string): void {
    this.postFeedAction(
      `/ui/api/notifications/workspace/${encodeURIComponent(workspaceAgentId)}/read`,
    );
  }

  private postFeedAction(path: string): void {
    // Best-effort: the feed frame that follows is the confirmation, and a
    // failed write leaves the entry where it was for the next click. Logged
    // either way -- an entry that comes straight back looks identical to one
    // the backend refused (no session, no feed configured).
    void this.fetchImpl()(path, {
      method: "POST",
      credentials: "same-origin",
    }).then(
      (response) => {
        if (!response.ok)
          console.warn(
            `The notification feed action ${path} answered ${response.status}`,
          );
      },
      (error) =>
        console.warn(`The notification feed action ${path} failed`, error),
    );
  }

  /** Guard so the focus-gain refresh cannot pile a second fetch onto the
   * boot load (or a rapid refocus): concurrent calls share one load. */
  private prefsLoadInFlight: Promise<void> | null = null;

  /** Load prefs (at boot, and again at focus-gain) so gating uses the
   * persisted choice rather than the defaults for the whole session.
   * Best-effort: the defaults (enabled + both) stand until a load succeeds. */
  loadPrefs(): Promise<void> {
    if (this.prefsLoadInFlight === null) {
      this.prefsLoadInFlight = this.loadPrefsOnce().finally(() => {
        this.prefsLoadInFlight = null;
      });
    }
    return this.prefsLoadInFlight;
  }

  private async loadPrefsOnce(): Promise<void> {
    const generationAtStart = appliedPrefsGeneration;
    try {
      const response = await this.fetchImpl()("/ui/api/settings", {
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const data = (await response.json()) as {
        notification_prefs?: NotificationPrefs;
      };
      // A write that landed while this load was on the wire is newer than
      // the response; applying the response anyway would silently revert it
      // (e.g. re-enable flashes the user just disabled).
      if (appliedPrefsGeneration !== generationAtStart) return;
      applyNotificationPrefs(data.notification_prefs);
      this.redraw();
    } catch {
      // Network failure: defaults stand until a later load (the settings
      // modal's own) pushes real prefs through applyNotificationPrefs.
    }
  }

  private relayBadgeCount(count: number): void {
    if (count === this.lastRelayedCount) return;
    this.lastRelayedCount = count;
    // Electron main coerces and applies this via app.setBadgeCount; the
    // bridge no-ops in plain-browser mode.
    this.relayShellEvent({ type: "notifications_count", count });
  }
}
