// Arrival behavior for the notification feed. The NotificationsStore stays a
// dumb wire mirror; everything an ARRIVING entry does beyond landing in the
// feed lives here: the flash decision (in-app toast / Web Notification), the
// live-toast set the ToastLayer renders, the dock-badge relay, the one-time
// OS-permission hint, and the notification preferences that gate it all.
//
// Reconnect IS resync: every (re)connect replays the feed as a snapshot frame
// (``is_snapshot``), so newness is judged by diffing entry ids against the
// previously seen set -- a replay re-seeds that set silently and never flashes.

import m from "mithril";
import type {
  UiNotificationEntry,
  UiNotificationsMessage,
} from "../channel/messages";
import { electronBridge } from "../electron-bridge";
import { resolveWindowFocus } from "../window-focus";
import type { NotificationsStore } from "./notifications";

export type NotificationStyle = "cards" | "os" | "both";

/** Hand-written mirror of the pydantic prefs model served inside the
 * /ui/api/settings overview (the generated schema covers only channel
 * frames). ``version`` is the If-Match token for the prefs write. */
export interface NotificationPrefs {
  is_enabled: boolean;
  style: NotificationStyle;
  is_os_hint_dismissed: boolean;
  /** Desktop only: whether native OS notification permission was ever
   * confirmed granted (see maybeProbeDesktopNotificationPermission). Always
   * false in plain-browser mode, which has its own Web Notification
   * permission model instead. */
  os_permission_confirmed: boolean;
  version: string;
}

/** What gating assumes until real prefs load (and whenever the backend does
 * not serve the field yet): notifications on, delivered both ways. */
export const DEFAULT_NOTIFICATION_PREFS: NotificationPrefs = {
  is_enabled: true,
  style: "both",
  is_os_hint_dismissed: false,
  os_permission_confirmed: false,
  version: "",
};

// The one applied-prefs cell for this window, shared by the arrival
// controller, the titlebar hint, and the settings panel (which pushes every
// load/write result through applyNotificationPrefs). A module-level cell like
// webLogin/help rather than per-consumer copies, so a prefs change in the
// settings modal gates the very next arrival without any re-wiring.
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

/**
 * The uniform review gesture: every out-of-context entry point to a request
 * (an in-app toast, a feed row, a Web Notification click, an OS-notification
 * deep link) navigates to the asking workspace with ``?review=<request-id>``,
 * which ShellState.handleRouteChanged consumes exactly once -- stripping the
 * param and opening the review popup if the request is still pending.
 */
/** What the review gesture needs to know about the world before it moves.
 * Wired once by index.ts (where the shell and stores exist); the models
 * layer deliberately holds no ShellState reference of its own. */
export interface ReviewGestureContext {
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
}

let reviewGestureContext: ReviewGestureContext | null = null;

export function setReviewGestureContext(context: ReviewGestureContext): void {
  reviewGestureContext = context;
}

// Test-only: drop the module-level gesture context between vitest cases.
export function resetReviewGestureContextForTests(): void {
  reviewGestureContext = null;
}

export function openReviewRoute(
  workspaceAgentId: string,
  requestId: string,
): void {
  if (requestId === "") return;
  const context = reviewGestureContext;
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

/** Ask the browser for Web Notification permission when a style that reaches
 * the OS is chosen (a user gesture, as the permission prompt requires).
 * Desktop builds deliver OS notifications from the backend, so only
 * plain-browser mode ever needs the browser's own permission. */
export function maybeRequestOsPermissionForStyle(
  style: NotificationStyle,
  isDesktop: boolean = electronBridge.isDesktop,
): void {
  if (isDesktop) return;
  if (style === "cards") return;
  if (
    typeof Notification === "undefined" ||
    Notification.permission !== "default"
  )
    return;
  void Notification.requestPermission();
}

export interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

/** The one builder of the If-Match-guarded notification-prefs write, shared
 * by every writer (the settings panel, the hint dismissal) so the endpoint
 * and its version contract cannot drift apart. Response handling stays with
 * the caller -- each surface rebases and surfaces failures its own way. */
export function postNotificationPrefsWrite(
  fetchImpl: FetchLike,
  ifMatchVersion: string,
  next: {
    is_enabled: boolean;
    style: NotificationStyle;
    is_os_hint_dismissed: boolean;
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

/** Persist the desktop app's own observation of whether native OS
 * notification permission is granted. Unguarded (no If-Match): this is
 * system-observed state the app derives for itself, not a user-typed
 * preference, so there is nothing for a stale window to clobber. */
function postNotificationOsPermissionWrite(
  fetchImpl: FetchLike,
  confirmed: boolean,
): Promise<Response> {
  return fetchImpl("/ui/api/settings/notification-os-permission", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ os_permission_confirmed: confirmed }),
  });
}

