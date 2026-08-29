// The persistent app shell: titlebar + switcher popover + the routed page
// body. Body modes, matching ChromeShell.jinja:
//
// - Local page: content scrolls inside #local-page-scroll, the inset white
//   card below the fixed titlebar (accent bleeds around it).
// - Agent content surface (/workspace/<id>): the page IS the fixed iframe
//   surface; no scroll container. The options route (/workspace/<id>/options)
//   keeps the frame mounted and renders the routed content as its own overlay
//   layer over it -- a docked panel hanging from the titlebar's icon-tabs (the
//   SPA heir of the legacy docked WorkspaceOptionsModal), which self-chromes,
//   so the Shell just floats it over the still-mounted surface.
// - App-level modals (/settings, /accounts, /help, /inbox): the routed content
//   floats in a centered AppOverlay card over the surface it was opened from --
//   the live workspace (Get help and the request popup forward ?workspace=) or
//   Home -- which stays mounted behind the dim backdrop, restoring the legacy
//   overlay-layer modals. The options panel, when a modal was opened over it,
//   stays mounted too: it holds the same vtree slot either way.

import m from "mithril";
import type { ShellState } from "./shell-state";
import {
  isAppOverlayPath,
  isWorkspaceOverlayPath,
  overlayBehindWorkspaceId,
} from "./classify";
import { noticeBandFor } from "./notice-band";
import { standingUpdateNotice, updateRunOutcome, updateRunPhase } from "../../models/updates";
import { NoticeBand } from "./NoticeBand";
import { NotificationsPage } from "../pages/NotificationsPage";
import type { OverlayShellAttrs } from "./OverlayShell";
import { ANCHORED_CARD_CLASS, OverlayShell } from "./OverlayShell";
import { openReviewRoute } from "../../models/notificationsUi";
import { SidebarMenu } from "./SidebarMenu";
import { Titlebar } from "./Titlebar";
import { ToastLayer } from "./ToastLayer";
import { WorkspaceFrame } from "./WorkspaceFrame";
import { LocalPageNotice } from "./LocalPageNotice";
import {
  dismissUpdateReady,
  updateReadyVersion,
  watchUpdateStatus,
} from "./update-ready";
import { UpdateReadyCard } from "./UpdateReadyCard";
import { RecoveryModal } from "../recovery/RecoveryModal";
import { UpdateApplyModal } from "../components/UpdateApplyModal";
import { UpdateModal } from "../components/UpdateModal";
import { WebLoginModal } from "../components/WebLoginModal";
import { electronBridge } from "../../electron-bridge";

/** The request popup's overlay attrs: the docked options panel's own window,
 * showing a request -- same rect, same raised strip, same key tab filled and
 * joined to the card. `animatesBox` is what sets it apart: the card grows out
 * of the panel it was opened over and resizes as the request loads (the
 * lifecycle lives in OverlayShell). */
function requestOverlayAttrs(
  shell: ShellState,
  bodyClass: string,
): OverlayShellAttrs {
  return {
    shell,
    placement: "docked",
    // Reviewing a permission request is a Permissions surface, so the popup
    // hangs from the same key the docked Permissions pane does.
    selected: "permissions",
    animatesBox: true,
    panelId: "app-overlay-panel",
    backdropId: "app-overlay-backdrop",
    cardClass: "w-[600px] min-h-0 max-w-full",
    // Laid out at the width the card settles at, not at the card's animating
    // width: left to fill, every frame of the resize would re-wrap the
    // request's text and re-flow its rows, which is what a smooth box change
    // must not do. The card clips, so the wider frame simply shows more
    // surface around it. Width only -- the card is a COLUMN flex container,
    // so `shrink-0` here would refuse to shrink in HEIGHT, and the body's own
    // overflow scroller would never engage.
    bodyClass: bodyClass + " w-[600px] max-w-full",
    onDismiss: () => shell.dismissAppOverlay(),
  };
}

/** The two surfaces hung from the titlebar's right-hand pair: the bell's feed
 * and the bug button's Get help form. One box, one placement, one dismissal
 * shape -- they differ only in which icon is lit, which panel id they carry,
 * and how they are put away (the feed is local state, Get help is a route).
 * Sharing this is what keeps the window from moving when you click from one
 * icon to the other. */
