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
import { NoticeBand } from "./NoticeBand";
import { OverlayBackdrop } from "./OverlayBackdrop";
import type { CardBox } from "./card-resize";
import {
  animateCardResize,
  holdSize,
  isContentPending,
  measureCardBox,
  releaseSize,
} from "./card-resize";
import { NotificationsPage } from "../pages/NotificationsPage";
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
import { WebLoginModal } from "../components/WebLoginModal";
import { DialogCloseButton } from "../components/Modal";
import type { OptionsTab } from "../../models/workspaceOptions";
import { DOCKED_TABS } from "../pages/workspace/WorkspaceOptionsOverlay";
import { Icon16 } from "../components/Icon";
import { Badge } from "../components/Badge";
import { electronBridge } from "../../electron-bridge";

interface AppOverlayAttrs {
  shell: ShellState;
  cardClass: string;
  bodyClass: string;
  /** How Esc / backdrop / the X dismiss the card. Route-based app modals
   * take the default (dismissAppOverlay, a navigation); a local-state
   * overlay reusing this chrome passes its own closer. */
  onDismiss?: () => void;
}

/** The floating card for an app-level modal (the request popup, Minds settings,
 * Accounts, Get help) in the shared OverlayBackdrop: a centered card with a
 * close X. Esc and backdrop clicks dismiss back to the surface it was opened
 * over. The card clips to its rounded corners and keeps its body inside itself
 * -- scrolling it, or handing it a bounded column to scroll within -- so the X
 * stays pinned. */
function AppOverlay(): m.Component<AppOverlayAttrs> {
  return {
    view(vnode) {
      const { shell, cardClass, bodyClass } = vnode.attrs;
      const dismiss =
        vnode.attrs.onDismiss ?? (() => shell.dismissAppOverlay());
      return m(
        OverlayBackdrop,
        {
          backdropId: "app-overlay-backdrop",
          fullWindow: true,
          onDismiss: dismiss,
        },
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
            m(DialogCloseButton, { onClose: dismiss }),
            m("div", { class: bodyClass }, vnode.children),
          ],
        ),
      );
    },
  };
}

/** The #ws-tab-strip (titlebar icon-tabs) window rect, or null when no
 * workspace titlebar is mounted (a hub-context or cold-start open). The strip
 * keeps its box while hidden by visibility, so the rect stays true even while
 * the titlebar's own tabs are hidden under a panel or this popup. The strip's
 * left edge is the key's, which is what the card hangs from. */
function readKeyAnchor(): { x: number; y: number; height: number } | null {
  const strip = document.getElementById("ws-tab-strip");
  if (strip === null) return null;
  const rect = strip.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { x: rect.left, y: rect.top, height: rect.height };
}

/** Gutter kept clear beside the anchored request card at small window sizes. */
const REQUEST_MIN_GUTTER_PX = 24;

/** How far left of the key tab the request card's edge sits, so the card reads
 * as hanging from the key rather than floating loose beside it. */
const REQUEST_KEY_OVERHANG_PX = 20;

const REQUEST_CARD_WIDTH_PX = 600;

/** The request popup, hung from the titlebar's key tab. Reviewing a permission
 * request is a Permissions surface, so the popup attaches to the same key icon
 * the docked Permissions panel hangs from: a raised key tab filled with the
 * card's surface, square-bottomed and joined to the card below it, wherever the
 * popup was opened from (the in-chat card or a Waiting-on-you row). Keeps the
 * centered AppOverlay's ids and dismissal chrome -- it IS the app overlay for
 * /inbox -- and falls back to the centered card when no key tab is mounted to
 * hang from (a hub-context or cold-start open). */
