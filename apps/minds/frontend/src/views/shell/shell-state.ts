// Cross-view shell state: which workspace is displayed, accent painting, and
// workspace-entry navigation. The mutable singleton the Shell + frame views
// share (mithril redraws pull from it; imperative document-level effects --
// CSS variables -- happen here, exactly as chrome.js did).

import m from "mithril";
import type { AppStores } from "../../models/boot";
import type { WorkspaceHealth } from "../../models/health";
import type { UiChannelClient } from "../../channel/client";
import type { RequestVerdict, ResolvedRequest } from "../../models/inbox";
import type { NotificationsUiController } from "../../models/notificationsUi";
import {
  accentSourceForRoute,
  isAppOverlayPath,
  isTitlebarPopupRoutePath,
  isWorkspaceOverlayPath,
  overlayBehindWorkspaceId,
  recoveryWorkspaceIdFromPath,
  workspaceDisplayIdFromPath,
  workspaceSurfaceIdFromPath,
} from "./classify";

/** Posts one permission-resolution message into the mounted workspace frame
 * over the embed contract. Registered by WorkspaceFrame, which owns the
 * contract endpoint; the shell only decides whether the message is due. */
export type PermissionResolvedSender = (
  requestId: string,
  verdict: RequestVerdict,
) => void;

/** The mounted Permissions pane's waiting list, as the shell addresses it. */
export interface WaitingRequestList {
  /** Drop an answered request from the list, at once. */
  forgetWaitingRequest(requestId: string): void;
  /** Whether the list still has anything on it (asked after an answer, so the
   * request just answered is already gone from it). */
  hasWaitingRequests(): boolean;
}

/** The mounted workspace content iframe, as the shell addresses it. */
export interface WorkspaceFrameHandle {
  /** The workspace the frame is navigated to, or null before it is first armed. */
  armedWorkspaceAnyId(): string | null;
  /** Re-navigate the frame to that workspace's root URL. */
  reload(): void;
}

export class ShellState {
  readonly stores: AppStores;
  channel: UiChannelClient | null = null;
  /** The notification arrival controller (toasts, badge relay, OS hint),
   * installed by index.ts like the channel; null only before boot wiring. */
  notificationsUi: NotificationsUiController | null = null;
  isMac = false;
  mngrForwardOrigin = "";
  /** The bell's notification feed: local overlay state, not a route, so it
   * pops over whatever surface is on screen (a hub page, the create form, a
   * machine) without navigating and without swapping what is painted behind
   * it -- a route-based app modal over a hub page would fall back to Home. */
  isNotificationsOpen = false;
  isSidebarOpen = false;
  sidebarAnchor: {
    x: number;
    y: number;
    width: number;
    height: number;
  } | null = null;
  /** The workspace whose CONTENT is displayed (null on hub pages). */
  displayedWorkspaceAnyId: string | null = null;
  /** The workspace-options route an app modal was opened over, so the Shell can
   * keep that panel painted (and mounted) beneath the modal: the live route is
   * the modal's, which carries none of the panel's params. Null whenever no
   * modal floats over the panel. */
  panelRouteBehindOverlay: string | null = null;
  /**
   * The hub PAGE an app-level modal was opened over, so the Shell keeps that
   * page painted (and mounted) beneath it. Null whenever no modal floats over
   * one.
   *
   * Only the machine-recovery page needs this, and needs it badly: it names a
   * machine in its own path, so the Get help modal it opens (which forwards
   * ?workspace= to report against the right machine) would otherwise put that
   * machine's surface behind the backdrop -- the surface the recovery page
   * exists precisely because nobody could load. The reader would watch the
   * card they were reading get replaced by the thing that would not open.
   */
  pageRouteBehindOverlay: string | null = null;
  /**
   * The one piece of recovery-card state: which machine's card is up, and
   * whether the shell raised it or the user did.
   *
   * Not derived from health. The card auto-raises on a single edge, and an edge
   * fires once, so there is nothing to re-derive and no dismissal to remember.
   * The isAutoRaised bit decides one thing: whether the card leaves on its own
   * when the machine comes back.
   */
  private openRecovery: { agentId: string; isAutoRaised: boolean } | null =
    null;
  /** The mounted content iframe, installed by WorkspaceFrame (null when none is
   * mounted: hub pages, recovery, destroying, the workspace sub-pages). The
   * frame is ALSO mounted behind an app modal that forwarded ?workspace=, which
   * is why the workspace it is showing is asked of it rather than read off
   * `displayedWorkspaceAnyId`. */
  workspaceFrame: WorkspaceFrameHandle | null = null;
  /** Re-entrancy guard for closeAppOverlay: its history.back() is not
   * idempotent, and a repeated Escape can reach it again before the first
   * back() has landed. Cleared on the next route change. */
  private isAppOverlayClosing = false;
  /** Raised by `switchToNotifications`: the feed is being opened as PART of a
   * navigation (the one putting the popup it is replacing away), so the
   * arrival must not close it the way an ordinary navigation does. Consumed
   * by the first route change that lands. */
  private isNotificationsArmed = false;
  /** The path the last `handleRouteChanged` saw, so a redraw on the route the
   * window is already on is not mistaken for a navigation to it. */
  private lastHandledRoutePath: string | null = null;
  /** The ``?review=`` deep link already consumed, keyed by route+id.
   * `handleRouteChanged` runs on EVERY redraw and its stripping route-set
   * lands a tick later, so without this every interim redraw would re-consume
   * the same param and re-open the popup. Cleared once a review-less route is
   * seen (i.e. the strip landed), so a later deep link consumes afresh. */
  private consumedReviewKey: string | null = null;
  /** The last ``selected`` request id `openInbox` pushed a NEW /inbox entry
   * for, and when. `openInbox` already replaces in place once the route IS
   * `/inbox` -- this guards the window before that lands: `currentRoutePath`
   * reads Mithril's resolved route, which can still report the PRE-popup path
   * for a beat after `m.route.set` fires, so two callers racing to open the
   * same request (a toast and a feed row, an embed-contract message and a
   * notification click, or any other double-fire) can both see "not on
   * /inbox yet" and both push -- stacking a second, stale popup that a single
   * dismissal then leaves behind. A short in-memory dedup, independent of the
   * route read, closes that window regardless of where the duplicate call
   * came from. */
  private lastOpenedInboxSelectedId: string | null = null;
  private lastOpenedInboxAtMs = 0;