function anchoredOverlayAttrs(
  shell: ShellState,
  id: "notifications" | "help",
): OverlayShellAttrs {
  const isFeed = id === "notifications";
  return {
    shell,
    placement: "anchored",
    selected: id,
    panelId: isFeed ? "notifications-panel" : "help-panel",
    backdropId: isFeed ? "notifications-backdrop" : "help-backdrop",
    // One width, so the box does not change when you click the other icon.
    // The height is the one dimension the two genuinely differ on: a viewport
    // fraction for the feed's row list, a fixed window offset for the help
    // form, whose card must not outgrow the window it hangs in.
    cardClass:
      ANCHORED_CARD_CLASS +
      (isFeed ? " max-h-[70vh]" : " max-h-[calc(100%-64px)]"),
    // One body shape too: both pages draw their own edge-to-edge title row
    // (icon + label over a hairline) and scroll their content below it, so the
    // window keeps one header line across a switch.
    bodyClass: "flex-1 min-h-0 flex flex-col",
    onDismiss: isFeed
      ? () => shell.closeNotifications()
      : () => shell.dismissAppOverlay(),
  };
}

/** Per-route sizing for a CENTERED app modal. Minds settings takes a definite
 * height -- its two columns scroll within it, and a card that resized itself
 * per section would move the section list out from under the cursor -- capped
 * to the window by the same min() the others' max uses. Accounts is a short
 * list and the AI-keys mint dialog a compact form, so those grow to their
 * content. The placed surfaces (the request popup, the feed, Get help) name
 * their own box where they are rendered. */
function appOverlayCardClass(path: string): string {
  if (path === "/settings") return "w-[880px] h-[min(660px,calc(100%-64px))]";
  if (path === "/accounts") return "w-[520px] min-h-0";
  return "w-[460px] min-h-0";
}

/** How the card holds its body. Minds settings is a two-column pane that
 * scrolls its own columns -- a scroller here would take its section list down
 * with the panel -- so it gets a height-bounded column instead, the same shape
 * the docked options card gives its panes. Every other overlay is a single
 * column of prose or fields and scrolls as a whole, which is the default a new
 * route falls through to. */
function appOverlayBodyClass(path: string): string {
  if (path === "/settings") return "flex-1 min-h-0 flex flex-col px-6 py-5";
  return "min-h-0 overflow-y-auto px-6 py-5";
}

export interface ShellAttrs {
  shell: ShellState;
  routePath: string;
  workspaceParam: string | null;
  content: m.Children;
  // The Home surface to paint behind an app-level modal opened over Home. The
  // router owns route->page identity, so it names this (keeping the Shell
  // page-agnostic -- it only knows there is a base to render).
  homeContent: m.Children;
  // The hub page to keep painted behind an app-level modal, for a page that
  // outranks both of the Shell's usual backdrops (Home, and the workspace
  // ?workspace= names); null when no modal floats over such a page. The router
  // owns which pages those are.
  behindContent: m.Children;
  // The workspace-options panel to keep painted behind an app-level modal that
  // was opened over it; null when no modal floats over the panel. Same page as
  // the routed `content` on the options route, so the slot below holds one
  // component instance across the modal opening and closing.
  optionsContent: m.Children;
}