function RequestOverlay(): m.Component<AppOverlayAttrs> {
  let anchor = readKeyAnchor();
  // The card's last settled size, so a change (the request landing after its
  // spinner, the Adjust editor opening) grows out of what was on screen.
  let lastBox: CardBox | null = null;
  // The resize in flight, if any. Held so an interrupting change animates from
  // where the card visually IS rather than from where it was headed.
  let resize: Animation | null = null;
  // The size the window is being held at while the request loads, or null when
  // the card is sizing itself.
  let held: CardBox | null = null;

  function remeasure(): void {
    const next = readKeyAnchor();
    if (next === null) return;
    if (
      anchor === null ||
      anchor.x !== next.x ||
      anchor.y !== next.y ||
      anchor.height !== next.height
    ) {
      anchor = next;
      m.redraw();
    }
  }

  /** The card's box as drawn and as its content wants it -- the same box unless
   * a resize is mid-flight, which is cancelled here so the content size can be
   * measured (an animated element measures at its interpolated size).
   * `wasResizing` says whether that happened, since the drawn box is then the
   * only record of where the card visually was. */
  function boxesOf(
    card: HTMLElement,
  ): { drawn: CardBox; content: CardBox; wasResizing: boolean } | null {
    const drawn = measureCardBox(card);
    if (drawn === null) return null;
    if (resize === null) return { drawn, content: drawn, wasResizing: false };
    resize.cancel();
    resize = null;
    const content = measureCardBox(card);
    return content === null ? null : { drawn, content, wasResizing: true };
  }

  /** Take the card to whatever size its content now asks for, animating from
   * `from` (the previous size, or the surface the popup was opened over). */
  function resizeInto(from: CardBox | null): void {
    // By id rather than through the vnode: this component's own dom is the
    // backdrop, and the card is the panel nested inside it.
    const card = document.getElementById("app-overlay-panel");
    if (card === null) return;
    const boxes = boxesOf(card);
    if (boxes === null) return;
    // A running resize was just cancelled to measure, and cancelling hands the
    // card straight to its content size -- so the trip has to be picked up from
    // where the card visually WAS, not from where the last one started. Without
    // this, any redraw landing inside the 200ms (the list load resolving, a
    // channel frame) ends the animation early and snaps the card to size:
    // `from` would be the size the cancelled run was already headed for, which
    // is no distance at all.
    const previous = boxes.wasResizing ? boxes.drawn : from;
    lastBox = boxes.content;
    if (previous === null) return;
    resize = animateCardResize(card, previous, boxes.content);
    if (resize !== null)
      resize.addEventListener("finish", () => (resize = null));
  }

  return {
    oncreate() {
      remeasure();
      const card = document.getElementById("app-overlay-panel");
      if (card === null) return;
      // Opened over the options panel (a "Waiting on you" row), the popup takes
      // that window over, so it starts at the panel's box and shrinks into its
      // own -- the panel is still mounted behind, so it is there to measure.
      // Opened from anywhere else there is no window to take over, and the
      // popup simply appears at its size.
      const panel = document.getElementById("ws-options-panel");
      const openedFrom = panel === null ? null : measureCardBox(panel);
      // Nothing to shrink INTO yet while the request is still loading: sizing
      // to the spinner would shrink past the answer and grow back into it. The
      // window holds the size it was opened at until the request lands, and
      // then makes that trip once.
      if (openedFrom !== null && isContentPending(card)) {
        holdSize(card, openedFrom);
        held = openedFrom;
        return;
      }
      resizeInto(openedFrom);
    },
    onupdate() {
      remeasure();
      const card = document.getElementById("app-overlay-panel");
      if (card === null) return;
      if (held !== null) {
        if (isContentPending(card)) return;
        const openedFrom = held;
        held = null;
        releaseSize(card);
        resizeInto(openedFrom);
        return;
      }
      resizeInto(lastBox);
    },
    onremove() {
      if (resize !== null) resize.cancel();
      resize = null;
      held = null;
    },
    view(vnode) {
      const { shell, cardClass, bodyClass } = vnode.attrs;
      if (anchor === null) return m(AppOverlay, vnode.attrs, vnode.children);
      // Left edge just left of the key, clamped so the card never leaves the
      // window's gutters (a narrow window slides it left rather than clipping).
      const leftLimit = Math.max(
        REQUEST_MIN_GUTTER_PX,
        window.innerWidth - REQUEST_MIN_GUTTER_PX - REQUEST_CARD_WIDTH_PX,
      );
      const gutterPx = Math.min(
        Math.max(
          REQUEST_MIN_GUTTER_PX,
          Math.round(anchor.x - REQUEST_KEY_OVERHANG_PX),
        ),
        leftLimit,
      );
      return m(
        OverlayBackdrop,
        {
          backdropId: "app-overlay-backdrop",
          fullWindow: true,
          onDismiss: () => shell.dismissAppOverlay(),
        },
        m(
          "div",
          {
            class:
              "fixed left-0 right-0 bottom-3 flex items-start justify-start pointer-events-none",
            style: `top: ${anchor.y + anchor.height}px; padding-left: ${gutterPx}px; padding-right: ${REQUEST_MIN_GUTTER_PX}px`,
          },
          [
            // The raised key: the popup's own copy of the titlebar tab it hangs
            // from, drawn over the dimmed real one at its measured rect.
            // The whole icon-tab strip, not just the key: the popup covers the
            // titlebar's own tabs, and a window that shows one of three tabs
            // while it is open has quietly taken the other two away. Same
            // shape the docked panel draws, so the strip reads as one strip
            // wherever you are.
            m(
              "div",
              {
                id: "app-overlay-key-tab",
                role: "tablist",
                "aria-label": "Machine options",
                class:
                  "pointer-events-auto absolute z-10 flex items-center gap-1",
                style: `left: ${anchor.x}px; top: -${anchor.height}px; height: ${anchor.height}px`,
              },
              DOCKED_TABS.map((entry) => {
                const isSelected = entry.id === "permissions";
                return m(
                  "button",
                  {
                    type: "button",
                    role: "tab",
                    "data-wsopt-tab": entry.id,
                    "aria-selected": isSelected ? "true" : "false",
                    "aria-label": isSelected
                      ? "Close permission request"
                      : entry.label,
                    "data-tooltip": isSelected ? "Close" : entry.label,
                    class:
                      "inline-flex items-center justify-center p-1.5 rounded-md cursor-pointer " +
                      "focus-visible:outline-2 focus-visible:outline-accent " +
                      (isSelected
                        ? "bg-surface-primary rounded-b-none text-primary"
                        : "titlebar-surface text-secondary hover:bg-fill-hover active:bg-fill-active hover:text-primary"),
                    // The key is the tab this window IS, so it puts the window
                    // away, like the titlebar tab it stands in for. The other
                    // two are different surfaces, so they go there -- leaving
                    // the request behind, exactly as they would from the panel.
                    onclick: () =>
                      isSelected
                        ? shell.dismissAppOverlay()
                        : openOptionsTab(shell, entry.id),
                  },
                  m(Icon16, { name: entry.icon }),
                );
              }),
            ),
            m(
              "div#app-overlay-panel",
              {
                class:
                  "pointer-events-auto relative " +
                  cardClass +
                  " max-w-full max-h-full flex flex-col " +
                  "rounded-xl bg-surface-primary shadow-overlay overflow-hidden",
              },
              [
                m(DialogCloseButton, {
                  onClose: () => shell.dismissAppOverlay(),
                }),
                // Laid out at the width the card settles at, not at the card's
                // animating width: left to fill, every frame of the resize
                // would re-wrap the request's text and re-flow its rows, which
                // is what a smooth box change must not do. The card clips, so
                // the wider frame simply shows more surface around it.
                // The width is written out rather than built from
                // REQUEST_CARD_WIDTH_PX: Tailwind generates classes by reading
                // complete literals out of the source.
                // Width only. The card is a COLUMN flex container, so a
                // `shrink-0` here would refuse to shrink in HEIGHT -- the body
                // would grow to its content instead of to the card, and its own
                // overflow scroller would never engage (which is what stopped
                // the Adjust editor's toggle list scrolling).
                m(
                  "div",
                  { class: bodyClass + " w-[600px] max-w-full" },
                  vnode.children,
                ),
              ],
            ),
          ],
        ),
      );
    },
  };
}