/** Desktop analog of maybeRequestOsPermissionForStyle: ask the OS for native
 * notification permission by actually attempting to show one (Electron
 * exposes no separate "request" call on macOS -- posting the first
 * notification IS the request, and the system's own permission dialog
 * appears before the banner does). Always re-verifies, even if a prior probe
 * already confirmed it granted: "confirmed" is only ever as fresh as the last
 * probe, and the reader can revoke it in System Settings at any time with the
 * app none the wiser, so a stale "yes" would silently strand the OS delivery
 * style with no way back except this call site. On denial (the probe times
 * out unseen -- Electron cannot tell "declined" from "not decided yet"),
 * falls the delivery style back to "cards" so OS delivery is never silently
 * retried forever with nothing to show for it.
 *
 * Best-effort throughout: any write failure here just means the app asks
 * again next time (the next boot, or the next settings save), never a
 * broken UI state.
 *
 * Returns whether the OS confirmed the probe granted -- false for every
 * skipped case too (not desktop, not enabled, cards-only style), since none
 * of those actually confirm anything either. */
export async function maybeProbeDesktopNotificationPermission(
  isDesktop: boolean = electronBridge.isDesktop,
  probe: () => Promise<boolean> = () =>
    electronBridge.probeNotificationPermission(),
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): Promise<boolean> {
  if (!isDesktop) return false;
  const prefs = currentNotificationPrefs();
  if (!prefs.is_enabled || prefs.style === "cards") return false;
  let granted: boolean;
  try {
    granted = await probe();
  } catch {
    // Best-effort: an IPC failure reads the same as a declined/undecided
    // probe -- there is nothing else to confirm it granted.
    granted = false;
  }
  try {
    await postNotificationOsPermissionWrite(fetchImpl, granted);
  } catch {
    // Best-effort: a failed write just means this probes again next time.
  }
  applyNotificationPrefs({ ...currentNotificationPrefs(), os_permission_confirmed: granted });
  if (granted) return true;
  const latest = currentNotificationPrefs();
  try {
    const response = await postNotificationPrefsWrite(fetchImpl, latest.version, {
      is_enabled: latest.is_enabled,
      style: "cards",
      is_os_hint_dismissed: latest.is_os_hint_dismissed,
    });
    if (!response.ok) return false;
    const result = (await response.json()) as { version: string };
    // Merge onto the CURRENT prefs, not the pre-await `latest` snapshot: a
    // concurrent writer to the shared appliedPrefs cell (a settings-panel
    // save, the OS hint's dismissal) may have already landed its own field
    // while this downgrade write was in flight, and spreading the stale
    // snapshot would clobber it (mirrors the settings.ts merge-onto-fresh
    // fix for the same shared-state race).
    applyNotificationPrefs({
      ...currentNotificationPrefs(),
      style: "cards",
      os_permission_confirmed: false,
      version: result.version,
    });
  } catch {
    // Best-effort: a failed downgrade write just leaves the style standing
    // until the next probe (this session's next settings save, or a later
    // boot once permission is actually decided).
  }
  return false;
}

