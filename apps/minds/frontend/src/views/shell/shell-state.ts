// Cross-view shell state: which workspace is displayed, accent painting, and
// workspace-entry navigation. The mutable singleton the Shell + frame views
// share (mithril redraws pull from it; imperative document-level effects --
// CSS variables -- happen here).

import m from "mithril";
import type { AppStores } from "../../models/boot";
import type { WorkspaceHealth } from "../../models/health";
import type { UiChannelClient } from "../../channel/client";
import type { RequestVerdict, ResolvedRequest } from "../../models/inbox";
import type { NotificationsUiController } from "../../models/notificationsUi";
import { AutoUpdatePromptModel } from "../../models/autoUpdatePrompt";
import type { UpdateActionResult } from "../../models/updates";
import {
  accentSourceForRoute,
  accountHubRoute,
  accountIdFromHubPath,
  creatingAttemptIdFromPath,
  destroyingWorkspaceIdFromPath,
  hubPagePath,
  isAccountId,
  SETTINGS_PATH,
  classifyRoute,
  isAppOverlayPath,
  isTitlebarPopupRoutePath,
  overlayBehindWorkspaceId,
  popoutFromPath,
  recoveryWorkspaceIdFromPath,
  workspaceDisplayIdFromPath,
  workspacePageIdFromPath,
} from "./classify";
import type { HubPage, PopoutRoute } from "./classify";
import { LOCAL_PROFILE_USER_ID } from "../../models/providers";
import type {
  UiRouteAccountResponse,
  UiSwitchAccountRequest,
  UiSwitchAccountResponse,
} from "../../channel/messages";
import type { PopoutReattachAsk, TearOutReport } from "../../electron-bridge";
import { electronBridge } from "../../electron-bridge";
import { dismissTopmostModal, isAnyModalOpen } from "../components/Modal";
import { recoveryRoute } from "../../models/create";
import { bugReportTextFor } from "../../models/errors";
import type { UserFacingError } from "../../models/errors";
import {
  setPendingHelpLaunch,
  stagedHelpLaunchWorkspace,
} from "../../models/help";
import { WebLoginModel, webLogin } from "../../models/webLogin";
import { ErrorDialogQueue, errorDialogs } from "../../models/errorDialog";
import {
  ClientPermissionPromptModel,
  clientPermissionPrompt,
} from "../../models/clientPermissions";
import type { UiWorkspaceEntry } from "../../channel/messages";
import type { UiWorkspaceApp } from "../../generated/ui";
import type { WorkspaceMessage } from "../../models/workspaceMessage";
import { rowClickActionFor } from "../pages/landing-controls";
import type { SharedEntry, SharedOpenPlan, SharedVerdict } from "../../models/sharedWithMe";
import {
  SharedWithMeError,
  isSharedEntry,
  sharedActionErrorMessage,
  sharedEntryForHost,
  sharedRouteId,
  sharedWithMeApi,
} from "../../models/sharedWithMe";

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

/** Sends a workspace message (a focus-chat ask, an app message) into the
 * mounted frame; false when the frame's page is not listening or not on that
 * workspace, so the shell holds the message. Registered by WorkspaceFrame. */
export type WorkspaceMessageSender = (
  workspaceAgentId: string,
  message: WorkspaceMessage,
) => boolean;

/** Sends a reattach ask into the mounted frame: return the pulled-out window
 * to the desktop. Registered by WorkspaceFrame. */
export type ReattachWindowSender = (windowId: string) => void;

/** Forwards a step of a watched title-bar drag into the mounted frame, when
 * the report names the frame's workspace. Registered by WorkspaceFrame. */
export type TearOutSender = (report: TearOutReport) => void;

/** Sends a link main handed back (a popup's or a frame navigation's URL)
 * into the mounted frame, as minds:open-link, when its page announced it
 * opens links. */
export type OpenLinkSender = (url: string) => void;

/** One pulled-out window as the workspace shell reports it. */
export interface DetachedWindowEntry {
  windowId: string;
  title: string;
}

// How long a reattach waits for the shell to report the window back: past it,
// a popout's own page lets the popout go anyway, and a main window leaves the
// popout to main's own timer (the ghost's "Bring back" remains if the shell
// never took it).
const REATTACH_SETTLE_TIMEOUT_MS = 1000;
// How long after a share was opened a landing of its frame on the accounts
// login page is taken as that open's own failure rather than a session that
// went away later (specs/shared-with-me, 4.7): the open's redeem carries the
// frame onto the share, so right after it the login page means the redeem
// did not; later, it means the partition's session is gone and a fresh open
// signs it in again.
const SHARED_REOPEN_INTERVAL_MS = 30_000;

/** How openSharedWorkspace behaves beyond the plain open from a row. */
interface SharedOpenOptions {
  /** This open follows the redeem's refusal of the partition's other session (now signed out): a second refusal is
   * not retried again. */
  isRetry?: boolean;
  /** Route onto the share only while the window still shows what it did when the open started (a reopen the shell
   * started on its own, which must not yank the window back once the user left). */
  isRoutedOnlyWhileDisplayed?: boolean;
}

/** What the window's shared frame is doing since its last open (specs/shared-with-me, 4.7): the frame draws it
 * over itself until the workspace's page is up, so the redirects of the sign-in never show as a blank frame or a
 * sign-in form. */
export interface SharedFrameProgress {
  shareKey: string;
  phase: "connecting" | "failed";
  /** Why the open failed in the frame, as the verdict the row records; null while connecting, and for an open the
   * desktop refused before the frame loaded anything. */
  verdict: SharedVerdict | null;
  /** The desktop's refusal of the open in its own words (the open route answered no plan, so no sign-in ran and no
   * verdict is recorded); null otherwise. */
  refusalMessage: string | null;
  /** When this open's frame load began (epoch ms), for the overlay's "still connecting" state. */
  startedAtMs: number;
  /** Whether this open already opened once more after the redeem refused the partition's other session. */
  isSignInRetried: boolean;
}

/** What a reopen the shell started on its own came to (reopenDisplayedSharedWorkspace). */
export type SharedReopenOutcome = "reopened" | "not_due" | "refused";

/** Whether `link` is served by `hostname` (case-insensitively; a link that does not parse is nowhere). */
function isLinkOnHost(link: string, hostname: string): boolean {
  try {
    return new URL(link).hostname.toLowerCase() === hostname.toLowerCase();
  } catch {
    return false;
  }
}

interface ReattachWaiter {
  windowId: string;
  resolve: (isReportedBack: boolean) => void;
  settleTimer: ReturnType<typeof setTimeout>;
}

/** The mounted workspace content iframe, as the shell addresses it. */
export interface WorkspaceFrameHandle {
  /** The workspace the frame is navigated to, or null before it is first armed. */
  armedWorkspaceAnyId(): string | null;
  /** Re-navigate the frame to that workspace's root URL. */
  reload(): void;
  /** When the page the frame holds last finished loading (epoch ms), or null
   * before its first load -- whether the embedder or the page itself started it. */
  lastLoadedAtMs(): number | null;
}

/** A control of the chrome's own that a workspace's tour may ask to have ringed: the embed contract's CHROME_ANCHORS. */
export type ChromeAnchor = "workspace-switcher" | "permissions" | "settings" | "share" | "help";

/** One request for a workspace's Updates tab, taken once by the tab: it
 * flashes its status block, and explains a press's failure when there is one. */
export interface UpdatesTabArrival {
  agentId: string;
  serial: number;
  /** A press made elsewhere that failed, which the tab opened to explain. */
  failure: UpdateActionResult | null;
}

/** The local address of an app as the forward serves it: its label under the
 * workspace's own origin (`https://<label>.<workspace id>.localhost:<port>/`),
 * or null when the app has no label yet or the forward's origin is unknown. */
export function workspaceAppUrl(forwardOrigin: string, agentId: string, label: string): string | null {
  if (label === "" || forwardOrigin === "") return null;
  try {
    const origin = new URL(forwardOrigin);
    const port = origin.port === "" ? "" : `:${origin.port}`;
    return `${origin.protocol}//${label}.${agentId}.localhost${port}/`;
  } catch {
    return null;
  }
}