/** A titlebar button's window rect by id, or null when none is mounted to
 * hang from (or there is no DOM, as under vitest's node environment). */
function readButtonAnchor(elementId: string): {
  x: number;
  y: number;
  width: number;
  height: number;
} | null {
  if (typeof document === "undefined") return null;
  const button = document.getElementById(elementId);
  if (button === null) return null;
  const rect = button.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
}

/** Air between an anchored button (the bell, the bug-report button) and its
 * popped-over panel: flush, so the panel reads as hanging directly off the
 * raised button's own square bottom corners rather than floating as a
 * separate card. Shared by every anchored popover (see anchoredPopoverPanel). */
const ANCHORED_POPOVER_GAP_PX = 0;

/** Gutter an anchored popover panel keeps from the window's right edge. */
const ANCHORED_POPOVER_MIN_GUTTER_PX = 8;

/** Measures and tracks one titlebar button's rect for an anchored popover,
 * re-running on create/update (mithril's redraw hooks) and only triggering a
 * redraw when the rect actually changed. Kept as per-instance closure state
 * (rather than a plain function call) since NotificationsOverlay/HelpOverlay
 * are themselves closure components -- this factors out their identical
 * measure-and-track logic without losing the fresh-on-remount state each
 * gets from mithril re-invoking its own closure factory on every mount. */