  private permissionResolvedSender: PermissionResolvedSender | null = null;
  /** The mounted Permissions pane's live list, registered while one is up. The
   * popup answers a request; the pane behind it is what shows the list that
   * request was in. */
  private waitingRequestList: WaitingRequestList | null = null;

  constructor(stores: AppStores) {
    this.stores = stores;
  }

  /** Rebuild this window's workspace view, if its frame is showing the one named. */
  reloadWorkspaceFrame(agentScopedId: string): void {
    const frame = this.workspaceFrame;
    if (frame === null) return;
    const armed = frame.armedWorkspaceAnyId();
    if (armed === null) return;
    if (this.stores.workspaces.toAgentScopedId(armed) !== agentScopedId) return;
    frame.reload();
  }

  currentRoutePath(): string {
    const route = m.route.get() ?? "/";
    return route.split("?")[0];
  }

  currentRouteSearch(): string {
    const route = m.route.get() ?? "";
    return route.split("?")[1] ?? "";
  }

  /** Enter a workspace: route to the content surface for its identity.
   * `query` carries route params along (the ``?review=`` deep link an
   * OS-notification click arrives with); empty adds nothing. */
  enterWorkspace(anyId: string, query: Record<string, string> = {}): void {
    const agentScoped = this.stores.workspaces.toAgentScopedId(anyId);
    m.route.set(`/workspace/${agentScoped}`, query);
  }

  /** Open the request-review popup over the current surface: forward the
   * displayed workspace as ?workspace so it floats over that live workspace
   * (kept mounted) instead of navigating the base layer to Home, mirroring how
   * Get help forwards ?workspace. Opened from Home (no workspace displayed), it
   * carries none and floats over Home. Extra query params (`selected` for a
   * named request) are merged in.
   *
   * Opened from the workspace-options panel, the route it was opened from is
   * remembered so that panel stays mounted underneath rather than being torn
   * down and rebuilt around the popup. */
  openInbox(params: Record<string, string> = {}): void {
    const path = this.currentRoutePath();
    const selected = params.selected;
    if (selected !== undefined) {
      // Idempotent open (see lastOpenedInboxSelectedId's comment): a second
      // trigger for the SAME request within this window is a no-op rather
      // than a second push, however it got here.
      const isDuplicate =
        this.lastOpenedInboxSelectedId === selected &&
        Date.now() - this.lastOpenedInboxAtMs < 1500;
      if (isDuplicate) return;
      this.lastOpenedInboxSelectedId = selected;
      this.lastOpenedInboxAtMs = Date.now();
    }
    // Only ever set here (handleRouteChanged clears it), so a second request
    // arriving while the popup is already up does not drop the panel it floats
    // over -- the route is /inbox by then, which names no panel.
    if (isWorkspaceOverlayPath(path))
      this.panelRouteBehindOverlay = m.route.get() ?? null;
    const displayed = this.displayedWorkspaceAnyId;
    const query =
      displayed === null
        ? params
        : {
            ...params,
            workspace: this.stores.workspaces.toAgentScopedId(displayed),
          };
    // Swinging the OPEN popup onto another request replaces its history entry
    // rather than stacking a second one, so one dismissal still lands back on
    // the surface the popup was opened over instead of on the request before it.
    m.route.set(
      "/inbox",
      query,
      path === "/inbox" ? { replace: true } : undefined,
    );
  }