export interface NotificationsUiHooks {
  /** Agent-scoped id of the workspace whose content surface is on screen
   * (kept mounted behind an app modal counts), or null on hub pages. */
  onScreenWorkspaceAgentId: () => string | null;
  /** Whether the /notifications feed overlay is the current route. */
  isFeedOverlayOpen: () => boolean;
  /** Injected in tests; defaults to document.hasFocus() (true when there is
   * no document, i.e. under node). */
  hasWindowFocus?: () => boolean;
  /** Injected in tests; defaults to electronBridge.isDesktop. */
  isDesktop?: () => boolean;
  /** Injected in tests; defaults to electronBridge.sendShellEvent. */
  relayShellEvent?: (event: { type: string } & Record<string, unknown>) => void;
  /** Injected in tests; defaults to the global fetch. */
  fetchImpl?: FetchLike;
  /** Injected in tests; defaults to m.redraw. */
  redraw?: () => void;
}

export class NotificationsUiController {
  /** Entry ids currently flashing as toasts, newest first. A transient view of
   * the feed: retiring one never touches the underlying entry. */
  liveToastIds: readonly string[] = [];

  private readonly hooks: NotificationsUiHooks;
  /** Ids of every entry the previous frame carried; null until seeded. */
  private seenEntryIds: Set<string> | null = null;
  private lastRelayedCount: number | null = null;
  /** Session-sticky local dismissal so a failed persistence write only means
   * the hint returns on the next launch, not on the next redraw. */
  private isOsHintDismissedLocally = false;
  /** Fresh, surfacing-worthy entry ids that arrived while this window was
   * NOT the OS-focused one -- cards deliberately skip them then (see
   * handleNotificationsMessage), but with only one window in the picture
   * (the common case) there is no other window for them to have shown in
   * either, so silently dropping them forever just reads as "the toast
   * never happened." Held here and flushed by handleWindowFocusGained (wired
   * to the window's own 'focus' event in index.ts) once this window IS the
   * focused one again, so the reader still gets the card -- just the moment
   * they come back rather than the moment it happened. */
  private pendingFocusFlashIds: string[] = [];

  constructor(hooks: NotificationsUiHooks) {
    this.hooks = hooks;
  }

  private redraw(): void {
    (this.hooks.redraw ?? m.redraw)();
  }

  private fetchImpl(): FetchLike {
    return this.hooks.fetchImpl ?? ((url, init) => fetch(url, init));
  }

  private isDesktop(): boolean {
    return this.hooks.isDesktop === undefined
      ? electronBridge.isDesktop
      : this.hooks.isDesktop();
  }

  private hasWindowFocus(): boolean {
    return resolveWindowFocus(this.hooks.hasWindowFocus);
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
    this.relayBadgeCount(store.unresolvedCount);
  }