function anchorTracker(elementId: string): {
  get(): { x: number; y: number; width: number; height: number } | null;
  remeasure(): void;
} {
  let anchor = readButtonAnchor(elementId);
  return {
    get: () => anchor,
    remeasure() {
      const next = readButtonAnchor(elementId);
      if (next === null) return;
      if (
        anchor === null ||
        anchor.x !== next.x ||
        anchor.y !== next.y ||
        anchor.width !== next.width ||
        anchor.height !== next.height
      ) {
        anchor = next;
        m.redraw();
      }
    },
  };
}

interface AnchoredPopoverAttrs {
  anchor: { x: number; y: number; width: number; height: number } | null;
  shell: ShellState;
  cardClass: string;
  bodyClass: string;
  onDismiss: () => void;
  children: m.Children;
  /** DOM id for the anchored panel (e.g. "notifications-panel"). */
  panelId: string;
  backdropId: string;
  /** DOM id for the popover's own raised copy of the anchor button. */
  raisedButtonId: string;
  ariaLabel: string;
  iconName: string;
  /** The panel's own max-height class -- the one dimension callers actually
   * differ on (a viewport fraction for the bell's feed, a fixed window
   * offset for the help modal). */
  panelMaxHeightClass: string;
  /** The raised button's badge (the bell's unresolved count); null draws none. */
  badge: { id: string; count: number } | null;
}

/** A popover hung under one of the titlebar's own buttons rather than
 * centered in the window: the bell's notification feed (NotificationsOverlay)
 * and the bug button's Get Help modal (HelpOverlay) both use this shape,
 * differing only in which button they hang from, their icon, and (for the
 * bell) the badge. Falls back to the centered AppOverlay when the button
 * isn't mounted (a hub-context or cold-start open, or under vitest's node
 * environment). The backdrop dismisses it, as does Escape and any
 * navigation -- both wired by the caller's own onDismiss.
 *
 * Draws its own raised copy of the anchor button over the dimmed real one
 * (which the titlebar hides by visibility while open, matching the
 * workspace options tabs) -- so the highlight is a real button on its own
 * surface rather than a re-colored titlebar button, with no titlebar
 * hover/active classes to fight for the background and no rebased
 * .titlebar-surface text color to go invisible against it. A plain function
 * (not a mithril component) so it returns vnodes directly into the caller's
 * own view() -- the caller (a closure component) owns the anchor-tracking
 * state via anchorTracker, since that state needs to be fresh per mount. */