  /**
   * Remember the current route as the page an app-level modal is about to
   * float over, when it is one that must stay painted behind it.
   *
   * Called by the surface opening the modal, before it routes: the modal's own
   * route carries nothing of where it was opened from, and the surfaces that
   * need this are the ones an ordinary backdrop would replace with something
   * worse (see `pageRouteBehindOverlay`).
   */
  rememberPageBehindOverlay(): void {
    const route = m.route.get() ?? "";
    this.pageRouteBehindOverlay = recoveryWorkspaceIdFromPath(route.split("?")[0]) !== null ? route : null;
  }

  /** Register the mounted Permissions pane's list, and drop it on the way out
   * (guarded like the frame's sender: a pane torn down after its successor
   * registered must not clear the successor's). */
  registerWaitingRequestList(list: WaitingRequestList): void {
    this.waitingRequestList = list;
  }

  unregisterWaitingRequestList(list: WaitingRequestList): void {
    if (this.waitingRequestList === list) this.waitingRequestList = null;
  }

  /** Drop a request from the mounted pane's list. Answering one goes through
   * `notifyRequestResolved`; this is for the other way a request stops
   * waiting -- answered in another window, or withdrawn by the agent -- where
   * there is no verdict to relay but the row is just as gone. */
  forgetWaitingRequest(requestId: string): void {
    this.waitingRequestList?.forgetWaitingRequest(requestId);
  }

  /** Register the mounted workspace frame's contract sender. */
  registerPermissionResolvedSender(sender: PermissionResolvedSender): void {
    this.permissionResolvedSender = sender;
  }

  /** Drop `sender` if it is still the registered one (a frame torn down after
   * its successor registered must not clear the successor's). */
  unregisterPermissionResolvedSender(sender: PermissionResolvedSender): void {
    if (this.permissionResolvedSender === sender)
      this.permissionResolvedSender = null;
  }

  /** Tell the workspace that asked that its request now has a verdict, so its
   * in-chat card flips without waiting for the agent transcript to carry the
   * resolution back.
   *
   * Only the displayed workspace is told, and only when it is the one that
   * asked: the chrome mounts a single workspace frame, so no other workspace
   * has a live page in this window, and posting a request id into a workspace
   * that did not ask would hand it to foreign content for nothing. A verdict
   * given while looking at some other workspace is simply not relayed -- the
   * frame pushes the workspace's verdict snapshot (from the response event
   * log) whenever it next loads that page, so missing this send never
   * strands a card.
   *
   * Both sides of the comparison are WORKSPACE agent ids. The request's own
   * ``agent_id`` is not usable here: latchkey requests are filed by the
   * workspace's system-services sibling agent, so it never equals the id of
   * the tile on screen -- which is why the card carries the workspace it
   * belongs to, resolved server-side by name. */
  notifyRequestResolved(resolved: ResolvedRequest): void {
    // The pane behind this popup is showing the list the request was in, and
    // the answer was just given: a row that sits there until the next read
    // comes back reads as an answer that did not take. Unconditional, unlike
    // the relay below -- the list is this window's own, whichever machine the
    // request belongs to.
    this.forgetWaitingRequest(resolved.requestId);
    const sender = this.permissionResolvedSender;
    const displayed = this.displayedWorkspaceAnyId;
    if (sender === null || displayed === null || resolved.agentId === null)
      return;
    if (this.stores.workspaces.toAgentScopedId(displayed) !== resolved.agentId)
      return;
    sender(resolved.requestId, resolved.verdict);
  }

  /** Close the options overlay if one is open, returning whether it was.
   * `routeOptions` is forwarded to the route set: a strip switch passes
   * `{replace: true}` so the panel being left is not one Back away under the
   * surface replacing it; a plain dismissal (Escape, the X) pushes, leaving
   * the panel in history like any left page. */
  closeWorkspaceOverlay(routeOptions?: { replace: boolean }): boolean {
    const path = this.currentRoutePath();
    if (!isWorkspaceOverlayPath(path)) return false;
    const surfaceId = workspaceSurfaceIdFromPath(path);
    if (surfaceId === null) return false;
    m.route.set(`/workspace/${surfaceId}`, undefined, routeOptions);
    return true;
  }