  /** The channel's per-frame hook (wired in index.ts as onNotificationsChanged). */
  handleNotificationsMessage(message: UiNotificationsMessage): void {
    const previouslySeen = this.seenEntryIds;
    const currentIds = new Set(message.entries.map((entry) => entry.id));
    this.seenEntryIds = currentIds;
    // A toast whose entry left the feed entirely has nothing to render (or
    // review); one whose entry merely resolved stays up -- its click then
    // navigates without opening the popup, and its timer retires it anyway.
    this.liveToastIds = this.liveToastIds.filter((id) => currentIds.has(id));
    // A queued catch-up flash is different: an entry that resolved or vanished
    // before focus returned has nothing left worth surfacing (unlike a LIVE
    // toast, nobody has seen it yet, so there is no "it stays up as a receipt"
    // case to preserve) -- drop it rather than flash a stale ask on focus.
    const unresolvedIds = new Set(
      message.entries
        .filter((entry) => !entry.is_resolved)
        .map((entry) => entry.id),
    );
    this.pendingFocusFlashIds = this.pendingFocusFlashIds.filter((id) =>
      unresolvedIds.has(id),
    );
    this.relayBadgeCount(message.unresolved_count);
    // A snapshot frame (connect-time replay) restates the world: seed the
    // seen set silently. Same for a first frame with nothing to diff against.
    if (message.is_snapshot === true || previouslySeen === null) return;
    const fresh = message.entries.filter(
      (entry) => !entry.is_resolved && !previouslySeen.has(entry.id),
    );
    if (fresh.length === 0) return;
    const prefs = currentNotificationPrefs();
    if (!prefs.is_enabled) return;
    const isOverlayOpen = this.hooks.isFeedOverlayOpen();
    const hasFocus = this.hasWindowFocus();
    // In-app cards flash for every fresh arrival, including one for the
    // workspace already on screen: the in-chat card shows the same ask
    // inline, but the toast is still its own worthwhile nudge, unlike an OS
    // banner for something already visible in the window (see the OS branch
    // below, which stays scoped to off-screen asks for that reason). Cards
    // flash only in the OS-focused window (or every open window would pop
    // the same toast for one event), and never while the feed overlay is
    // open -- the arrival lands there in plain sight, and a queued flash
    // would ambush the reader when the overlay closes.
    if (prefs.style !== "os" && !isOverlayOpen) {
      if (hasFocus) {
        this.liveToastIds = [
          ...fresh.map((entry) => entry.id),
          ...this.liveToastIds,
        ];
      } else {
        // Not focused right now -- with just one window (the common case)
        // there is no OTHER window this could have flashed in either, so
        // queue it for handleWindowFocusGained rather than dropping it: the
        // reader still gets the card, just the moment they come back.
        for (const entry of fresh) {
          if (!this.pendingFocusFlashIds.includes(entry.id))
            this.pendingFocusFlashIds.push(entry.id);
        }
      }
    }
    // The OS channel dedupes on its own (the system shows one banner; the tag
    // collapses browser tabs). An open feed overlay makes the banner
    // redundant only when that feed is actually visible (this window
    // focused); an open feed left in a background tab still needs it.
    // Unlike the in-app toast above, this stays scoped to workspaces not
    // already on screen: the workspace already on screen surfaces its ask
    // inline (the in-chat card), so a redundant OS banner on top of that
    // would be actual noise, not just a second useful nudge. But "on screen"
    // is a route check, not a focus check -- only skip the banner when this
    // tab is also focused, or an alt-tabbed-away/background tab that merely
    // still shows the right route would wrongly stay silent (the same gap
    // fixed server-side for desktop in the OS-banners-require-focus commit).
    const onScreen = this.hooks.onScreenWorkspaceAgentId();
    const osEligible =
      onScreen === null || !hasFocus
        ? fresh
        : fresh.filter((entry) => entry.workspace_agent_id !== onScreen);
    if (prefs.style !== "cards" && !(isOverlayOpen && hasFocus))
      this.notifyOs(osEligible);
  }

  /** Flush any catch-up flashes queued while this window was unfocused.
   * Wired to the window's own 'focus' event (index.ts) alongside the prefs
   * refresh already there. A no-op with nothing queued (redraws for free,
   * so this is cheap to call unconditionally on every focus gain). */
  handleWindowFocusGained(): void {
    if (this.pendingFocusFlashIds.length === 0) return;
    // Still gated by the feed overlay (if the reader opened it while away,
    // the arrivals are sitting there in plain sight already) and the style
    // preference (which may have changed while this window was unfocused).
    if (this.hooks.isFeedOverlayOpen()) {
      this.pendingFocusFlashIds = [];
      return;
    }
    const prefs = currentNotificationPrefs();
    const queued = this.pendingFocusFlashIds;
    this.pendingFocusFlashIds = [];
    if (!prefs.is_enabled || prefs.style === "os") return;
    this.liveToastIds = [...queued, ...this.liveToastIds];
    this.redraw();
  }

  /** Retire one flash (its corner X or the auto-dismiss timer). */
  dismissToast(entryId: string): void {
    if (!this.liveToastIds.includes(entryId)) return;
    this.liveToastIds = this.liveToastIds.filter((id) => id !== entryId);
    this.redraw();
  }

  /** Opening the feed overlay acknowledges the flashes: retire them all. */
  clearLiveToasts(): void {
    if (this.liveToastIds.length === 0) return;
    this.liveToastIds = [];
    this.redraw();
  }