export class ShellState {
  readonly stores: AppStores;
  /** The one-time auto-update offer, one per window, so its answer and a
   * put-away hold across every surface the window shows. */
  readonly autoUpdatePrompt: AutoUpdatePromptModel;
  private pendingUpdatesTabArrival: UpdatesTabArrival | null = null;
  private updatesTabArrivalCount = 0;
  private readonly webLogin: WebLoginModel;
  private readonly errorDialogs: ErrorDialogQueue;
  private readonly clientPermissionPrompt: ClientPermissionPromptModel;
  channel: UiChannelClient | null = null;
  /** The notification arrival controller (toasts, badge relay, OS hint),
   * installed by index.ts like the channel; null only before boot wiring. */
  notificationsUi: NotificationsUiController | null = null;
  isMac = false;
  /** The bar draws macOS-style traffic lights itself (a window off macOS made
   * to look like one, the demo box); with isMac, the bar takes the Mac layout. */
  isTrafficLightsDrawn = false;
  /** The window keeps its native frame (WSL), whose title bar carries the
   * window controls, so the bar draws none. */
  isNativeFrame = false;
  mngrForwardOrigin = "";
  /** The bell's notification feed: local overlay state, not a route, so it
   * pops over whatever surface is on screen (a hub page, the create form, a
   * machine) without navigating and without swapping what is painted behind
   * it -- a route-based app modal over a hub page would fall back to Home. */
  isNotificationsOpen = false;
  /** The titlebar's account switcher popover, local state like the feed. */
  isAccountSwitcherOpen = false;
  /** A switch asked of the backend and not yet landed, so a second press is not a second switch. */
  isAccountSwitchPending = false;
  /**
   * The account this window shows: a signed-in account's user id, a signed-out
   * one's, or `local` for the device. Seeded from the bootstrap, then set on
   * every route change from the hub segment, the displayed workspace's
   * association, or an overlay's ?account=; a sign-in the window asked for
   * adopts the account without a navigation (see adoptAccount). Every hub
   * route the window builds carries it, and the channel scopes its frames to it.
   */
  accountId: string = LOCAL_PROFILE_USER_ID;
  /** The bar's workspace menu, local state like the feed. */
  isWorkspaceSwitcherOpen = false;
  /** Share's "check back after the workspace is created" dialog, raised on a creation page. */
  isShareNotYetCreatedDialogOpen = false;
  /** The workspace whose CONTENT is displayed (null on hub pages). */
  displayedWorkspaceAnyId: string | null = null;
  /** The chrome control the displayed workspace's tour is talking about, ringed in the titlebar; null for none. */
  highlightedChromeAnchor: ChromeAnchor | null = null;
  /** The Settings route the request popup was opened over, so the Shell can
   * keep Settings mounted (and hidden) beneath the popup: the live route is
   * the popup's, which carries none of Settings' params. Null whenever the
   * popup does not float over Settings. */
  settingsRouteBehindOverlay: string | null = null;
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
  /** The same for the account switcher, raised by `switchToAccountSwitcher`. */
  private isAccountSwitcherArmed = false;
  /** The same, for the workspace menu. */
  private isWorkspaceSwitcherArmed = false;
  /** The path the last `handleRouteChanged` saw, so a redraw on the route the
   * window is already on is not mistaken for a navigation to it. */
  private lastHandledRoutePath: string | null = null;
  /** The query that came with it. */
  private lastHandledRouteSearch: string | null = null;
  /** Whether the route the window is on has not said whose account it is yet
   * (a workspace whose row has not arrived), so a later redraw asks it again. */
  private isRouteAccountUnknown = false;
  /** The account the route a switch is landing on belongs to: the backend
   * chose the landing from that account's own rows, so the route's account is
   * known before the account's list has arrived, and the backend is not asked
   * whose it is. Consumed by the navigation it was made for, and dropped by
   * any other. */
  private routeAccountInHand: { path: string; accountId: string } | null = null;
  /** share key -> when the shell last re-opened the share after a login bounce. */
  private sharedReopenAtMsByShareKey = new Map<string, number>();
  /** The open in progress on the window's shared frame, or its failure, until the workspace's page is up. */
  sharedFrameProgress: SharedFrameProgress | null = null;
  /** The ``?review=`` deep link already consumed, keyed by route+id.
   * `handleRouteChanged` runs on EVERY redraw and its stripping route-set
   * lands a tick later, so without this every interim redraw would re-consume
   * the same param and re-open the popup. Cleared once a review-less route is
   * seen (i.e. the strip landed), so a later deep link consumes afresh. */
  private consumedReviewKey: string | null = null;
  /** The ``?chat=`` deep link already consumed, keyed by route+id; same
   * reasoning as ``consumedReviewKey``. */
  private consumedChatKey: string | null = null;
  /** A message the displayed workspace should take once its page is
   * listening. Set by `requestWorkspaceMessage` when the frame cannot take
   * it yet (its page has not announced itself, or it is not on that
   * workspace) and flushed when the announcement arrives. */
  private pendingWorkspaceMessage: {
    workspaceAgentId: string;
    message: WorkspaceMessage;
  } | null = null;
  private workspaceMessageSender: WorkspaceMessageSender | null = null;
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
  private reattachWindowSender: ReattachWindowSender | null = null;
  private tearOutSender: TearOutSender | null = null;
  private openLinkSender: OpenLinkSender | null = null;
  /** The pulled-out windows the mounted shell last reported (the
   * pull-out-window spec, section 5.5). */
  private detachedWindows: readonly DetachedWindowEntry[] = [];
  /** Callers waiting for a window to leave the detached set (a reattach on
   * its way): resolved by the next report without it, or by their timer. */
  private reattachWaiters: ReattachWaiter[] = [];
  /** The mounted Permissions pane's live list, registered while one is up. The
   * popup answers a request; the pane behind it is what shows the list that
   * request was in. */
  private waitingRequestList: WaitingRequestList | null = null;

  constructor(
    stores: AppStores,
    webLoginModel: WebLoginModel = webLogin,
    autoUpdatePrompt: AutoUpdatePromptModel = new AutoUpdatePromptModel(),
    errorDialogQueue: ErrorDialogQueue = errorDialogs,
    clientPermissionPromptModel: ClientPermissionPromptModel = clientPermissionPrompt,
  ) {
    this.stores = stores;
    this.webLogin = webLoginModel;
    this.autoUpdatePrompt = autoUpdatePrompt;
    this.errorDialogs = errorDialogQueue;
    this.clientPermissionPrompt = clientPermissionPromptModel;
  }

  /** Whether this window's workspace frame is showing the workspace named. */
  isWorkspaceFrameOn(agentScopedId: string): boolean {
    const armed = this.workspaceFrame?.armedWorkspaceAnyId() ?? null;
    return (
      armed !== null &&
      this.stores.workspaces.toAgentScopedId(armed) === agentScopedId
    );
  }