  /** Dismiss an open app-level modal (the request popup, Minds settings,
   * Accounts, Get help), returning to the surface it was opened over, and
   * report whether there was one. Prefers history so the opener (Home, Create,
   * the workspace, or its options panel) is restored exactly; falls back to
   * routing to the base when there is no history (a cold-start deep link).
   *
   * The request popup (/inbox) is the one exception: routed there directly
   * (from the workspace already on screen), history.back()'s "undo exactly
   * one push" assumption holds. But a notification click for a DIFFERENT
   * workspace hops there first and THEN opens the popup over it -- two
   * pushes, not one -- and history.back() only undoes the popup, landing
   * back on the ORIGINAL screen rather than the workspace the popup was
   * actually reviewing (and, depending on exactly how those two pushes
   * landed, could leave the popup itself still on screen). /inbox always
   * names its workspace explicitly (?workspace=, forwarded by openInbox), so
   * dismissing it routes there directly instead of trusting history depth --
   * correct regardless of how many entries getting here actually pushed. */
  closeAppOverlay(): boolean {
    const path = this.currentRoutePath();
    const search = this.currentRouteSearch();
    // The fixed app modals are always closeable; the New machine template
    // stepper is a closeable modal only while it floats over a machine
    // (?workspace=) -- with none it is a redirect, not an overlay.
    const isCloseable =
      isAppOverlayPath(path) ||
      (path === "/create/template" &&
        overlayBehindWorkspaceId(path, search) !== null);
    if (!isCloseable) return false;
    // history.back() does not update the route synchronously, so a second
    // dismissal arriving before it lands (a repeated Escape) would fire
    // another back() and over-navigate past the opener. Reported as handled
    // even so: the key belongs to the overlay that is still on its way out.
    if (this.isAppOverlayClosing) return true;
    this.isAppOverlayClosing = true;
    if (path === "/inbox") {
      // Replace, not push: this collapses the popup's own entry into the
      // workspace view rather than adding a further forward step, so the
      // popup is not left sitting one Back away and the stack this
      // dismissal walks away from does not grow on every review.
      const behind = overlayBehindWorkspaceId(path, search);
      m.route.set(behind !== null ? `/workspace/${behind}` : "/", undefined, {
        replace: true,
      });
      return true;
    }
    if (window.history.length > 1) {
      window.history.back();
      return true;
    }
    // No history to go back through: land on the surface the modal was opened
    // over. The remembered page comes first -- it was opened over that page
    // BECAUSE the machine it names would not load, so the machine ?workspace=
    // forwards is the one place not to send anyone.
    const behindPage = this.pageRouteBehindOverlay;
    if (behindPage !== null) {
      m.route.set(behindPage);
      return true;
    }
    const behind = overlayBehindWorkspaceId(path, search);
    m.route.set(behind !== null ? `/workspace/${behind}` : "/");
    return true;
  }

  /**
   * Go back up to the Permissions panel this popup was opened from, reporting
   * whether there was one.
   *
   * The way OUT of the popup leaves the window (see `dismissAppOverlay`); this
   * is the way BACK, for a reader who opened a request from the panel and wants
   * the panel again. It restores the panel's own route, so the tab and section
   * it was left on come back with it.
   */
  returnToPanelBehindOverlay(): boolean {
    const panelRoute = this.panelRouteBehindOverlay;
    if (panelRoute === null) return false;
    // Left set: handleRouteChanged drops it on arrival, and until then the
    // panel underneath keeps rendering rather than blinking out.
    m.route.set(panelRoute);
    return true;
  }

  /**
   * Dismiss an app-level modal the way the person dismissing it means it,
   * reporting whether there was one.
   *
   * Differs from `closeAppOverlay` for one surface: the request popup opened
   * from a "Waiting on you" row. That popup takes the options panel's window
   * over -- it hangs from the same key and resizes out of the panel's box -- so
   * it reads as that window showing a request, not as a second card stacked on
   * one. Clicking away from it therefore leaves the window, rather than
   * uncovering a panel the reader has not thought of as still being there.
   *
   * Resolving the last request still goes through `closeAppOverlay`, which
   * returns to the panel: finishing the review is what the panel is FOR, and
   * landing back on it is how the new grant is seen.
   */
  dismissAppOverlay(): boolean {
    const path = this.currentRoutePath();
    if (path === "/inbox" && this.panelRouteBehindOverlay !== null) {
      const behind = overlayBehindWorkspaceId(path, this.currentRouteSearch());
      if (behind !== null) {
        // Same re-entrancy guard as closeAppOverlay: one dismissal can arrive
        // twice (the in-document Escape plus Electron's forward of it).
        if (this.isAppOverlayClosing) return true;
        this.isAppOverlayClosing = true;
        // Straight to the machine rather than back through history: the panel
        // is the history entry, and going back to it is the thing this avoids.
        // Replace, so Back from the machine does not re-raise the popup being
        // dismissed. handleRouteChanged forgets the remembered panel on the
        // way.
        m.route.set(`/workspace/${behind}`, undefined, { replace: true });
        return true;
      }
    }
    return this.closeAppOverlay();
  }

