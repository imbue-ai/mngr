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
// - App-level modals (/settings, /accounts, /help): the routed content floats
//   in a centered AppOverlay card over the surface it was opened from -- the
//   live workspace (Get help forwards ?workspace=) or Home -- which stays
//   mounted behind the dim backdrop, restoring the legacy overlay-layer modals.

import m from "mithril";
import type { ShellState } from "./shell-state";
import { isAppOverlayPath, isWorkspaceOverlayPath, overlayBehindWorkspaceId } from "./classify";
import { noticeBandFor } from "./notice-band";
import { NoticeBand } from "./NoticeBand";
import { OverlayBackdrop } from "./OverlayBackdrop";
import { SidebarMenu } from "./SidebarMenu";
import { Titlebar } from "./Titlebar";
import { WorkspaceFrame } from "./WorkspaceFrame";
import { LocalPageNotice } from "./LocalPageNotice";
import { RecoveryModal } from "../recovery/RecoveryModal";
import { DialogCloseButton } from "../components/Modal";
import { Icon16 } from "../components/Icon";
import { electronBridge } from "../../electron-bridge";

interface AppOverlayAttrs {
  shell: ShellState;
  cardClass: string;
}

/** The floating card for an app-level modal (Minds settings / Accounts / Get
 * help) in the shared OverlayBackdrop: a centered card with a close X. Esc and
 * backdrop clicks dismiss back to the surface it was opened over. The card
 * clips to its rounded corners and scrolls its body internally so the X stays
 * pinned. */
function AppOverlay(): m.Component<AppOverlayAttrs> {
  return {
    view(vnode) {
      const { shell, cardClass } = vnode.attrs;
      return m(
        OverlayBackdrop,
        { backdropId: "app-overlay-backdrop", fullWindow: true, onDismiss: () => shell.closeAppOverlay() },
        m(
          "div#app-overlay-panel",
          {
            class:
              "relative " +
              cardClass +
              " max-w-full max-h-[calc(100%-64px)] flex flex-col " +
              "rounded-xl border border-subtle bg-surface-primary shadow-overlay overflow-hidden",
          },
          [
            m(DialogCloseButton, { onClose: () => shell.closeAppOverlay() }),
            m("div", { class: "min-h-0 overflow-y-auto px-6 py-5" }, vnode.children),
          ],
        ),
      );
    },
  };
}

/** Per-route sizing for the app modal card. Minds settings gets a tall floor so
 * it never looks cramped -- min() caps that floor at the shared max so it still
 * fits a short window. Accounts is a short list; Get help and the AI-keys mint
 * dialog are compact forms. */
function appOverlayCardClass(path: string): string {
  if (path === "/settings") return "w-[880px] min-h-[min(600px,calc(100%-64px))]";
  if (path === "/accounts") return "w-[520px] min-h-0";
  if (path === "/settings/ai-keys") return "w-[460px] min-h-0";
  return "w-[460px] min-h-0"; // /help
}

interface InboxOverlayAttrs {
  shell: ShellState;
}

/** The Requests inbox drawer: a full-height panel flush to the left edge (not
 * rounded, its right edge bordered), the SPA twin of the legacy inbox overlay
 * drawer. Its own 38px header -- matching the titlebar it covers -- carries the
 * title + close and is draggable so the window still moves; the backdrop dims
 * the strip to the drawer's right, which also carries a window-drag handle. */
function InboxOverlay(): m.Component<InboxOverlayAttrs> {
  return {
    view(vnode) {
      const { shell } = vnode.attrs;
      return m(
        OverlayBackdrop,
        { backdropId: "inbox-backdrop", fullWindow: true, align: "start", onDismiss: () => shell.closeAppOverlay() },
        [
          m(
            "div#inbox-dialog",
            {
              class:
                "relative w-[90vw] lg:w-[75vw] max-w-[1100px] h-full flex flex-col " +
                "bg-surface-primary border-r border-default shadow-overlay overflow-hidden",
            },
            [
              m(
                "div",
                {
                  class: "grid grid-cols-3 items-center px-2 h-[38px] shrink-0 border-b border-default",
                  style: "-webkit-app-region: drag;",
                },
                [
                  m("div"),
                  m("h1", { class: "type-section text-primary text-center" }, "Requests"),
                  m(
                    "button",
                    {
                      type: "button",
                      "aria-label": "Close",
                      "data-tooltip": "Close",
                      style: "-webkit-app-region: no-drag;",
                      onclick: () => shell.closeAppOverlay(),
                      class:
                        "justify-self-end inline-flex items-center justify-center w-6 h-6 rounded-md " +
                        "text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
                    },
                    m(Icon16, { name: "close" }),
                  ),
                ],
              ),
              m("div", { class: "flex-1 min-h-0" }, vnode.children),
            ],
          ),
          // Drag strip over the backdrop to the right of the drawer, matching
          // the titlebar height, so the window stays draggable there too.
          m("div", { class: "flex-1 h-[38px] self-start", style: "-webkit-app-region: drag;" }),
        ],
      );
    },
  };
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
}