function anchoredPopoverPanel(config: AnchoredPopoverAttrs): m.Children {
  const { anchor, shell, cardClass, bodyClass, onDismiss, children } = config;
  if (anchor === null) {
    return m(AppOverlay, { shell, cardClass, bodyClass, onDismiss }, children);
  }
  // Right-align the panel's edge to the button's, clamped into the gutter.
  const rightPx = Math.max(
    ANCHORED_POPOVER_MIN_GUTTER_PX,
    Math.round(window.innerWidth - (anchor.x + anchor.width)),
  );
  return m(
    OverlayBackdrop,
    { backdropId: config.backdropId, fullWindow: true, onDismiss },
    [
      m(
        `div#${config.panelId}`,
        {
          class:
            "pointer-events-auto fixed " +
            cardClass +
            ` ${config.panelMaxHeightClass} max-w-[calc(100%-16px)] flex flex-col ` +
            // Square only the top-right corner (joins the raised button's
            // own squared-off bottom directly above it, right-aligned to the
            // panel); the top-left corner has nothing above it to join and
            // stays rounded like the rest of the card. No border: the
            // raised button has none either, so together they read as one
            // shape with a tab, not two seamed-together cards.
            "rounded-xl rounded-tr-none bg-surface-primary shadow-overlay overflow-hidden",
          style: `top: ${anchor.y + anchor.height + ANCHORED_POPOVER_GAP_PX}px; right: ${rightPx}px`,
        },
        [
          m(DialogCloseButton, { onClose: onDismiss }),
          m("div", { class: bodyClass }, children),
        ],
      ),
      // The raised button: the popover's own copy of the titlebar button it
      // hangs from, drawn over the dimmed real one (hidden by the titlebar)
      // at its measured rect. Painted after (so: on top of) the panel,
      // whose shadow-overlay halo would otherwise bleed a few px onto the
      // bottom of this opaque white button and read as a slightly darker
      // shade right at the seam.
      m(
        "button",
        {
          type: "button",
          id: config.raisedButtonId,
          "aria-label": config.ariaLabel,
          class:
            "pointer-events-auto fixed inline-flex items-center justify-center p-1.5 rounded-md rounded-b-none " +
            "bg-surface-primary text-primary cursor-pointer focus-visible:outline-2 focus-visible:outline-accent",
          style: `left: ${anchor.x}px; top: ${anchor.y}px; width: ${anchor.width}px; height: ${anchor.height}px`,
          onclick: onDismiss,
        },
        [
          m(Icon16, { name: config.iconName }),
          config.badge !== null
            ? m(
                "span",
                { class: "pointer-events-none absolute -top-1 -right-1 flex" },
                m(Badge, { id: config.badge.id, count: config.badge.count }),
              )
            : null,
        ],
      ),
    ],
  );
}

/** The bell's notification feed: a popover hung under the titlebar bell,
 * keyed on ``shell.isNotificationsOpen`` rather than a route so it floats over
 * whatever surface is on screen (a hub page, the create form, a machine) and
 * never navigates away from it. Anchors to the bell by the same measure-by-id
 * the request popup uses on the key tab; when no bell is mounted to hang from
 * it falls back to a centered card. The backdrop dismisses it, as does
 * Escape (shell.handleEscape) and any navigation (handleRouteChanged). See
 * anchoredPopoverPanel for the shared anchored-popover shape (also used by
 * HelpOverlay). */
function NotificationsOverlay(): m.Component<{ shell: ShellState }> {
  const anchor = anchorTracker("notifications-toggle");

  return {
    oncreate() {
      anchor.remeasure();
    },
    onupdate() {
      anchor.remeasure();
    },
    view(vnode) {
      const { shell } = vnode.attrs;
      const unresolvedCount = shell.stores.notifications.unresolvedCount;
      return anchoredPopoverPanel({
        anchor: anchor.get(),
        shell,
        cardClass: "w-[360px] min-h-0",
        bodyClass: "flex-1 min-h-0 flex flex-col",
        onDismiss: () => shell.closeNotifications(),
        children: m(NotificationsPage),
        panelId: "notifications-panel",
        backdropId: "notifications-backdrop",
        raisedButtonId: "notifications-toggle-raised",
        ariaLabel: "Notifications",
        iconName: "bell",
        panelMaxHeightClass: "max-h-[70vh]",
        badge:
          unresolvedCount > 0
            ? { id: "notifications-badge-raised", count: unresolvedCount }
            : null,
      });
    },
  };
}

/** Get help / report a bug: hung under the titlebar's bug-report button
 * rather than centered in the window, mirroring the bell's own feed panel
 * (see NotificationsOverlay and anchoredPopoverPanel) -- attached to the
 * button that opened it, and drawing the same raised copy of the button
 * over the dimmed real one. Falls back to the centered AppOverlay when the
 * button isn't mounted (a hub-context or cold-start open, or under
 * vitest's node environment). */
function HelpOverlay(): m.Component<{
  shell: ShellState;
  cardClass: string;
  bodyClass: string;
}> {
  const anchor = anchorTracker("help-toggle");

  return {
    oncreate() {
      anchor.remeasure();
    },
    onupdate() {
      anchor.remeasure();
    },
    view(vnode) {
      const { shell, cardClass, bodyClass } = vnode.attrs;
      const dismiss = () => shell.dismissAppOverlay();
      return anchoredPopoverPanel({
        anchor: anchor.get(),
        shell,
        cardClass,
        bodyClass,
        onDismiss: dismiss,
        children: vnode.children,
        panelId: "help-panel",
        backdropId: "help-backdrop",
        raisedButtonId: "help-toggle-raised",
        ariaLabel: "Report a bug",
        iconName: "bug",
        panelMaxHeightClass: "max-h-[calc(100%-64px)]",
        badge: null,
      });
    },
  };
}