  /**
   * Hand the Permissions panel back once the request it was opened from has
   * been answered, reporting whether there was a panel to hand back. A request
   * opened from the chat has none, and its page simply closes.
   *
   * Back to the panel's own route while other requests are still waiting: the
   * list the reader picked from is still there, still theirs to work through,
   * and it is the section they left. Once nothing is waiting that list is gone,
   * so returning them to it would return them to nothing -- they land on Add
   * connection instead, the next thing anyone is in that pane to do.
   */
  returnToPanelAfterRequest(): boolean {
    const panelRoute = this.panelRouteBehindOverlay;
    if (panelRoute === null) return false;
    // Asked after the answer, and the pane drops an answered request the
    // moment it is answered, so "any left" is already the list without it.
    if (this.waitingRequestList?.hasWaitingRequests() === true)
      return this.returnToPanelBehindOverlay();
    const [path, query = ""] = panelRoute.split("?");
    const params = new URLSearchParams(query);
    params.set("tab", "permissions");
    params.set("section", "add-connection");
    m.route.set(`${path}?${params.toString()}`);
    return true;
  }

  /**
   * Dismiss the topmost dismissible surface, reporting whether there was one.
   *
   * The one place that knows what is stacked over what, so the precedence is a
   * plain ordered list rather than something the surfaces negotiate through
   * listener registration order (which follows mount order, not z-order).
   *
   * The switcher popover and the notification feed lead: they are the only
   * surfaces that can open over the recovery card. The card comes before the
   * two route-based overlays because
   * it is not one -- it can be raised over the workspace options overlay, and
   * it sits above it. It is never raised over an app-level modal, so this never
   * has to choose between those two. The two route-based closers gate on the
   * live route, so a request popup floating over the options panel closes
   * alone (the route is the popup's) and leaves the panel standing.
   */
  handleEscape(): boolean {
    if (this.isSidebarOpen) {
      this.closeSidebar();
      return true;
    }
    if (this.isNotificationsOpen) {
      this.closeNotifications();
      return true;
    }
    return (
      this.closeOpenRecoveryModal() ||
      this.closeWorkspaceOverlay() ||
      this.dismissAppOverlay()
    );
  }

  /** Route-change hook: track displayed workspace, repaint accent, register. */
  handleRouteChanged(path: string, search = ""): void {
    // The router runs this from its render, which is every redraw and not only
    // every navigation -- so "the route is no longer a modal's" has to mean the
    // route CHANGED to one, not merely that this draw is not on one.
    //
    // Opening the popup is exactly where that bites: `openInbox` remembers the
    // panel and then asks for the route, and `m.route.set` lands a tick later.
    // Mithril redraws as soon as the click handler returns, so a draw happens
    // with the panel's own route still current -- and taking that for a
    // navigation threw away the panel that had just been remembered, one
    // instruction after it was written.
    const isSameRoute = path === this.lastHandledRoutePath;
    this.lastHandledRoutePath = path;
    // The dismissal navigation has landed; clear the closeAppOverlay guard.
    // Gated on an actual navigation, like the panel below: history.back() does
    // not land synchronously, so a redraw on the route still being left would
    // otherwise drop the guard before the dismissal it guards -- and a held
    // Escape (repeating every ~30ms) would fire a second back(), carrying the
    // reader past the surface the popup was opened over.
    if (!isSameRoute) this.isAppOverlayClosing = false;
    // The feed is a popover over the surface it was opened on; leaving that
    // surface (a feed row's jump to a machine, the sidebar, anything) closes
    // it, like a dropdown would. A switch INTO it from another titlebar popup
    // is the exception: that navigation is how the popup being replaced goes
    // away, so the feed rides across it once.
    if (!isSameRoute) {
      if (this.isNotificationsArmed) this.isNotificationsArmed = false;
      else this.isNotificationsOpen = false;
    }
    // The dedup guard (see lastOpenedInboxSelectedId) only needs to survive
    // the race right at open time; once a real navigation lands away from
    // /inbox, the popup is confirmed gone and a later re-open of the SAME
    // request is a fresh, legitimate ask, not a duplicate.
    if (!isSameRoute && path !== "/inbox")
      this.lastOpenedInboxSelectedId = null;
    // The panel underneath belongs to the request popup that took its window
    // over, so it lives exactly as long as /inbox is the route: navigating to
    // ANY other route -- including another app modal's, like a Get help the
    // strip or an Electron open-overlay ask raised -- leaves the panel
    // behind. A modal route that kept it would paint the panel underneath
    // itself, backdrop and raised strip and all.
    if (!isSameRoute && path !== "/inbox") this.panelRouteBehindOverlay = null;
    // The page underneath belongs to the modal that was opened over it; once
    // the route is no longer a modal's, it is (or is not) the route.
    if (!isSameRoute && !isAppOverlayPath(path))
      this.pageRouteBehindOverlay = null;
    // Pass the query so an app modal opened over a workspace (/help?workspace=)
    // keeps that workspace's accent painting behind it.
    const accentSource = accentSourceForRoute(path, search);
    // The options overlay and the app modals both keep the workspace surface
    // mounted behind them, so either still counts as displaying that
    // workspace -- which is what addresses a verdict to the machine that asked
    // while its request popup is the current route.
    this.displayedWorkspaceAnyId =
      workspaceSurfaceIdFromPath(path) ??
      overlayBehindWorkspaceId(path, search);
    this.paintAccent(accentSource);
    const agentScoped =
      this.displayedWorkspaceAnyId === null
        ? null
        : this.stores.workspaces.toAgentScopedId(this.displayedWorkspaceAnyId);
    // A card belongs to one machine; arriving anywhere else does not carry it
    // along. Keyed on the card's own machine rather than on "the route
    // changed", so a card opened for a machine the window is still navigating
    // to survives its arrival.
    //
    // "Anywhere else" means the machine is off screen entirely, not merely
    // covered: an app-level modal opened over it (Get help, the Requests inbox)
    // keeps its surface mounted behind the backdrop, and the Shell renders no
    // card while one is up but expects it back on the way out. The card's own
    // "Report a problem" opens exactly such a modal.
    const heldWorkspaceAnyId =
      this.displayedWorkspaceAnyId ?? overlayBehindWorkspaceId(path, search);
    const heldAgentScoped =
      heldWorkspaceAnyId === null
        ? null
        : this.stores.workspaces.toAgentScopedId(heldWorkspaceAnyId);
    if (
      this.openRecovery !== null &&
      this.openRecovery.agentId !== heldAgentScoped
    ) {
      this.openRecovery = null;
    }
    this.consumeReviewParam(path, search);
    this.channel?.setClientState(path, agentScoped);
  }