export function Shell(): m.Component<ShellAttrs> {
  // Escape is handled by the app's single in-document listener (index.ts),
  // which routes through ShellState.handleEscape -- not here, so the Shell
  // never competes with it for the same keypress.
  return {
    oncreate() {
      watchUpdateStatus(() => m.redraw());
    },
    view(vnode) {
      const {
        shell,
        routePath,
        workspaceParam,
        content,
        homeContent,
        behindContent,
        optionsContent,
      } = vnode.attrs;
      // The visual-diff harness captures with ?visual-diff=1 and no live
      // channel; suppress the indicator so screenshots stay deterministic.
      const isCaptureMode = new URLSearchParams(window.location.search).has(
        "visual-diff",
      );
      const isReconnecting =
        (shell.channel?.isVisiblyReconnecting ?? false) && !isCaptureMode;
      const updateReady = updateReadyVersion();

      const routeSearch = shell.currentRouteSearch();
      const isAppOverlay = isAppOverlayPath(routePath);
      // The New machine template stepper floats as a modal only when it is
      // over a machine (?workspace=); opened with none it redirects to the full
      // create form, so it is not an overlay then.
      const isTemplateRoute = routePath === "/create/template";
      // The workspace surface kept mounted underneath: the agent surface itself,
      // or the workspace a Get help / request popup / template modal was
      // opened over. Rendering it at a stable vtree position keeps its iframe
      // from reloading across open/close.
      const behindWorkspaceId =
        isAppOverlay || isTemplateRoute
          ? overlayBehindWorkspaceId(routePath, routeSearch)
          : null;
      const isTemplateModal = isTemplateRoute && behindWorkspaceId !== null;
      // A page kept behind the modal IS the surface, so the workspace the modal
      // forwards does not become one: mounting that machine's frame is exactly
      // what the remembered page is there to prevent.
      const isPageKeptBehind =
        isAppOverlay && behindContent !== null && behindContent !== undefined;
      const surfaceWorkspaceId =
        workspaceParam ?? (isPageKeptBehind ? null : behindWorkspaceId);
      const localScrollClass =
        "bg-surface-primary overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable]";

      const base =
        surfaceWorkspaceId !== null
          ? m(WorkspaceFrame, { shell, workspaceAnyId: surfaceWorkspaceId })
          : m(
              "div#local-page-scroll",
              { class: localScrollClass },
              // LocalPageNotice sits here rather than in each page: the
              // takeover it replaced covered every window, so leaving pages to
              // opt in would silently drop the condition on the ones that
              // forget. Pages that want it inline place their own.
              // An app modal keeps its opener painted behind its backdrop --
              // the page the router remembered, else Home; otherwise the routed
              // page is the surface itself.
              [
                m(LocalPageNotice),
                isAppOverlay
                  ? isPageKeptBehind
                    ? behindContent
                    : homeContent
                  : content,
              ],
            );

      // The options panel is its own docked overlay layer (backdrop + tab strip
      // + card): the Shell just floats it over the mounted surface, from the
      // route while it IS the route and from the router's remembered copy while
      // a modal floats over it. Not while the feed is open, though: the feed
      // only ever opens over the LIVE panel route in the beat between a strip
      // switch and its dismissal navigation landing, and rendering the panel
      // under it for that beat would double the backdrop -- the flash the one
      // overlay slot below exists to prevent.
      const optionsLayer =
        isWorkspaceOverlayPath(routePath) && !shell.isNotificationsOpen
          ? content
          : optionsContent;
      // The request popup does not float OVER the options panel, it takes that
      // window over: it hangs from the same key and resizes out of the panel's
      // box. So the panel is hidden while it is up -- left visible, the two
      // cards overlap almost exactly for the length of the resize, each with
      // its own close X, which is what made one window read as two. Hidden
      // rather than unmounted: the panel keeps its scroll, its section and its
      // in-flight edits for the return trip, and `visibility` (unlike
      // `display`) leaves it laid out, so the popup can still measure the box
      // it is resizing out of.
      const isPanelTakenOver = routePath === "/inbox" && optionsLayer !== null;

      // THE ONE OVERLAY SLOT: whichever of the Shell's floating surfaces is up
      // -- the bell's feed, the request popup, Get help, a centered app modal,
      // the template stepper -- renders as ONE OverlayShell at this one vtree
      // position. One position means one component instance across a strip
      // switch, so the backdrop, the raised strip, and the card are the SAME
      // DOM nodes on both sides of it: switching from Get help to the feed
      // swaps the card's contents in place instead of tearing one overlay
      // down and mounting another, which flashed a doubled (or missing)
      // backdrop for the frames in between.
      //
      // The feed leads. It is local state, not a route, and it is OPEN during
      // the beat between a strip switch and the dismissal navigation landing
      // (switchToNotifications arms it across exactly that arrival) -- so
      // while both the feed and a popup route are up, the feed is the surface
      // being switched TO, and the one the slot must show. (The options panel
      // is the one floating surface with a slot of its own, above: it must
      // stay mounted -- models, scroll, edits -- behind a request popup that
      // takes its window over, which means panel and popup genuinely coexist.)
      let overlayAttrs: OverlayShellAttrs | null = null;
      let overlayContent: m.Children = null;
      if (shell.isNotificationsOpen) {
        overlayAttrs = anchoredOverlayAttrs(shell, "notifications");
        overlayContent = m(NotificationsPage);
      } else if (isAppOverlay) {
        const bodyClass = appOverlayBodyClass(routePath);
        // The request popup hangs from the titlebar's key tab, Get help hangs
        // from the right-hand icon pair; every other app modal is a centered
        // card.
        overlayAttrs =
          routePath === "/inbox"
            ? requestOverlayAttrs(shell, bodyClass)
            : routePath === "/help"
              ? anchoredOverlayAttrs(shell, "help")
              : {
                  shell,
                  placement: "centered",
                  selected: null,
                  panelId: "app-overlay-panel",
                  backdropId: "app-overlay-backdrop",
                  cardClass: appOverlayCardClass(routePath),
                  bodyClass,
                  onDismiss: () => shell.dismissAppOverlay(),
                };
        overlayContent = content;
      } else if (isTemplateModal) {
        // The New machine template stepper over a live machine: a centered
        // card, dismissed back to that machine (closeAppOverlay handles it).
        overlayAttrs = {
          shell,
          placement: "centered",
          selected: null,
          panelId: "app-overlay-panel",
          backdropId: "app-overlay-backdrop",
          cardClass: "w-[600px] min-h-0 max-w-full",
          bodyClass: appOverlayBodyClass(routePath),
          onDismiss: () => shell.dismissAppOverlay(),
        };
        overlayContent = content;
      }
      const overlay =
        overlayAttrs === null ? null : m(OverlayShell, overlayAttrs, overlayContent);

      // The band speaks for whichever machine is painted, so it is keyed to the
      // surface rather than the route: the workspace route's own frame, and
      // equally the machine an app-level modal was opened over, which stays
      // mounted behind the backdrop. A hub page with no machine behind it
      // reports these conditions in its own flow instead.
      //
      // Not gated on capture mode, unlike the reconnecting indicator above:
      // that one reads live channel liveness, which no capture has, while
      // these read health the fixture bootstrap sets deterministically. The
      // harness is how these surfaces get looked at.
      const agentScoped =
        surfaceWorkspaceId === null
          ? null
          : shell.stores.workspaces.toAgentScopedId(surfaceWorkspaceId);
      const health =
        agentScoped === null
          ? "healthy"
          : shell.stores.health.statusFor(agentScoped);
      // A machine the recovery verdict reads as unreachable through its own
      // backend is unreachable for a reason the band can state, which is why it
      // reads unhealthy at all. The list frame already carries the verdict and
      // the backend's name per row, so the band can name it without a poll of
      // its own -- and says the same thing the card behind "Open recovery" will.
      const entry =
        agentScoped === null
          ? null
          : shell.stores.workspaces.entryByAnyId(agentScoped);
      const unreachableProviderLabel = entry?.is_backend_unreachable
        ? entry.provider_label || null
        : null;
      const standingNotice =
        agentScoped === null
          ? "none"
          : standingUpdateNotice(shell.stores.updates.forAgent(agentScoped), shell.stores.updates.isUpdating(agentScoped));
      // The band is where a run reports itself to the reader inside the
      // machine, who cannot see the row badge.
      const published = agentScoped === null ? null : shell.stores.updates.publishedFor(agentScoped);
      const updatePhase =
        agentScoped === null ? "none" : updateRunPhase(published, shell.stores.updates.isUpdating(agentScoped));
      const updateOutcome = updateRunOutcome(published);
      const band = noticeBandFor(
        health,
        shell.stores.health.discoveryHealth,
        surfaceWorkspaceId !== null,
        {
          isRestartAppAvailable: electronBridge.isDesktop,
          unreachableProviderLabel,
          deviceEnvironment: shell.stores.health.appEnvironmentCondition(),
          // So the band can tell the user's own bounce, which narrates itself,
          // from the app's unattended start, which must not hide the device.
          recoveryKind: agentScoped === null ? null : shell.stores.health.recoveryKindFor(agentScoped),
          // Which scopes that app-global condition to the machines it can
          // explain: one on an on-device backend answers over loopback with the
          // wifi off. A row we have no entry for keeps the conservative default.
          isWorkspaceNetworkDependent: entry?.is_network_dependent ?? true,
          isDeviceCannotConnect: entry?.is_device_cannot_connect ?? false,
          updateRunPhase: updatePhase,
          updateHoldDetail: published?.is_hold_recorded ? (published.hold_detail ?? "") : null,
          updateRunOutcome: updateOutcome,
          standingUpdateNotice: standingNotice,
        },
      );
      // The card is a modal of its own, so it is raised only where it can sit
      // on top: the machine's own route. It out-z-indexes the docked options
      // overlay there, but an app-level modal shares its z and is emitted after
      // it, so a card raised behind one would be dimmed and unclickable. A
      // machine behind an app modal keeps its band and gets its card back on
      // the way out.
      const isRecoveryOpen =
        workspaceParam !== null &&
        agentScoped !== null &&
        shell.isRecoveryModalOpenFor(agentScoped);
      // Not gated on a displayed machine: the machines list opens this for a
      // row while Home is the surface.
      const updateModalAgentId = shell.openUpdateModalAgentId();
      // Covers only the machine the apply is landing in; up exactly while the
      // run is applying.
      const isApplyCovering =
        workspaceParam !== null &&
        agentScoped !== null &&
        shell.stores.updates.isApplying(agentScoped);

      return m("div", { style: "display: contents" }, [
        m(Titlebar, { shell, routePath }),
        m(SidebarMenu, { shell }),
        band !== null
          ? m(NoticeBand, {
              shell,
              payload: band,
              onAction: () => {
                if (band.action?.kind === "restart-app") {
                  electronBridge.restartApp();
                } else if (agentScoped === null) {
                  return;
                } else if (band.action?.kind === "update-workspace") {
                  shell.openUpdateModal(agentScoped);
                } else {
                  shell.openRecoveryModal(agentScoped);
                }
              },
            })
          : null,
        isRecoveryOpen && workspaceParam !== null && agentScoped !== null
          ? m(RecoveryModal, {
              workspaceAnyId: workspaceParam,
              isAutoRaised: shell.isRecoveryModalAutoRaised(agentScoped),
              onClose: () => shell.closeRecoveryModal(),
            })
          : null,
        isApplyCovering && workspaceParam !== null
          ? m(UpdateApplyModal, {
              workspaceName:
                shell.stores.workspaces.entryByAnyId(workspaceParam)?.name ??
                "this machine",
            })
          : null,
        updateModalAgentId !== null
          ? m(UpdateModal, {
              agentId: updateModalAgentId,
              workspaceName:
                shell.stores.workspaces.entryByAnyId(updateModalAgentId)
                  ?.name ?? "this machine",
              onClose: () => shell.closeUpdateModal(),
            })
          : null,
        // The browser sign-in waiting modal: any page (welcome, accounts,
        // create) can trigger it through the shared webLogin model.
        m(WebLoginModal),
        isReconnecting
          ? m(
              "div",
              {
                class:
                  "fixed top-[42px] right-2 z-[150] type-helper text-secondary bg-surface-primary border border-subtle rounded-md px-2 py-1 shadow-raised",
              },
              "Reconnecting…",
            )
          : null,
        // The floating toast stack, below the Reconnecting chip's spot (it
        // starts under the chip whenever the chip is visible).
        shell.notificationsUi !== null
          ? m(ToastLayer, {
              // The bell's feed IS the toasts' durable home: while it is
              // open the floating cards are redundant (opening it also
              // retires them).
              toasts: shell.isNotificationsOpen
                ? []
                : shell.notificationsUi.liveToastEntries(
                    shell.stores.notifications.entries,
                  ),
              isReconnecting,
              onDismiss: (id) => shell.notificationsUi?.dismissToast(id),
              onReview: openReviewRoute,
            })
          : null,
        // Bottom right, out of the way: the update is not urgent, and the app
        // stays fully usable with it on screen.
        updateReady !== null && !isCaptureMode
          ? m(
              "div",
              {
                class:
                  "fixed bottom-4 right-4 z-[150] max-w-[calc(100vw-2rem)]",
              },
              m(UpdateReadyCard, {
                version: updateReady,
                onRestart: () => void electronBridge.installUpdate(),
                onDismiss: () => dismissUpdateReady(),
              }),
            )
          : null,
        m("div#local-page-root", { style: "display: contents" }, [
          base,
          m(
            "div#ws-options-layer",
            {
              // Always present so the panel keeps one vtree slot (and so one
              // component instance) whether or not the popup is over it.
              style: isPanelTakenOver
                ? "display: contents; visibility: hidden"
                : "display: contents",
            },
            optionsLayer,
          ),
          // The one overlay slot (see above). Emitted after RecoveryModal
          // (both share z-[110]; later DOM siblings paint on top) so the feed
          // visually wins when both are open at once, matching handleEscape's
          // coded precedence (notifications closes ahead of the recovery
          // card -- see shell-state.ts).
          overlay,
        ]),
      ]);
    },
  };
}