/** Leave the request popup for one of the machine's other option panes. */
function openOptionsTab(shell: ShellState, tab: OptionsTab): void {
  const behind = overlayBehindWorkspaceId("/inbox", shell.currentRouteSearch());
  if (behind === null) return;
  m.route.set(`/workspace/${behind}/options`, { tab });
}

/** Per-route sizing for the app modal card. Minds settings takes a definite
 * height -- its two columns scroll within it, and a card that resized itself
 * per section would move the section list out from under the cursor -- capped
 * to the window by the same min() the others' max uses. Accounts is a short
 * list; the request popup is a grant dialog; Get help and the AI-keys mint
 * dialog are compact forms, so those grow to their content. */
function appOverlayCardClass(path: string): string {
  if (path === "/settings") return "w-[880px] h-[min(660px,calc(100%-64px))]";
  if (path === "/inbox") return "w-[600px] min-h-0";
  if (path === "/accounts") return "w-[520px] min-h-0";
  if (path === "/settings/ai-keys") return "w-[460px] min-h-0";
  return "w-[460px] min-h-0"; // /help
}

/** How the card holds its body. Minds settings is a two-column pane that
 * scrolls its own columns -- a scroller here would take its section list down
 * with the panel -- so it gets a height-bounded column instead, the same shape
 * the docked options card gives its panes. Every other overlay is a single
 * column of prose or fields and scrolls as a whole, which is the default a new
 * route falls through to. */
function appOverlayBodyClass(path: string): string {
  if (path === "/settings") return "flex-1 min-h-0 flex flex-col px-6 py-5";
  // The notification feed draws its own edge-to-edge header and row list, so
  // it gets an unpadded bounded column and scrolls its rows itself.
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
      // a modal floats over it.
      const optionsLayer = isWorkspaceOverlayPath(routePath)
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

      let overlay: m.Children = null;
      if (isAppOverlay) {
        const cardClass = appOverlayCardClass(routePath);
        // The request popup hangs from the titlebar's key tab, and Get help
        // hangs from the bug-report button (mirroring the bell's own feed
        // panel); every other app modal is a centered card. (The bell's feed
        // is not a route: it is its own state-keyed layer below.)
        const component =
          routePath === "/inbox"
            ? RequestOverlay
            : routePath === "/help"
              ? HelpOverlay
              : AppOverlay;
        overlay = m(
          component,
          { shell, cardClass, bodyClass: appOverlayBodyClass(routePath) },
          content,
        );
      } else if (isTemplateModal) {
        // The New machine template stepper over a live machine: a centered
        // card, dismissed back to that machine (closeAppOverlay handles it).
        const bodyClass = appOverlayBodyClass(routePath);
        overlay = m(
          AppOverlay,
          { shell, cardClass: "w-[600px] min-h-0", bodyClass },
          content,
        );
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
      const band = noticeBandFor(
        health,
        shell.stores.health.discoveryHealth,
        surfaceWorkspaceId !== null,
        {
          isRestartAppAvailable: electronBridge.isDesktop,
          unreachableProviderLabel,
          deviceEnvironmentBlock: shell.stores.health.appEnvironmentBlock(),
          // Which scopes that app-global condition to the machines it can
          // explain: one on an on-device backend answers over loopback with the
          // wifi off. A row we have no entry for keeps the conservative default.
          isWorkspaceNetworkDependent: entry?.is_network_dependent ?? true,
          isDeviceCannotConnect: entry?.is_device_cannot_connect ?? false,
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
              isAutoRaised: shell.isRecoveryModalAutoRaised(agentScoped),
              onClose: () => shell.closeRecoveryModal(),
            })
          : null,
        // The bell's feed: a popover over whatever surface is on screen.
        // Emitted after RecoveryModal (both share z-[110]; later DOM siblings
        // paint on top) so it visually wins when both are open at once,
        // matching handleEscape's coded precedence (notifications closes
        // ahead of the recovery card -- see shell-state.ts).
        shell.isNotificationsOpen ? m(NotificationsOverlay, { shell }) : null,
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
          overlay,
        ]),
      ]);
    },
  };
}