  /** Consume a ``?review=<request-id>`` param on the workspace surface -- the
   * landing half of the uniform review gesture (toast click, feed row, Web
   * Notification click, and the Electron OS-notification deep link all route
   * to ``/workspace/<id>?review=<request-id>``).
   *
   * Consumed exactly ONCE per landing (see ``consumedReviewKey``): the param
   * is stripped with a history-REPLACING route set, so Back from wherever the
   * gesture leads never returns to a URL that would re-run it, and then the
   * review popup opens over the workspace if the request is still pending.
   * A resolved or unknown id just leaves the workspace on screen -- the hop
   * itself is still the right landing for a stale click.
   *
   * The actual ``m.route.set`` calls are queued past this render (see the
   * inline comment) rather than issued here: this method runs from inside
   * the router's own ``render()`` (called synchronously while Mithril is
   * resolving the ``?review=`` navigation), and a route change issued
   * synchronously from there is a NESTED one -- it stacks a second resolve
   * on top of the one still committing, which left a stale duplicate popup
   * on screen (the first resolve's card, mid-exit-animation, peeking out
   * from under the second's). Deferring lets this render finish and commit
   * first, so the follow-up navigation behaves like any ordinary
   * click-issued one instead of a reentrant one. */
  private consumeReviewParam(path: string, search: string): void {
    const review = new URLSearchParams(search).get("review");
    if (review === null || review === "") {
      this.consumedReviewKey = null;
      return;
    }
    const workspaceAnyId = workspaceDisplayIdFromPath(path);
    if (workspaceAnyId === null) return;
    const key = `${path}?review=${review}`;
    if (this.consumedReviewKey === key) return;
    this.consumedReviewKey = key;
    const agentScoped = this.stores.workspaces.toAgentScopedId(workspaceAnyId);
    const entry = this.stores.workspaces.entryByAnyId(agentScoped);
    queueMicrotask(() => {
      if (entry !== null && (entry.create_attempt_state ?? "") !== "") {
        // The machine is still setting up: /workspace/<id> would render the
        // Home-looking fallback page, so the deep link lands on the
        // machine's own creating page instead. The ask stays in the bell.
        m.route.set(`/creating/${agentScoped}`, undefined, { replace: true });
        return;
      }
      m.route.set(`/workspace/${agentScoped}`, undefined, { replace: true });
      if (this.stores.requests.requestIds.includes(review)) {
        // Lands as a second history entry over the stripped workspace route
        // (openInbox pushes), so dismissing the popup goes back to the
        // machine. Re-read fresh: the request may have resolved while this
        // microtask was queued.
        this.openInbox({ selected: review });
      }
    });
  }

  paintAccent(workspaceAnyId: string | null): void {
    const root = document.documentElement;
    if (workspaceAnyId === null) {
      root.style.removeProperty("--workspace-accent");
      root.style.removeProperty("--titlebar-bg");
      this.setTitlebarSurface(false);
      return;
    }
    const cached = this.stores.workspaces.accentEntry(workspaceAnyId);
    if (cached?.accent == null) return; // painted when the list next lands
    root.style.setProperty("--workspace-accent", cached.accent);
    root.style.setProperty("--titlebar-bg", cached.accent);
    this.setTitlebarSurface(true);
  }