  /** The live toasts as feed entries, newest first (the ToastLayer's list). */
  liveToastEntries(
    entries: readonly UiNotificationEntry[],
  ): UiNotificationEntry[] {
    const byId = new Map(entries.map((entry) => [entry.id, entry]));
    const live: UiNotificationEntry[] = [];
    for (const id of this.liveToastIds) {
      const entry = byId.get(id);
      if (entry !== undefined) live.push(entry);
    }
    return live;
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

  /** Whether the one-time "Enable system notifications?" hint belongs by the
   * bell: browser mode only, an OS-reaching style chosen, permission not yet
   * decided, and never after it was dismissed. */
  shouldShowOsHint(): boolean {
    if (this.isDesktop()) return false;
    const prefs = currentNotificationPrefs();
    if (!prefs.is_enabled || prefs.style === "cards") return false;
    if (prefs.is_os_hint_dismissed || this.isOsHintDismissedLocally)
      return false;
    return (
      typeof Notification !== "undefined" &&
      Notification.permission === "default"
    );
  }

  /** The hint's affirmative click: ask the browser (a user gesture). */
  async requestOsPermissionFromHint(): Promise<void> {
    if (typeof Notification === "undefined") return;
    await Notification.requestPermission();
    this.redraw();
  }

  /** The hint's X: hide it now and persist the dismissal. Optimistic -- a
   * failed write only means the hint returns on the next launch. */
  async dismissOsHint(): Promise<void> {
    this.isOsHintDismissedLocally = true;
    this.redraw();
    const prefs = currentNotificationPrefs();
    const next = {
      is_enabled: prefs.is_enabled,
      style: prefs.style,
      is_os_hint_dismissed: true,
    };
    try {
      const response = await postNotificationPrefsWrite(
        this.fetchImpl(),
        prefs.version,
        next,
      );
      if (response.status === 412) {
        // Another window changed prefs first: rebase on the newer state (the
        // local session flag keeps the hint hidden either way).
        await this.loadPrefs();
        return;
      }
      if (!response.ok) return;
      const result = (await response.json()) as { version: string };
      // Merge onto the CURRENT prefs, not the pre-await `prefs` snapshot: a
      // concurrent writer to the shared appliedPrefs cell (a settings-panel
      // save, the desktop permission probe's own downgrade) may have already
      // landed its own field while this write was in flight, and spreading
      // the stale snapshot would clobber it (mirrors the settings.ts
      // merge-onto-fresh fix for the same shared-state race).
      const latest = currentNotificationPrefs();
      applyNotificationPrefs({ ...latest, ...next, version: result.version });
    } catch {
      // The session-local dismissal stands.
    }
  }

  private relayBadgeCount(count: number): void {
    if (count === this.lastRelayedCount) return;
    this.lastRelayedCount = count;
    // Electron main coerces and applies this via app.setBadgeCount; the
    // bridge no-ops in plain-browser mode.
    this.relayShellEvent({ type: "notifications_count", count });
  }

  /** Browser-mode OS delivery via the Web Notifications API. Desktop builds
   * are excluded: there the backend dispatches native OS notifications
   * itself, and a renderer copy would double them. */
  private notifyOs(entries: readonly UiNotificationEntry[]): void {
    if (this.isDesktop()) return;
    if (
      typeof Notification === "undefined" ||
      Notification.permission !== "granted"
    )
      return;
    for (const entry of entries) {
      try {
        // The tag makes the banner unique per entry ACROSS open tabs: every
        // tab fires one, and the browser collapses same-tag notifications.
        const notification = new Notification(
          `${entry.workspace_name} asks — ${entry.title}`,
          {
            body: entry.body,
            tag: entry.id,
          },
        );
        notification.onclick = () => {
          window.focus();
          openReviewRoute(entry.workspace_agent_id, entry.request_id);
          this.redraw();
        };
      } catch {
        // Some engines require a ServiceWorker registration to construct one;
        // the feed already recorded the entry, so nothing is lost.
      }
    }
  }
}