  /** Rebuild this window's workspace view, if its frame is showing the one named
   * -- and, given a cutoff, only if its page last loaded before it: a page
   * loaded since already runs what the machine serves now. */
  reloadWorkspaceFrame(agentScopedId: string, loadedBeforeMs: number | null): void {
    const frame = this.workspaceFrame;
    if (frame === null || !this.isWorkspaceFrameOn(agentScopedId)) return;
    const lastLoadedAtMs = frame.lastLoadedAtMs();
    if (loadedBeforeMs !== null && lastLoadedAtMs !== null && lastLoadedAtMs >= loadedBeforeMs) return;
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

  /** The hub route of `page` on this window's account (`/account/<id>/` for home). */
  hubRoute(page: HubPage = "/"): string {
    return accountHubRoute(this.accountId, page);
  }

  /** The query an overlay route (/settings, /help, /inbox) carries so a reload keeps the window's account. */
  overlayQuery(params: Record<string, string> = {}): Record<string, string> {
    return { ...params, account: this.accountId };
  }

  /** Route to one of the hub pages of this window's account. */
  routeToHub(page: HubPage = "/", options?: { replace: boolean }): void {
    m.route.set(this.hubRoute(page), undefined, options);
  }

  /** Whether this window's account is signed out here: a user id with no session, which its hub says instead of offering Create. */
  isAccountSignedOut(): boolean {
    return (
      this.accountId !== LOCAL_PROFILE_USER_ID &&
      !this.stores.accounts.isSignedIn(this.accountId)
    );
  }

  /**
   * Make `accountId` this window's account without navigating: the start flow
   * and the create form, both on the device, take up the account a sign-in they
   * asked for just landed, so the create is submitted under it. The browser
   * URL's hub segment is rewritten in place so a reload keeps the account; the
   * router's own idea of the route is left alone, since the page is not
   * changing. The channel is told at once, so the frames follow.
   */
  adoptAccount(accountId: string): void {
    if (accountId === this.accountId) return;
    const path = this.currentRoutePath();
    const hubAccountId = accountIdFromHubPath(path);
    if (hubAccountId !== null && typeof window !== "undefined") {
      const rewritten = path.replace(
        `/account/${hubAccountId}`,
        `/account/${encodeURIComponent(accountId)}`,
      );
      const search = this.currentRouteSearch();
      window.history.replaceState(
        null,
        "",
        rewritten + (search ? `?${search}` : ""),
      );
    }
    this.takeUpAccount(accountId);
  }

  /** Become `accountId`'s window and tell the channel, so its frames (the account's rows first) follow. */
  private takeUpAccount(accountId: string): void {
    this.accountId = accountId;
    // A switch or an adoption settles whose window this is, so an answer still
    // out for the route it happened on is dropped rather than undoing it.
    this.isRouteAccountUnknown = false;
    this.channel?.setClientState(
      this.currentRoutePath(),
      this.displayedWorkspaceAgentId(),
      this.accountId,
    );
  }

  openAccountSwitcher(): void {
    this.isWorkspaceSwitcherOpen = false;
    this.isAccountSwitcherOpen = true;
  }

  closeAccountSwitcher(): void {
    this.isAccountSwitcherOpen = false;
  }

  /**
   * Open the account menu from a raised strip, putting the open flyout away
   * first. Like the feed (see switchToNotifications), the menu is armed
   * across the navigation that puts a route-backed flyout away, so that
   * arrival does not close it the way an ordinary navigation does.
   */
  switchToAccountSwitcher(): void {
    this.closeNotifications();
    this.isAccountSwitcherArmed = this.dismissAppOverlay();
    this.openAccountSwitcher();
  }

  /** The same for the workspace menu. */
  switchToWorkspaceSwitcher(): void {
    this.closeNotifications();
    this.isWorkspaceSwitcherArmed = this.dismissAppOverlay();
    this.openWorkspaceSwitcher();
  }

  /**
   * Move this window to `accountId`: the backend answers where to land (the
   * account's most recent workspace no other window shows, else its hub) and
   * the window routes there like any navigation, so the desktop's one-window
   * rule still runs; a claim lost to another window lands on the hub, the
   * documented fallback. The window becomes the account's before it routes:
   * a workspace landing is one the old account's list does not know, so the
   * route alone could not say whose it is, and the channel has to be on the
   * new account for that row to arrive at all.
   */
  async switchAccount(accountId: string): Promise<void> {
    this.closeAccountSwitcher();
    if (accountId === this.accountId || this.isAccountSwitchPending) return;
    this.isAccountSwitchPending = true;
    const body: UiSwitchAccountRequest = {
      account_id: accountId,
      client_id: this.channel?.clientId ?? "",
      is_desktop: electronBridge.isDesktop,
    };
    const hubRoute = accountHubRoute(accountId);
    let route = hubRoute;
    try {
      const response = await fetch("/ui/api/windows/switch-account", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (response.ok)
        route = ((await response.json()) as UiSwitchAccountResponse).route;
      else
        console.warn(
          `[account-switch] switch-account answered ${response.status}; landing on the account's hub`,
        );
    } catch (error) {
      // The hub is the landing when the backend cannot say better.
      console.warn(
        `[account-switch] switch-account could not be asked; landing on the account's hub: ${String(error)}`,
      );
    } finally {
      this.isAccountSwitchPending = false;
    }
    this.takeUpAccount(accountId);
    this.routeAccountInHand = { path: route.split("?")[0], accountId };
    this.routeTo(route, undefined, hubRoute);
    m.redraw();
  }

  /** The account in hand for a landing on `path`, when a switch left one for it. */
  private routeAccountInHandFor(path: string): string | null {
    const inHand = this.routeAccountInHand;
    return inHand !== null && inHand.path === path ? inHand.accountId : null;
  }

  /** The workspace (either coordinate) or create attempt a route names in its path, else null. An overlay's
   * `?workspace=` is not a route's own subject: see `overlayBehindWorkspaceId`. */
  private static routeSubjectIdFromPath(path: string): string | null {
    return (
      workspacePageIdFromPath(path) ??
      popoutFromPath(path)?.workspaceAnyId ??
      recoveryWorkspaceIdFromPath(path) ??
      creatingAttemptIdFromPath(path) ??
      destroyingWorkspaceIdFromPath(path) ??
      null
    );
  }

  /** The account a route is for, from what the route itself says; null when it says nothing (keep the window's). */
  accountIdForRoute(path: string, search: string): string | null {
    const hubAccountId = accountIdFromHubPath(path);
    if (hubAccountId !== null) return hubAccountId;
    const workspaceAnyId =
      ShellState.routeSubjectIdFromPath(path) ??
      overlayBehindWorkspaceId(path, search) ??
      null;
    if (workspaceAnyId !== null) {
      // A workspace the list does not know yet (a cold start on it) says nothing until it arrives.
      const entry = this.stores.workspaces.entryByAnyId(workspaceAnyId);
      return entry === null
        ? null
        : entry.account_user_id || LOCAL_PROFILE_USER_ID;
    }
    // An overlay's ?account=, when it names an account id; anything else says nothing, as the backend reads it.
    const queryAccountId = new URLSearchParams(search).get("account");
    if (queryAccountId !== null && isAccountId(queryAccountId))
      return queryAccountId;
    // The start flow runs on the device, whatever is signed in.
    if (path === "/start" || path === "/consent") return LOCAL_PROFILE_USER_ID;
    return null;
  }

  /**
   * Ask the backend whose a route is when the list cannot say. The list holds
   * the window's own account's rows, so a workspace of another account (a step
   * back onto the one the window showed before a switch, a workspace moved to
   * another account) never arrives on this connection; its association is in
   * the records, which the backend reads the way it seeds a window's account.
   * The answer counts only while the window is still on that route and the
   * list has not said first.
   */
  private async askRouteAccount(path: string, search: string): Promise<void> {
    let accountId: string;
    try {
      const params = new URLSearchParams({ path, query: search });
      const response = await fetch(
        `/ui/api/windows/route-account?${params.toString()}`,
        { credentials: "same-origin" },
      );
      if (!response.ok) {
        console.warn(
          `[account] route-account answered ${response.status} for ${path}; the window keeps its account`,
        );
        return;
      }
      accountId = ((await response.json()) as UiRouteAccountResponse)
        .account_id;
      if (typeof accountId !== "string" || accountId === "") {
        console.warn(
          `[account] route-account named no account for ${path}; the window keeps its account`,
        );
        return;
      }
    } catch (error) {
      console.warn(
        `[account] route-account could not be asked for ${path}; the window keeps its account: ${String(error)}`,
      );
      return;
    }
    if (path !== this.lastHandledRoutePath || !this.isRouteAccountUnknown)
      return;
    this.isRouteAccountUnknown = false;
    if (accountId !== this.accountId) this.takeUpAccount(accountId);
    m.redraw();
  }

  /** Enter a workspace: route to the content surface for its identity.
   * `query` carries route params along (the ``?review=`` deep link an
   * OS-notification click arrives with); empty adds nothing. */
  enterWorkspace(anyId: string, query: Record<string, string> = {}): void {
    const agentScoped = this.stores.workspaces.toAgentScopedId(anyId);
    this.routeTo(`/workspace/${agentScoped}`, query);
  }

  /** Set the route without putting a second main window on a workspace. A
   * route onto the surface of a workspace this window is not showing first
   * asks the desktop app, which raises that workspace's own window instead
   * when it has one (handing it the route) and leaves this window where it
   * is -- or sends it to `fallbackRoute` when one is given. Outside the
   * desktop app, where windows and tabs are not deduplicated, it is a plain
   * route set. */
  routeTo(
    route: string,
    params?: Record<string, string>,
    fallbackRoute: string | null = null,
  ): void {
    const setRoute = (): void => {
      if (params === undefined) m.route.set(route);
      else m.route.set(route, params);
    };
    const targetAnyId = workspaceDisplayIdFromPath(route.split("?")[0]);
    const displayed = this.displayedWorkspaceAnyId;
    const workspaces = this.stores.workspaces;
    if (
      targetAnyId === null ||
      (displayed !== null &&
        workspaces.toAgentScopedId(displayed) === workspaces.toAgentScopedId(targetAnyId))
    ) {
      setRoute();
      return;
    }
    const claim = electronBridge.claimWorkspaceWindow(
      workspaces.toAgentScopedId(targetAnyId),
      params === undefined ? route : m.buildPathname(route, params),
    );
    if (claim === null) {
      setRoute();
      return;
    }
    void claim.then(
      (isOpenedElsewhere) => {
        if (!isOpenedElsewhere) setRoute();
        else if (fallbackRoute !== null) m.route.set(fallbackRoute);
      },
      // No answer from main: navigating here is better than going nowhere.
      () => setRoute(),
    );
  }

  /** Land in a workspace on one of its chats: asked of the frame directly when
   * the workspace is already on screen, with whatever floats over it (Settings,
   * or an app modal opened over it) put away so the chat is
   * not left behind it, else carried as ``?chat=`` for handleRouteChanged to
   * forward once the frame is up. */
  openWorkspaceChat(anyId: string, chatAgentId: string): void {
    const agentScoped = this.stores.workspaces.toAgentScopedId(anyId);
    if (this.displayedWorkspaceAgentId() === agentScoped) {
      this.dismissAppOverlay();
      this.requestFocusChat(agentScoped, chatAgentId);
      return;
    }
    this.enterWorkspace(agentScoped, { chat: chatAgentId });
  }

  /** Open one of an owned workspace's apps from the home page
   * (specs/home-screen-redesign, 4.11): enter the workspace the way its card
   * does, then ask it to open the app's address, held until its page is
   * listening. An app with no address yet opens the workspace alone. */
  openWorkspaceApp(entry: UiWorkspaceEntry, app: UiWorkspaceApp, liveness: string): void {
    const url = workspaceAppUrl(this.mngrForwardOrigin, entry.id, app.label ?? "");
    // A blocked workspace goes nowhere, so no message is held for it: one
    // would fire whenever the workspace is next entered, for any reason.
    if (!this.enterWorkspaceOrRecover(entry, liveness)) return;
    if (url !== null) this.requestWorkspaceMessage(entry.id, { kind: "open-link", url });
  }

  /** Answer an update refused for want of a signed-in AI account: enter the
   * workspace, then ask it for a new chat with the provider chooser up. The
   * entry comes first because the chat opens only in a connected frame. */
  async signInForUpdate(anyId: string): Promise<UpdateActionResult> {
    const agentScoped = this.stores.workspaces.toAgentScopedId(anyId);
    this.enterWorkspace(agentScoped);
    return await this.stores.updates.signIn(agentScoped);
  }

  /** Enter a workspace the way its machines-list row does: onto its surface
   * when healthy and running, otherwise onto Recovery (with a start for a
   * stopped container), which returns to the surface. `liveness` is the entry's
   * raw field or the tracker's displayed reading. Answers whether it went
   * anywhere: false for a blocked row. */
  enterWorkspaceOrRecover(entry: UiWorkspaceEntry, liveness: string): boolean {
    const isHealthy = this.stores.health.statusFor(entry.id) === "healthy";
    const returnTo = `/goto/${entry.id}/`;
    switch (rowClickActionFor(entry, liveness, isHealthy)) {
      case "recover":
        m.route.set(recoveryRoute(entry.id, returnTo, null));
        return true;
      case "recover-start":
        m.route.set(recoveryRoute(entry.id, returnTo, "start"));
        return true;
      case "enter":
        this.enterWorkspace(entry.id);
        return true;
      case "blocked":
        // The row is not rendered clickable; there is nowhere useful to go.
        return false;
    }
  }

  /**
   * Open a workspace shared with this window's account (specs/shared-with-me,
   * 4.7): the desktop records the acceptance and answers the target's link
   * and the redeem URL; the window routes onto the workspace surface, whose
   * frame loads the redeem URL, which signs this window's partition in on
   * the accounts origin and sends the frame on to the link. Throws the
   * desktop's refusal as a SharedWithMeError (its message and code, as the
   * row shows them).
   */
  async openSharedWorkspace(shareKey: string, app: string | null, options: SharedOpenOptions = {}): Promise<void> {
    // Stamped before anything is awaited: a landing of the frame on the
    // accounts login page during this open's round trips is this open's to
    // answer, not a reason to start another.
    this.sharedReopenAtMsByShareKey.set(shareKey, Date.now());
    await this.runSharedOpen(shareKey, app, options, this.displayedWorkspaceAnyId);
  }

  /** The round trips of openSharedWorkspace; `displayedAtStart` is what the window showed when the open began, kept
   * across the retry a refused sign-in starts. */
  private async runSharedOpen(
    shareKey: string,
    app: string | null,
    { isRetry = false, isRoutedOnlyWhileDisplayed = false }: SharedOpenOptions,
    displayedAtStart: string | null,
  ): Promise<void> {
    let plan: SharedOpenPlan;
    try {
      plan = await sharedWithMeApi.open(this.accountId, shareKey, app);
    } catch (error) {
      console.warn(`[shared-with-me] could not open ${shareKey}: ${String(error)}`);
      if (error instanceof SharedWithMeError) throw error;
      throw new SharedWithMeError(error instanceof Error ? error.message : String(error), null, 0);
    }
    // A reopen the shell started on its own must not pull the window back onto
    // the share when the user went elsewhere during the round trips; the open
    // it was retrying is abandoned with it, so nothing is in progress there.
    if (isRoutedOnlyWhileDisplayed && this.displayedWorkspaceAnyId !== displayedAtStart) {
      if (this.sharedFrameProgress?.shareKey === shareKey) this.sharedFrameProgress = null;
      return;
    }
    // Stamped again now that the frame is about to load: the half minute a
    // login-page landing is read as this open's runs from here.
    this.sharedReopenAtMsByShareKey.set(shareKey, Date.now());
    this.sharedFrameProgress = {
      shareKey: plan.share_key,
      phase: "connecting",
      verdict: null,
      refusalMessage: null,
      startedAtMs: Date.now(),
      isSignInRetried: isRetry,
    };
    const routeId = sharedRouteId(plan.share_key);
    const isFrameOnShare = this.isWorkspaceFrameOn(this.stores.workspaces.toAgentScopedId(routeId));
    const loadUrlBefore = this.stores.workspaces.workspaceFrameUrl(routeId);
    // The store's change re-arms a mounted frame whose URL moved (onto the
    // redeem, or another target's link); a frame already on the chosen link
    // with no redeem to go through is reloaded outright.
    this.stores.workspaces.chooseSharedLink(plan.share_key, plan.link, plan.redeem_url);
    if (!isFrameOnShare) this.routeTo(`/workspace/${routeId}`);
    else if (this.stores.workspaces.workspaceFrameUrl(routeId) === loadUrlBefore) this.workspaceFrame?.reload();
    m.redraw();
  }

  /** The shared workspace's own page is up in the frame: its open is done. */
  sharedFrameReady(shareKey: string): void {
    if (this.sharedFrameProgress?.shareKey === shareKey) this.sharedFrameProgress = null;
  }

  /** The open of the displayed shared workspace failed in the frame: the overlay says so, and the row records it. */
  private failDisplayedSharedOpen(verdict: SharedVerdict): void {
    const entry = this.displayedSharedEntry();
    if (entry === null) return;
    this.showDisplayedSharedOpenFailed(entry, verdict, null);
    void sharedWithMeApi.recordVerdict(this.accountId, entry.share.share_key, verdict).catch((error: unknown) => {
      console.warn(`[shared-with-me] could not record the verdict for ${entry.share.share_key}: ${String(error)}`);
    });
  }

  /** The desktop refused to open the displayed shared workspace (no plan, so the frame loaded nothing): the overlay
   * says so in the refusal's words, as the row's own Open would, and no verdict is recorded. */
  private refuseDisplayedSharedOpen(error: unknown): void {
    const entry = this.displayedSharedEntry();
    if (entry === null) return;
    this.showDisplayedSharedOpenFailed(entry, null, sharedActionErrorMessage(error));
  }

  private showDisplayedSharedOpenFailed(
    entry: SharedEntry,
    verdict: SharedVerdict | null,
    refusalMessage: string | null,
  ): void {
    const progress = this.sharedFrameProgress;
    const isSameOpen = progress?.shareKey === entry.share.share_key;
    this.sharedFrameProgress = {
      shareKey: entry.share.share_key,
      phase: "failed",
      verdict,
      refusalMessage,
      startedAtMs: isSameOpen ? progress.startedAtMs : Date.now(),
      isSignInRetried: isSameOpen ? progress.isSignInRetried : false,
    };
    m.redraw();
  }

  /** The shared row the window displays, or null when it shows something else. */
  private displayedSharedEntry(): SharedEntry | null {
    const displayed = this.displayedWorkspaceAnyId;
    const entry = displayed === null ? null : this.stores.workspaces.entryByAnyId(displayed);
    return entry !== null && isSharedEntry(entry) ? entry : null;
  }

  /** The app of the target the share's frame is on, as openSharedWorkspace takes it (null for the shell): an open
   * the shell starts on its own goes back to that target, not to the shell. */
  sharedFrameTargetApp(entry: SharedEntry): string | null {
    const frameLink = this.stores.workspaces.sharedFrameLink(entry.share.share_key);
    const app = entry.share.targets.find((target) => target.link === frameLink)?.app ?? null;
    return app === "system_interface" ? null : app;
  }

  /**
   * Record what a shared workspace's frame document answered, for the row
   * whose link the frame loaded. Main relays the window's own frame's
   * document responses under the share's domain (never a frame the workspace
   * nests); the frame may since have moved to another host under the domain,
   * and only a response from the host of the link it was opened on speaks
   * for the share.
   */
  async recordSharedFrameVerdict(hostname: string, verdict: SharedVerdict): Promise<void> {
    const entry = sharedEntryForHost(this.stores.workspaces.workspaces, hostname);
    if (entry === null || !isSharedEntry(entry)) return;
    const frameLink = this.stores.workspaces.sharedFrameLink(entry.share.share_key);
    if (frameLink === null || !isLinkOnHost(frameLink, hostname)) return;
    if (this.sharedFrameProgress?.shareKey === entry.share.share_key) {
      this.sharedFrameProgress =
        verdict === "admitted"
          ? null
          : { ...this.sharedFrameProgress, phase: "failed", verdict, refusalMessage: null };
      m.redraw();
    }
    try {
      await sharedWithMeApi.recordVerdict(this.accountId, entry.share.share_key, verdict);
    } catch (error) {
      console.warn(`[shared-with-me] could not record the verdict for ${hostname}: ${String(error)}`);
    }
  }

  /**
   * The redeem page refused the displayed shared workspace's frame
   * (specs/shared-with-me, 4.8). A partition that held another account's
   * session has been signed out by main, and the spent code is replaced by
   * opening once more; a second such refusal, or any other, is the open
   * failing.
   */
  async handleSharedFrameRedeemRefused(refusal: "session_mismatch" | "refused"): Promise<void> {
    const entry = this.displayedSharedEntry();
    if (entry === null) return;
    const progress = this.sharedFrameProgress;
    const isRetryDue =
      refusal === "session_mismatch" && progress?.shareKey === entry.share.share_key && !progress.isSignInRetried;
    if (!isRetryDue) {
      this.failDisplayedSharedOpen("sign_in_failed");
      return;
    }
    try {
      await this.runSharedOpen(
        entry.share.share_key,
        this.sharedFrameTargetApp(entry),
        { isRetry: true, isRoutedOnlyWhileDisplayed: true },
        this.displayedWorkspaceAnyId,
      );
    } catch (error) {
      console.warn(`[shared-with-me] could not open ${entry.share.share_key} again after a session mismatch: ${String(error)}`);
      this.refuseDisplayedSharedOpen(error);
    }
  }

  /**
   * The displayed shared workspace's frame landed on the accounts login page
   * instead of the share. Right after an open, that is the open's own redeem
   * failing (a spent code, or a confirmation the broker did not see), and the
   * open has failed; later, it means the partition's session is gone, so the
   * workspace is opened again, which redeems a fresh sign-in (a refusal of
   * that open is shown as the desktop's own). The frame never shows the
   * sign-in form itself: Google sign-in cannot work there.
   */
  async handleSharedFrameLoginLanding(isSignInFailed: boolean): Promise<void> {
    if (isSignInFailed) {
      this.failDisplayedSharedOpen("sign_in_failed");
      return;
    }
    const outcome = await this.reopenDisplayedSharedWorkspace();
    if (outcome === "not_due") this.failDisplayedSharedOpen("sign_in_failed");
  }

  /**
   * Open the displayed shared workspace again when its last open is more than
   * half a minute old, which signs the partition in afresh. A refused open is
   * logged and shown as the desktop's refusal (the bridge handler that calls
   * this cannot catch it); with nothing displayed, nothing is due.
   */
  async reopenDisplayedSharedWorkspace(): Promise<SharedReopenOutcome> {
    const entry = this.displayedSharedEntry();
    if (entry === null) return "not_due";
    const shareKey = entry.share.share_key;
    const lastMs = this.sharedReopenAtMsByShareKey.get(shareKey) ?? 0;
    if (Date.now() - lastMs < SHARED_REOPEN_INTERVAL_MS) return "not_due";
    try {
      await this.openSharedWorkspace(shareKey, this.sharedFrameTargetApp(entry), {
        isRoutedOnlyWhileDisplayed: true,
      });
      return "reopened";
    } catch (error) {
      console.warn(`[shared-with-me] could not sign ${shareKey} in again: ${String(error)}`);
      this.refuseDisplayedSharedOpen(error);
      return "refused";
    }
  }

  /** Retry the displayed shared workspace's open from its overlay: forget the last verdict, then open the target
   * the frame was on. */
  async retryDisplayedSharedOpen(): Promise<void> {
    const entry = this.displayedSharedEntry();
    if (entry === null) return;
    const shareKey = entry.share.share_key;
    try {
      await sharedWithMeApi.retry(this.accountId, shareKey);
      await this.openSharedWorkspace(shareKey, this.sharedFrameTargetApp(entry));
    } catch (error) {
      console.warn(`[shared-with-me] could not retry ${shareKey}: ${String(error)}`);
      this.refuseDisplayedSharedOpen(error);
    }
  }

  /** Open the request-review popup over the current surface: forward the
   * displayed workspace as ?workspace so it floats over that live workspace
   * (kept mounted) instead of navigating the base layer to Home, mirroring how
   * Get help forwards ?workspace. Opened from Home (no workspace displayed), it
   * carries none and floats over Home. Extra query params (`selected` for a
   * named request) are merged in.
   *
   * Opened from Settings, the route it was opened from is remembered so that
   * Settings stays mounted underneath rather than being torn down and rebuilt
   * around the popup. */
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
    // arriving while the popup is already up does not drop the Settings it
    // floats over -- the route is /inbox by then, which names no Settings.
    if (path === SETTINGS_PATH)
      this.settingsRouteBehindOverlay = m.route.get() ?? null;
    const displayed = this.displayedWorkspaceAnyId;
    const query = this.overlayQuery(
      displayed === null
        ? params
        : {
            ...params,
            workspace: this.stores.workspaces.toAgentScopedId(displayed),
          },
    );
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
    this.pageRouteBehindOverlay =
      recoveryWorkspaceIdFromPath(route.split("?")[0]) !== null ? route : null;
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

  registerReattachWindowSender(sender: ReattachWindowSender): void {
    this.reattachWindowSender = sender;
  }

  unregisterReattachWindowSender(sender: ReattachWindowSender): void {
    if (this.reattachWindowSender === sender) this.reattachWindowSender = null;
  }

  registerTearOutSender(sender: TearOutSender): void {
    this.tearOutSender = sender;
  }

  unregisterTearOutSender(sender: TearOutSender): void {
    if (this.tearOutSender === sender) this.tearOutSender = null;
  }

  /** Main reported a step of the title-bar drag it is watching; the mounted
   * frame hears it when the report names its workspace. */
  handleTearOut(report: TearOutReport): void {
    this.tearOutSender?.(report);
  }

  registerOpenLinkSender(sender: OpenLinkSender): void {
    this.openLinkSender = sender;
  }

  unregisterOpenLinkSender(sender: OpenLinkSender): void {
    if (this.openLinkSender === sender) this.openLinkSender = null;
  }

  /** Main caught a popup or frame navigation the mounted workspace asked for
   * and hands its URL back: the workspace opens it where it belongs. */
  handleOpenLink(url: string): void {
    if (this.openLinkSender === null) {
      console.warn(`[link-routing] dropped a link main handed back, since no workspace frame is mounted: ${url}`);
      return;
    }
    this.openLinkSender(url);
  }

  /** The popout this window is, when its route is a popout's. */
  popoutRoute(): PopoutRoute | null {
    return popoutFromPath(this.currentRoutePath());
  }

  /** The title the mounted shell last reported for a pulled-out window, else
   * a placeholder until it does. */
  popoutWindowTitle(windowId: string): string {
    return (
      this.detachedWindows.find((entry) => entry.windowId === windowId)
        ?.title ?? "…"
    );
  }

  /** The mounted shell reported its pulled-out windows. In a popout window
   * this is what says whether the window is still out: absent means it was
   * closed for everyone or brought back from the desktop, and the popout
   * closes itself. */
  handleDetachedWindows(
    windows: readonly DetachedWindowEntry[],
    onTitle: (title: string) => void,
    onGone: () => void,
  ): void {
    const previous = this.detachedWindows;
    this.detachedWindows = windows;
    const present = new Set(windows.map((entry) => entry.windowId));
    const waiting = this.reattachWaiters;
    this.reattachWaiters = waiting.filter((waiter) => present.has(waiter.windowId));
    for (const waiter of waiting) {
      if (present.has(waiter.windowId)) continue;
      clearTimeout(waiter.settleTimer);
      waiter.resolve(true);
    }
    const popout = this.popoutRoute();
    if (popout === null) return;
    const own = windows.find((entry) => entry.windowId === popout.windowId);
    if (own === undefined) {
      onGone();
      return;
    }
    const previousTitle = previous.find((entry) => entry.windowId === popout.windowId)?.title;
    if (own.title !== previousTitle) onTitle(own.title);
  }

  /** Main asks this window to return a pulled-out window of the workspace it
   * shows to the desktop (the popout is closing): the mounted shell takes the window back. Resolves true
   * once the shell reports it back, which is main's word to close the popout
   * (its own page may not have loaded to see it), and false when this window
   * did not take it (it shows some other workspace, or is a popout itself,
   * whose shell is a view of one window and owns no desktop), when the shell
   * never reported the window out, or when no report came in time. */
  handleReattachPopoutWindow(ask: PopoutReattachAsk): Promise<boolean> {
    const sender = this.reattachWindowSender;
    const displayed = this.displayedWorkspaceAnyId;
    if (sender === null || displayed === null || this.popoutRoute() !== null) return Promise.resolve(false);
    // Either spelling on either side: a restored popout or a cold-started
    // main window may still name the workspace by its host-scoped id.
    const workspaces = this.stores.workspaces;
    if (workspaces.toAgentScopedId(displayed) !== workspaces.toAgentScopedId(ask.workspaceId)) {
      return Promise.resolve(false);
    }
    const reportedBack = this.whenReportedBack(ask.windowId);
    sender(ask.windowId);
    return reportedBack;
  }

  /** Ask this popout's own shell to return its window to the desktop: the
   * way back when no desktop window can take it. Resolves when the shell reports the window back (which it does
   * only once its save has landed), or after a short wait when it never does. */
  async returnPopoutToDesktop(): Promise<void> {
    const popout = this.popoutRoute();
    const sender = this.reattachWindowSender;
    if (popout === null || sender === null) return;
    const reportedBack = this.whenReportedBack(popout.windowId);
    sender(popout.windowId);
    await reportedBack;
  }

  /** Resolves true once the mounted shell's report lacks ``windowId``, and
   * false after a short wait, or at once when the last report did not have it
   * out to begin with. Taken before the reattach is sent, so a report that
   * follows the send is not missed. */
  private whenReportedBack(windowId: string): Promise<boolean> {
    if (!this.detachedWindows.some((entry) => entry.windowId === windowId)) return Promise.resolve(false);
    return new Promise((resolve) => {
      const waiter: ReattachWaiter = {
        windowId,
        resolve,
        settleTimer: setTimeout(() => {
          this.reattachWaiters = this.reattachWaiters.filter((candidate) => candidate !== waiter);
          resolve(false);
        }, REATTACH_SETTLE_TIMEOUT_MS),
      };
      this.reattachWaiters.push(waiter);
    });
  }

  registerWorkspaceMessageSender(sender: WorkspaceMessageSender): void {
    this.workspaceMessageSender = sender;
  }

  /** Drop `sender` if it is still the registered one (a frame torn down after
   * its successor registered must not clear the successor's). */
  unregisterWorkspaceMessageSender(sender: WorkspaceMessageSender): void {
    if (this.workspaceMessageSender === sender)
      this.workspaceMessageSender = null;
  }

  /** Post a message into the workspace's page (a notification's click: show
   * a chat, or show an app's page). Sent now when the mounted page is that
   * workspace's and listening; otherwise held until it announces itself,
   * which calls `flushPendingWorkspaceMessage`.
   * Fire-and-forget: a workspace that does not know the message just stays
   * where it is. A newer message replaces an older unsent one. Landing in
   * the workspace is leaving the feed, like a row's route jump, so the
   * popover closes even though the in-place send changes no route. */
  requestWorkspaceMessage(
    workspaceAgentId: string,
    message: WorkspaceMessage,
  ): void {
    this.closeNotifications();
    const sender = this.workspaceMessageSender;
    if (sender !== null && sender(workspaceAgentId, message)) {
      this.pendingWorkspaceMessage = null;
      return;
    }
    this.pendingWorkspaceMessage = { workspaceAgentId, message };
  }

  /** Ask the workspace to show one of its chats (an agent message's click). */
  requestFocusChat(workspaceAgentId: string, chatAgentId: string): void {
    this.requestWorkspaceMessage(workspaceAgentId, {
      kind: "focus-chat",
      chatAgentId,
    });
  }

  /** The mounted page announced that it is listening: send the message it
   * was holding, if it is for the workspace now on screen. One for some
   * other workspace stays held (the reader may be on their way there). */
  flushPendingWorkspaceMessage(): void {
    const pending = this.pendingWorkspaceMessage;
    const sender = this.workspaceMessageSender;
    if (pending === null || sender === null) return;
    if (sender(pending.workspaceAgentId, pending.message))
      this.pendingWorkspaceMessage = null;
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

  /** Dismiss an open floating route (Settings, Get help, the request popup,
   * the AI-keys dialog), returning to the surface it was opened over, and
   * report whether there was one. Prefers history so the opener (Home, Create,
   * the workspace) is restored exactly; falls back to routing to the base when
   * there is no history (a cold-start deep link).
   *
   * A flyout naming a machine is one exception: it routes straight to that
   * machine, for the reasons on `dismissFlyoutToItsMachine`.
   *
   * The request popup (/inbox) is the other exception: routed there directly
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
    // The fixed app modals are always closeable; the New workspace template
    // stepper is a closeable modal only while it floats over a machine
    // (?workspace=) -- with none it is a redirect, not an overlay.
    const isCloseable =
      isAppOverlayPath(path) ||
      (hubPagePath(path) === "/create/template" &&
        overlayBehindWorkspaceId(path, search) !== null);
    if (!isCloseable) return false;
    // history.back() does not update the route synchronously, so a second
    // dismissal arriving before it lands (a repeated Escape) would fire
    // another back() and over-navigate past the opener. Reported as handled
    // even so: the key belongs to the overlay that is still on its way out.
    if (this.isAppOverlayClosing) return true;
    this.isAppOverlayClosing = true;
    if (this.dismissFlyoutToItsMachine()) return true;
    if (path === "/inbox") {
      // Replace, not push: this collapses the popup's own entry into the
      // workspace view rather than adding a further forward step, so the
      // popup is not left sitting one Back away and the stack this
      // dismissal walks away from does not grow on every review.
      const behind = overlayBehindWorkspaceId(path, search);
      m.route.set(
        behind !== null ? `/workspace/${behind}` : this.hubRoute(),
        undefined,
        {
          replace: true,
        },
      );
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
    m.route.set(behind !== null ? `/workspace/${behind}` : this.hubRoute());
    return true;
  }

  /**
   * Go back to the Settings this popup was opened from, reporting whether
   * there was one. It restores Settings' own route, so the tab and section it
   * was left on come back with it.
   */
  returnToSettingsBehindOverlay(): boolean {
    const settingsRoute = this.settingsRouteBehindOverlay;
    if (settingsRoute === null) return false;
    // Left set: handleRouteChanged drops it on arrival, and until then the
    // Settings underneath keeps rendering rather than blinking out. Replace,
    // so Back from Settings does not re-raise the popup being left.
    m.route.set(settingsRoute, undefined, { replace: true });
    return true;
  }

  /**
   * Dismiss a floating route the way the person dismissing it means it,
   * reporting whether there was one.
   *
   * Differs from `closeAppOverlay` for one surface: the request popup opened
   * from Settings' "Waiting on you" list, which goes back to the Settings it
   * was opened from rather than out to the workspace.
   */
  dismissAppOverlay(): boolean {
    if (this.currentRoutePath() === "/inbox" && this.settingsRouteBehindOverlay !== null) {
      // Same re-entrancy guard as closeAppOverlay: one dismissal can arrive
      // twice (the in-document Escape plus Electron's forward of it).
      if (this.isAppOverlayClosing) return true;
      this.isAppOverlayClosing = true;
      return this.returnToSettingsBehindOverlay();
    }
    return this.closeAppOverlay();
  }

  /**
   * Hand Settings back once the request it was opened from has been answered,
   * reporting whether there was a Settings to hand back. A request opened
   * from the chat has none, and its page simply closes.
   *
   * Back to Settings' own route while other requests are still waiting: the
   * list the reader picked from is still there, still theirs to work through,
   * and it is the section they left. Once nothing is waiting that list is gone,
   * so returning them to it would return them to nothing -- they land on Add
   * connection instead, the next thing anyone is in that pane to do.
   */
  returnToSettingsAfterRequest(): boolean {
    const settingsRoute = this.settingsRouteBehindOverlay;
    if (settingsRoute === null) return false;
    // Asked after the answer, and the pane drops an answered request the
    // moment it is answered, so "any left" is already the list without it.
    if (this.waitingRequestList?.hasWaitingRequests() === true)
      return this.returnToSettingsBehindOverlay();
    const [path, query = ""] = settingsRoute.split("?");
    const params = new URLSearchParams(query);
    params.set("tab", "workspace");
    params.set("group", "permissions");
    params.set("perm_section", "add-connection");
    m.route.set(`${path}?${params.toString()}`, undefined, { replace: true });
    return true;
  }

  /**
   * Dismiss the topmost dismissible surface, reporting whether there was one.
   *
   * The one place that knows what is stacked over what, so the precedence is a
   * plain ordered list rather than something the surfaces negotiate through
   * listener registration order (which follows mount order, not z-order).
   *
   * The error dialog leads: it is drawn above every other surface, the
   * two menus included. Then the menus (z-[200]), then the
   * client-permission prompt, which the Shell emits last of all the z-[110]
   * surfaces, then the browser sign-in modal: the Shell emits it after every
   * other z-[110] surface but that one, so it sits over the Settings that
   * opened it, and over the recovery card too.
   * Then a dialog the page on screen raised, which is drawn over the flyouts
   * and the routed overlays.
   * Next the notification feed, which can also open over the recovery card.
   * The card comes before the route-based overlays because it is not one,
   * and it is never raised over one, so this never has to choose between
   * them. The route-based closer gates on the live route, so a request popup
   * floating over Settings closes alone (the route is the popup's) and
   * leaves Settings standing.
   */
  handleEscape(): boolean {
    if (this.errorDialogs.current !== null) {
      this.errorDialogs.dismiss();
      return true;
    }
    if (this.isWorkspaceSwitcherOpen) {
      this.closeWorkspaceSwitcher();
      return true;
    }
    if (this.isAccountSwitcherOpen) {
      this.closeAccountSwitcher();
      return true;
    }
    if (this.clientPermissionPrompt.isOpen) {
      this.clientPermissionPrompt.dismiss();
      return true;
    }
    if (this.webLogin.isOpen) {
      this.webLogin.dismiss();
      return true;
    }
    // A dialog raised by whatever is on screen (a confirmation inside Settings, a page's own): it is drawn over
    // the flyouts and the routed overlays, so it goes first and they stay.
    if (dismissTopmostModal()) return true;
    if (this.isNotificationsOpen) {
      this.closeNotifications();
      return true;
    }
    return this.closeOpenRecoveryModal() || this.dismissAppOverlay();
  }

  /**
   * Open a workspace's Settings -> Updates tab, the one place its update is
   * explained and acted on. The tab flashes its status block once on arrival.
   */
  openUpdatesTab(
    anyId: string,
    options: { failure?: UpdateActionResult } = {},
  ): void {
    const agentId = this.stores.workspaces.toAgentScopedId(anyId);
    this.pendingUpdatesTabArrival = {
      agentId,
      serial: ++this.updatesTabArrivalCount,
      failure: options.failure ?? null,
    };
    this.openSettings({ tab: "workspace", group: "updates", settings_workspace: agentId });
  }

  /** Take the pending Updates-tab request if it is for this machine, so it is acted on once. */
  takeUpdatesTabArrival(agentId: string): UpdatesTabArrival | null {
    const arrival = this.pendingUpdatesTabArrival;
    if (arrival === null || arrival.agentId !== agentId) return null;
    this.pendingUpdatesTabArrival = null;
    return arrival;
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
    const isSameSearch = search === this.lastHandledRouteSearch;
    this.lastHandledRoutePath = path;
    this.lastHandledRouteSearch = search;
    // The window's account follows the route when the route changes, and is
    // asked of it again while it has not said whose it is. A redraw of an
    // unchanged route leaves the account alone: an account adopted without a
    // navigation (adoptAccount) would otherwise be undone by the next draw,
    // since the start flow's route is the device's and a hub route rewritten
    // in place still reads as the old account from m.route.get().
    if (!isSameRoute || this.isRouteAccountUnknown) {
      const routeAccountId =
        this.accountIdForRoute(path, search) ??
        this.routeAccountInHandFor(path);
      this.isRouteAccountUnknown = routeAccountId === null;
      if (routeAccountId !== null) this.accountId = routeAccountId;
      // A route naming a workspace the list does not carry is asked of the
      // backend, once per navigation; a redraw keeps asking the list.
      else if (!isSameRoute && ShellState.routeSubjectIdFromPath(path) !== null)
        void this.askRouteAccount(path, search);
    }
    if (!isSameRoute) this.routeAccountInHand = null;
    // The switcher closes like any popover when the surface under it changes,
    // except across the one navigation that opened it from a raised strip.
    if (!isSameRoute) {
      if (this.isAccountSwitcherArmed) this.isAccountSwitcherArmed = false;
      else this.isAccountSwitcherOpen = false;
      if (this.isWorkspaceSwitcherArmed) this.isWorkspaceSwitcherArmed = false;
      else this.isWorkspaceSwitcherOpen = false;
    }
    // The dismissal navigation has landed; clear the closeAppOverlay guard.
    // Gated on an actual navigation, like the panel below: history.back() does
    // not land synchronously, so a redraw on the route still being left would
    // otherwise drop the guard before the dismissal it guards -- and a held
    // Escape (repeating every ~30ms) would fire a second back(), carrying the
    // reader past the surface the popup was opened over. The query counts as
    // well as the path: a step back from one Settings entry onto an earlier
    // one (a link from outside the page pushed the second) lands on the same
    // path, and a guard left set there would swallow every later dismissal.
    if (!isSameRoute || !isSameSearch) this.isAppOverlayClosing = false;
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
    // The Settings underneath belongs to the request popup opened over it, so
    // it lives exactly as long as /inbox is the route: navigating to ANY other
    // route leaves it behind.
    if (!isSameRoute && path !== "/inbox") this.settingsRouteBehindOverlay = null;
    // The page underneath belongs to the modal that was opened over it; once
    // the route is no longer a modal's, it is (or is not) the route.
    if (!isSameRoute && !isAppOverlayPath(path))
      this.pageRouteBehindOverlay = null;
    // Pass the query so an app modal opened over a workspace (/help?workspace=)
    // keeps that workspace's accent painting behind it.
    const accentSource = accentSourceForRoute(path, search);
    // A floating route keeps the workspace surface mounted behind it, so it
    // still counts as displaying that workspace -- which is what addresses a
    // verdict to the machine that asked while its request popup is the
    // current route.
    this.displayedWorkspaceAnyId =
      workspaceDisplayIdFromPath(path) ??
      popoutFromPath(path)?.workspaceAnyId ??
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
    this.consumeChatParam(path, search);
    this.channel?.setClientState(path, agentScoped, this.accountId);
  }

  /** Consume a ``?chat=<chat-agent-id>`` param on the workspace surface --
   * the landing half of an agent message's click (a feed row, a toast, the
   * OS banner's deep link). Consumed once per landing like ``?review=``: the
   * param is stripped with a history-replacing route set, and the workspace
   * is asked to show the chat once its frame is up. */
  private consumeChatParam(path: string, search: string): void {
    const chatAgentId = new URLSearchParams(search).get("chat");
    if (chatAgentId === null || chatAgentId === "") {
      this.consumedChatKey = null;
      return;
    }
    const workspaceAnyId = workspaceDisplayIdFromPath(path);
    if (workspaceAnyId === null) return;
    const key = `${path}?chat=${chatAgentId}`;
    if (this.consumedChatKey === key) return;
    this.consumedChatKey = key;
    const agentScoped = this.stores.workspaces.toAgentScopedId(workspaceAnyId);
    // Deferred past this render for the same reason as consumeReviewParam.
    queueMicrotask(() => {
      m.route.set(`/workspace/${agentScoped}`, undefined, { replace: true });
      this.requestFocusChat(agentScoped, chatAgentId);
    });
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
   * A resolved or unknown id leaves the workspace on screen -- the hop
   * itself is still the right landing for a stale click -- with a line saying so.
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
      } else {
        // Answered, withdrawn or no longer listed: the click still lands on
        // the workspace, and says why nothing opened over it.
        this.notificationsUi?.showTransientToast("That request is no longer waiting for you.", "");
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
   * with the same ``workspaceDisplayIdFromPath`` the router computes the
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
   * Raise the card on the edge into recovery_failed for the displayed machine,
   * and drop an auto-raised one on the edge back into healthy.
   *
   * recovery_failed means the app tried to bring the machine back -- unattended,
   * that is a plain start, not a bounce -- and it is still unresponsive. That is
   * the end of the unattended path, and a one-line band is too quiet for it;
   * every other condition leaves the band as the sole surface and waits to be
   * asked.
   *
   * Only an edge raises it -- ``isSnapshotFrame`` marks the connect-time replay
   * of current state -- so a window that opens onto a machine already in this
   * state gets the band, whose "Open recovery" is one click away.
   *
   * Nothing raises itself while the discovery consumer is dead: every machine
   * reads unhealthy then, and the card's actions all route through the forward
   * that consumer feeds, so it would offer "Restart workspace" over a band
   * correctly saying only restarting Imbue Studio can help.
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
    // Mid-apply the services are down because an update took them down;
    // raising the recovery card would blame the machine for it. Only the raise
    // is withheld: health frames are edge-published, so a card already up must
    // still come down on the healthy frame. A machine that dies mid-prepare
    // gets the normal card.
    if (this.stores.updates.isApplying(agentId)) return;
    if (isSnapshotFrame || health !== "recovery_failed") return;
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

  openWorkspaceSwitcher(): void {
    this.isAccountSwitcherOpen = false;
    this.isWorkspaceSwitcherOpen = true;
  }

  closeWorkspaceSwitcher(): void {
    this.isWorkspaceSwitcherOpen = false;
  }

  /**
   * The switcher's shortcut: open the workspace menu, or put it away. Nothing
   * happens in the start flow, which has no bar to hang it from, nor under a
   * modal that is not one of the bar's flyouts, which holds the window until
   * it is answered.
   */
  toggleWorkspaceSwitcher(): void {
    if (this.isWorkspaceSwitcherOpen) {
      this.closeWorkspaceSwitcher();
      m.redraw();
      return;
    }
    const path = this.currentRoutePath();
    if (classifyRoute(path, this.currentRouteSearch()).kind === "start") return;
    if (popoutFromPath(path) !== null) return;
    const isModalUp =
      this.errorDialogs.current !== null ||
      this.clientPermissionPrompt.isOpen ||
      this.webLogin.isOpen ||
      this.openRecovery !== null ||
      this.isShareNotYetCreatedDialogOpen ||
      // A centered card over the window: the request popup, the AI-keys dialog, the template stepper.
      ((isAppOverlayPath(path) || overlayBehindWorkspaceId(path, this.currentRouteSearch()) !== null) &&
        !isTitlebarPopupRoutePath(path)) ||
      isAnyModalOpen();
    if (isModalUp) return;
    this.switchToWorkspaceSwitcher();
    m.redraw();
  }

  /** What the workspace menu's search field shows for the shortcut that opens it; null when it has none to show
   * (turned off, or not yet read). */
  async workspaceSwitcherShortcutLabel(): Promise<string | null> {
    return electronBridge.workspaceSwitcherShortcutLabel();
  }

  /**
   * Open Settings over what the window shows. `params` say where in it (see
   * SettingsPage); the workspace on screen rides along as `?workspace=`, so
   * Settings floats over it (kept mounted) and speaks for it by default.
   *
   * Arrived at from another routed flyout, this is a switch, not a stack: that
   * flyout's history entry is replaced rather than built on.
   */
  openSettings(params: Record<string, string> = {}): void {
    const path = this.currentRoutePath();
    this.isAccountSwitcherOpen = false;
    this.isWorkspaceSwitcherOpen = false;
    // Opened over another floating route, the page behind is already the one that route remembered.
    if (!isAppOverlayPath(path)) this.rememberPageBehindOverlay();
    const agentScoped = this.displayedWorkspaceAgentId();
    m.route.set(
      SETTINGS_PATH,
      this.overlayQuery(agentScoped === null ? params : { ...params, workspace: agentScoped }),
      isTitlebarPopupRoutePath(path) ? { replace: true } : undefined,
    );
  }

  /** The workspace the bar is about: the one on screen, else the one a workspace page (its backups, its recovery)
   * names; null on a hub page. */
  private barWorkspaceAgentId(): string | null {
    const displayed = this.displayedWorkspaceAgentId();
    if (displayed !== null) return displayed;
    const context = classifyRoute(this.currentRoutePath(), this.currentRouteSearch());
    return context.kind === "workspace" && context.workspaceAnyId !== null
      ? this.stores.workspaces.toAgentScopedId(context.workspaceAnyId)
      : null;
  }

  /** The gear: Settings on the workspace the bar is about, at its Permissions while its agents wait on an answer. */
  openSettingsFromBar(): void {
    const agentId = this.barWorkspaceAgentId();
    if (agentId === null) {
      this.openSettings();
      return;
    }
    const params: Record<string, string> = { tab: "workspace", settings_workspace: agentId };
    if (this.stores.requests.hasPendingForWorkspace(agentId)) params.group = "permissions";
    this.openSettings(params);
  }

  /**
   * The bar's Share: Settings at Sharing, on the whole workspace the bar is
   * about. With Settings already there it puts Settings away, as a second
   * press of any flyout's button does. On a creation page the workspace does
   * not exist yet, and a dialog says so.
   */
  pressShare(): void {
    const path = this.currentRoutePath();
    const search = this.currentRouteSearch();
    this.isAccountSwitcherOpen = false;
    this.isWorkspaceSwitcherOpen = false;
    if (classifyRoute(path, search).kind === "creating") {
      this.isShareNotYetCreatedDialogOpen = true;
      return;
    }
    const agentId = this.barWorkspaceAgentId();
    if (agentId === null) return;
    if (path === SETTINGS_PATH) {
      const params = new URLSearchParams(search);
      const isOnWholeWorkspaceSharing =
        params.get("tab") === "workspace" &&
        params.get("group") === "sharing" &&
        (params.get("settings_workspace") ?? params.get("workspace")) === agentId &&
        !params.get("share_target");
      if (isOnWholeWorkspaceSharing) {
        this.dismissAppOverlay();
        return;
      }
    }
    this.openSettings({ tab: "workspace", group: "sharing", settings_workspace: agentId });
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
   * nothing to put away and nothing to arm. A flyout's entry is REPLACED,
   * like the other strip switches, so the flyout is not one Back away under
   * the feed.
   */
  switchToNotifications(): void {
    this.isAccountSwitcherOpen = false;
    this.isWorkspaceSwitcherOpen = false;
    this.isNotificationsArmed = this.dismissAppOverlay();
    this.openNotifications();
  }

  /**
   * Put a routed flyout (Settings, Get help) away by routing straight to the
   * machine it names, rather than back through history, reporting whether
   * that applied.
   *
   * The entry `history.back()` returns to is not reliably the machine: it can
   * be a request popup that has since been answered, or another flyout's
   * entry. Back onto one of those is a dismissal that never visibly lands, so
   * Escape stays inert behind `isAppOverlayClosing`. `replace` so the machine
   * is not left one Back away from the flyout again.
   *
   * Only with a machine named and no remembered page: the recovery page's
   * flyouts forward the very machine nobody could load (see
   * `pageRouteBehindOverlay`), and with no `?workspace=` at all history is the
   * only thing that knows where this came from.
   */
  private dismissFlyoutToItsMachine(): boolean {
    const path = this.currentRoutePath();
    if (!isTitlebarPopupRoutePath(path) || this.pageRouteBehindOverlay !== null) return false;
    const behind = overlayBehindWorkspaceId(path, this.currentRouteSearch());
    if (behind === null) return false;
    m.route.set(`/workspace/${behind}`, undefined, { replace: true });
    return true;
  }

  /**
   * Open Get help / report a bug, carrying the displayed workspace so the page
   * can offer the in-workspace /assist flow -- only when that workspace's
   * interface is healthy. The one place the bug button's route is built, so
   * the titlebar's own button and its raised copy open the identical page.
   *
   * Arrived at from another flyout's route (Settings, via the raised strip),
   * this is a switch, not a stack: that flyout's history entry is replaced
   * rather than built on. (The Settings a request popup was remembering is let
   * go by handleRouteChanged when the /help route lands -- it lives exactly as
   * long as /inbox does.)
   */
  openHelp(): void {
    this.isAccountSwitcherOpen = false;
    this.isWorkspaceSwitcherOpen = false;
    const isSwitching = isTitlebarPopupRoutePath(this.currentRoutePath());
    const routeOptions = isSwitching ? { replace: true } : undefined;
    const agentScoped = this.displayedWorkspaceAgentId();
    if (agentScoped === null) {
      m.route.set("/help", this.overlayQuery(), routeOptions);
      return;
    }
    m.route.set(
      "/help",
      this.overlayQuery(this.scopedHelpQuery(agentScoped)),
      routeOptions,
    );
  }

  /** The /help query naming a workspace: assist is offered only while its
   * interface is healthy, and never for a shared workspace, which is still
   * named (it keeps its frame mounted behind the modal) but has no agent of
   * the account's to ask. */
  private scopedHelpQuery(workspaceAgentId: string): {
    workspace: string;
    assist: "1" | "0";
  } {
    const entry = this.stores.workspaces.entryByAnyId(workspaceAgentId);
    const isAssistOffered =
      (entry === null || !isSharedEntry(entry)) &&
      this.stores.health.isContentAssumedReady(workspaceAgentId);
    return {
      workspace: workspaceAgentId,
      assist: isAssistOffered ? "1" : "0",
    };
  }

  /**
   * Open the report form for an error the user was just shown, with the
   * error's sentence and details attached.
   *
   * What stays behind the form and what the report is about are separate.
   * The surface behind is always the one on screen: the displayed workspace
   * (the route names it, keeping its frame mounted), or the recovery page kept
   * painted, or a hub page when neither. The report collects from
   * `workspaceAgentId` when the error names a workspace, else from the one on
   * screen. Help with an agent is offered only when the two are the same
   * workspace, so a helper never starts in a workspace the error is not about.
   */
  openHelpForError(
    error: UserFacingError,
    workspaceAgentId: string | null,
  ): void {
    const path = this.currentRoutePath();
    // Raised over an app modal, the page that modal floats over is still the
    // one behind, and the live route is only the modal's.
    if (!isAppOverlayPath(path)) this.rememberPageBehindOverlay();
    const recoveryId = recoveryWorkspaceIdFromPath(
      (this.pageRouteBehindOverlay ?? path).split("?")[0],
    );
    const behindAgentId =
      this.displayedWorkspaceAgentId() ??
      (recoveryId !== null
        ? this.stores.workspaces.toAgentScopedId(recoveryId)
        : null);
    const reportAgentId = workspaceAgentId ?? behindAgentId;
    const isReportAboutBehind = reportAgentId === behindAgentId;
    setPendingHelpLaunch({
      workspaceAgentId: behindAgentId ?? "",
      errorDetails: bugReportTextFor(error),
      errorWorkspaceAgentId: isReportAboutBehind ? "" : (reportAgentId ?? ""),
    });
    m.route.set(
      "/help",
      this.overlayQuery(
        behindAgentId === null
          ? {}
          : {
              ...this.scopedHelpQuery(behindAgentId),
              ...(isReportAboutBehind ? {} : { assist: "0" }),
            },
      ),
      isTitlebarPopupRoutePath(path) ? { replace: true } : undefined,
    );
  }

  /**
   * Open the report form pre-filled with an in-workspace agent's diagnosis,
   * floating over the machine that asked.
   *
   * The diagnosis is staged rather than routed (a report body is far too large
   * for a URL), but the machine is NAMED in the route exactly as `openHelp`
   * names it: `?workspace=` is the only thing that keeps that machine's surface
   * mounted behind the modal. Routed without it, a report ABOUT a machine opens
   * over Home with that machine's frame torn down behind it.
   *
   * Assist is not offered: the description is already written, so the form is
   * the only mode this launch can land in.
   */
  openHelpForAgentAsk(workspaceAgentId: string, description: string): void {
    // A report still staged means the previous ask's /help has been pushed but
    // has not landed (m.route.get() trails route.set), so the route read below
    // is stale. This ask takes that entry rather than stacking a second /help.
    const isHelpPending = stagedHelpLaunchWorkspace() !== null;
    setPendingHelpLaunch({
      workspaceAgentId,
      description,
      isAgentReport: true,
      workspaceName:
        this.stores.workspaces.accentEntry(workspaceAgentId)?.name ?? "",
    });
    // Same switch-not-stack rule as `openHelp`: raised over another flyout,
    // this takes that flyout's entry rather than leaving it one Back away
    // underneath.
    const isSwitching =
      isHelpPending || isTitlebarPopupRoutePath(this.currentRoutePath());
    m.route.set(
      "/help",
      this.overlayQuery(
        workspaceAgentId ? { workspace: workspaceAgentId } : {},
      ),
      isSwitching ? { replace: true } : undefined,
    );
  }
}