  /** Re-derive the accent for the current route (list updates, previews). */
  repaintAccentForCurrentRoute(): void {
    this.paintAccent(
      accentSourceForRoute(this.currentRoutePath(), this.currentRouteSearch()),
    );
  }

  private setTitlebarSurface(isOn: boolean): void {
    document
      .getElementById("minds-titlebar")
      ?.classList.toggle("titlebar-surface", isOn);
  }

  /** Publish the failure band's measured height so the workspace surface can
   * shrink by it (see .workspace-surface). A CSS variable rather than view
   * state: nothing needs to re-render for the surface to follow. */
  setNoticeBandHeight(height: number): void {
    document.documentElement.style.setProperty(
      "--notice-band-height",
      `${height}px`,
    );
  }

  /** Whether the recovery card is up over `agentId`. */
  isRecoveryModalOpenFor(agentId: string): boolean {
    return this.openRecovery?.agentId === agentId;
  }

  /** Whether the card that is up was raised by the shell rather than asked for. */
  isRecoveryModalAutoRaised(agentId: string): boolean {
    return (
      this.openRecovery?.agentId === agentId && this.openRecovery.isAutoRaised
    );
  }

  /** The user asked for the card, from the band's "Open recovery". */
  openRecoveryModal(agentId: string): void {
    this.openRecovery = { agentId, isAutoRaised: false };
  }

  /**
   * A machine came back under an auto-raised card: drop the card.
   *
   * Does not reload the stale frame behind it. The server owns that: the
   * tracker's recovery edge broadcasts a ``workspace_refresh``, which every
   * window applies to its own frame -- including the ones with no card up.
   */
  finishRecovery(): void {
    this.openRecovery = null;
  }

  /** The user closed the card, which can happen while the machine is still
   * down -- so no recovery edge is coming, and this is the only thing that
   * repaints the dead page the frame is holding. */
  closeRecoveryModal(): void {
    this.workspaceFrame?.reload();
    this.openRecovery = null;
  }

  /** Close the recovery card if one is up over the displayed machine,
   * reporting whether there was one. ``handleEscape``'s step for the card,
   * ahead of the two route-based overlays it can be raised over.
   *
   * Keyed on ``displayedWorkspaceAnyId``, which ``handleRouteChanged`` derives
   * with the same ``workspaceSurfaceIdFromPath`` the router computes the
   * Shell's ``workspaceParam`` from -- the condition the card is rendered
   * under. The two must keep sharing that derivation: a card held through an
   * app-level modal (see ``handleRouteChanged``) is not rendered, and reporting
   * the key as spent on it would leave Escape unable to dismiss the modal that
   * is actually on top. */
  closeOpenRecoveryModal(): boolean {
    const displayed = this.displayedWorkspaceAnyId;
    if (displayed === null) return false;
    if (
      !this.isRecoveryModalOpenFor(
        this.stores.workspaces.toAgentScopedId(displayed),
      )
    )
      return false;
    this.closeRecoveryModal();
    return true;
  }

  /**
   * A fresh snapshot is starting: give up any card this shell raised itself.
   *
   * After a reconnect the window no longer knows the edge that raised the card
   * still stands. A machine that recovered while the socket was down sends no
   * frame to say so -- hello clears the health store and the snapshot carries
   * only unhealthy agents -- so nothing else would ever drop the card. If the
   * failure does stand, the snapshot says so a moment later and the band
   * offers "Open recovery". A card the user opened themselves survives.
   */
  handleSnapshotStart(): void {
    if (this.openRecovery?.isAutoRaised === true) this.openRecovery = null;
  }

  /**
   * Raise the card on the edge into restart_failed for the displayed machine,
   * and drop an auto-raised one on the edge back into healthy.
   *
   * restart_failed means the app restarted the machine and it is still
   * unresponsive. That is the end of the unattended path, and a one-line band
   * is too quiet for it; every other condition leaves the band as the sole
   * surface and waits to be asked.
   *
   * Only an edge raises it -- ``isSnapshotFrame`` marks the connect-time replay
   * of current state -- so a window that opens onto a machine already in this
   * state gets the band, whose "Open recovery" is one click away.
   *
   * Nothing raises itself while the discovery consumer is dead: every machine
   * reads unhealthy then, and the card's actions all route through the forward
   * that consumer feeds, so it would offer "Restart Machine" over a band
   * correctly saying only restarting Minds can help.
   *
   * A card the user opened stays up when the machine answers -- they asked to
   * be there, and it gets to tell them how it ended.
   */
  handleHealthChanged(
    agentId: string,
    health: WorkspaceHealth,
    isSnapshotFrame: boolean,
  ): void {
    if (health === "healthy") {
      if (
        this.openRecovery?.agentId === agentId &&
        this.openRecovery.isAutoRaised
      )
        this.finishRecovery();
      return;
    }
    if (isSnapshotFrame || health !== "restart_failed") return;
    if (this.openRecovery !== null) return;
    if (this.stores.health.discoveryHealth === "blocked") return;
    const displayed = this.displayedWorkspaceAnyId;
    if (
      displayed === null ||
      this.stores.workspaces.toAgentScopedId(displayed) !== agentId
    )
      return;
    this.openRecovery = { agentId, isAutoRaised: true };
  }