export function Shell(): m.Component<ShellAttrs> {
  return {
    view(vnode) {
      const { shell, routePath, workspaceParam, content, homeContent } = vnode.attrs;
      // The visual-diff harness captures with ?visual-diff=1 and no live
      // channel; suppress the indicator so screenshots stay deterministic.
      const isCaptureMode = new URLSearchParams(window.location.search).has("visual-diff");
      const isReconnecting = (shell.channel?.isVisiblyReconnecting ?? false) && !isCaptureMode;

      const routeSearch = shell.currentRouteSearch();
      const isAppOverlay = isAppOverlayPath(routePath);
      // The New machine inspiration stepper floats as a modal only when it is
      // over a machine (?workspace=); opened with none it redirects to the full
      // create form, so it is not an overlay then.
      const isInspirationRoute = routePath === "/create/inspiration";
      // The workspace surface kept mounted underneath: the agent surface itself,
      // or the workspace a Get help / inbox / inspiration modal was opened over.
      // Rendering it at a stable vtree position keeps its iframe from reloading
      // across open/close.
      const behindWorkspaceId =
        isAppOverlay || isInspirationRoute ? overlayBehindWorkspaceId(routePath, routeSearch) : null;
      const isInspirationModal = isInspirationRoute && behindWorkspaceId !== null;
      const surfaceWorkspaceId = workspaceParam ?? behindWorkspaceId;
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
              // An app modal over Home keeps Home painted behind its backdrop;
              // otherwise the routed page is the surface itself.
              [m(LocalPageNotice), isAppOverlay ? homeContent : content],
            );

      let overlay: m.Children = null;
      if (isWorkspaceOverlayPath(routePath)) {
        // The options page is its own docked overlay layer (backdrop + tab
        // strip + card): the Shell just floats it over the mounted surface.
        overlay = content;
      } else if (routePath === "/inbox") {
        overlay = m(InboxOverlay, { shell }, content);
      } else if (isAppOverlay) {
        overlay = m(AppOverlay, { shell, cardClass: appOverlayCardClass(routePath) }, content);
      } else if (isInspirationModal) {
        // The New machine inspiration stepper over a live machine: a centered
        // card, dismissed back to that machine (closeAppOverlay handles it).
        overlay = m(AppOverlay, { shell, cardClass: "w-[600px] min-h-0" }, content);
      }

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
        surfaceWorkspaceId === null ? null : shell.stores.workspaces.toAgentScopedId(surfaceWorkspaceId);
      const health = agentScoped === null ? "healthy" : shell.stores.health.statusFor(agentScoped);
      const band = noticeBandFor(
        health,
        shell.stores.health.discoveryHealth,
        surfaceWorkspaceId !== null,
        electronBridge.isDesktop,
      );
      // The card is a modal of its own, so it is raised only where it can sit
      // on top: the machine's own route. It out-z-indexes the docked options
      // overlay there, but an app-level modal shares its z and is emitted after
      // it, so a card raised behind one would be dimmed and unclickable -- and
      // its capture-phase Escape listener would still take the key, spending
      // the episode's one dismissal on a card the user never saw. A machine
      // behind an app modal keeps its band and gets its card back on the way
      // out.
      const isRecoveryOpen =
        workspaceParam !== null && agentScoped !== null && shell.isRecoveryModalOpenFor(agentScoped);

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
                } else if (agentScoped !== null) {
                  shell.openRecoveryModal(agentScoped);
                }
              },
            })
          : null,
        isRecoveryOpen && workspaceParam !== null && agentScoped !== null
          ? m(RecoveryModal, {
              workspaceAnyId: workspaceParam,
              isSidebarAbove: shell.isSidebarOpen,
              onClose: () => shell.closeRecoveryModal(),
            })
          : null,
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
        m("div#local-page-root", { style: "display: contents" }, [base, overlay]),
      ]);
    },
  };
}