  openSidebar(anchor: {
    x: number;
    y: number;
    width: number;
    height: number;
  }): void {
    this.sidebarAnchor = anchor;
    this.isSidebarOpen = true;
  }

  closeSidebar(): void {
    this.isSidebarOpen = false;
  }

  /** The displayed workspace's stable agent id, or null on a hub page. */
  displayedWorkspaceAgentId(): string | null {
    const displayed = this.displayedWorkspaceAnyId;
    return displayed === null
      ? null
      : this.stores.workspaces.toAgentScopedId(displayed);
  }

  /** Open the bell's feed over the current surface. Opening acknowledges the
   * floating toasts (the feed is their durable home), so they retire. */
  openNotifications(): void {
    this.isNotificationsOpen = true;
    this.notificationsUi?.clearLiveToasts();
  }

  closeNotifications(): void {
    this.isNotificationsOpen = false;
  }

  /**
   * Open the bell's feed, putting away whatever floats on screen first: the
   * raised strip's "go to another surface" for the bell, and equally the
   * titlebar bell's own click (which is only reachable with no titlebar popup
   * up, but IS reachable under a centered app modal -- which must leave, or
   * the feed would raise beneath that modal's backdrop, dimmed and
   * unclickable).
   *
   * Putting a route-backed surface away is a navigation, and an arriving
   * navigation ordinarily closes the feed, so the feed is armed across that
   * one arrival. Opened from a surface with nothing floating, there is
   * nothing to put away and nothing to arm. The options panel's entry is
   * REPLACED, like the other strip switches, so the panel is not one Back
   * away under the feed.
   */
  switchToNotifications(): void {
    this.isNotificationsArmed =
      this.dismissHelpToItsMachine() ||
      this.dismissAppOverlay() ||
      this.closeWorkspaceOverlay({ replace: true });
    this.openNotifications();
  }

  /**
   * Put Get help away by routing straight to the machine it names, rather than
   * back through history, reporting whether that applied.
   *
   * Get help is one click from the docked options panel, so the entry
   * `history.back()` returns to is often that panel -- which the armed feed
   * would then be sitting on top of, two of the five surfaces up at once, each
   * drawing its own raised strip and its own backdrop. Same reasoning
   * `dismissAppOverlay` applies to the request popup, and `replace` for the
   * same reason: the surface landed on is not left one Back away from the modal
   * again.
   *
   * Only with a machine named and no remembered page: the recovery page's Get
   * help forwards the very machine nobody could load (see
   * `pageRouteBehindOverlay`), and with no `?workspace=` at all history is the
   * only thing that knows where this came from -- and no popup can be waiting
   * there to come back up.
   */
  private dismissHelpToItsMachine(): boolean {
    const path = this.currentRoutePath();
    if (path !== "/help" || this.pageRouteBehindOverlay !== null) return false;
    const behind = overlayBehindWorkspaceId(path, this.currentRouteSearch());
    if (behind === null) return false;
    m.route.set(`/workspace/${behind}`, undefined, { replace: true });
    return true;
  }

  /**
   * Open Get help / report a bug, carrying the displayed workspace so the page
   * can offer the in-workspace /assist flow -- only when that workspace's
   * interface is healthy, mirroring the legacy titlebar's assist gating. The
   * one place the bug button's route is built, so the titlebar's own button
   * and its raised copy open the identical page.
   *
   * Arrived at from another titlebar popup's route (the options panel or the
   * request popup, via the raised strip), this is a switch, not a stack: that
   * popup's history entry is replaced rather than built on. (The panel a
   * request popup was remembering is let go by handleRouteChanged when the
   * /help route lands -- the panel lives exactly as long as /inbox does.)
   */
  openHelp(): void {
    const isSwitching = isTitlebarPopupRoutePath(this.currentRoutePath());
    const routeOptions = isSwitching ? { replace: true } : undefined;
    const agentScoped = this.displayedWorkspaceAgentId();
    if (agentScoped === null) {
      m.route.set("/help", undefined, routeOptions);
      return;
    }
    const isHealthy = this.stores.health.isContentAssumedReady(agentScoped);
    m.route.set(
      "/help",
      { workspace: agentScoped, assist: isHealthy ? "1" : "0" },
      routeOptions,
    );
  }
}
