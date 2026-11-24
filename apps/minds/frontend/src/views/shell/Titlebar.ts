// The fixed titlebar. At its left, after the window controls, the trail: Home
// (the wordmark), the window's account, and the workspace open in it, the last
// two each a button that opens its own menu. At its right: the notification
// bell, Report a bug, the Settings gear, Share, and the non-mac window
// controls. The start flow's bar is the centered mark alone.
//
// The bell counts notification reminders. The gear separately stays badged
// while the workspace on screen has unanswered permission requests, even
// after the user opens or clears their reminders.

import m from "mithril";
import lockupUrl from "../../assets/studio-wordmark.svg?inline";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { Icon12, Icon16 } from "../components/Icon";
import { Modal } from "../components/Modal";
import { TitlebarButton } from "../components/TitlebarButton";
import { TrafficLights } from "./TrafficLights";
import { electronBridge } from "../../electron-bridge";
import { isSharedEntry } from "../../models/sharedWithMe";
import type { ChromeAnchor, ShellState } from "./shell-state";
import { classifyRoute } from "./classify";
import { DeviceResourceWarning, DeviceResourceWarningSlot } from "./ResourceWarning";
import {
  SETTINGS_BUTTON_ID,
  SHARE_BUTTON_CLASS,
  SHARE_BUTTON_ID,
  isTitlebarFlyoutOpen,
  shareButtonContent,
} from "./RaisedTitlebarIcons";
import { TitlebarStatusPill } from "./titlebar-status";
import {
  ACCOUNT_BUTTON_ID,
  TRAIL_BUTTON_INSET,
  WORKSPACE_BUTTON_ID,
  accountButtonContent,
  trailWorkspaceFor,
  workspaceButtonContent,
} from "./trail";
import { titlebarStatusFor } from "./workspace-notices";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { warmPermissionsOverview } from "../../models/permissionsPrefetch";

export interface TitlebarAttrs {
  shell: ShellState;
  routePath: string;
}

/** The bar across the top of every chrome (the titlebar and the popout's bar):
 * painted with the workspace accent while one is active (--titlebar-bg), else
 * the app's ground (--c-surface-app). The workspace surface starts under it. */
export const CHROME_BAR_STYLE = "background-color: var(--titlebar-bg, var(--c-surface-app));";
export const CHROME_BAR_CLASS =
  "fixed top-0 left-0 right-0 h-(--titlebar-height) flex items-center select-none z-[100] px-1";

/** The ring a workspace's tour asks for around one chrome control (minds:highlight-chrome), as extra classes. */
export function chromeAnchorRingClass(
  shell: ShellState,
  anchor: ChromeAnchor,
): string {
  return shell.highlightedChromeAnchor === anchor
    ? " ring-2 ring-accent ring-offset-2 animate-pulse"
    : "";
}

/** The room a bar leaves under the native traffic lights on macOS (main.js
 * places them at the bar's left edge). The page under the native lights sets
 * their cursor, so the spacer keeps a draggable bar's grab cursor off them. */
export function trafficLightSpacer(): m.Children {
  return m("div", { class: "w-[72px] shrink-0 cursor-default", "aria-hidden": "true" });
}

/** Whether the bar draws its own minimize / maximize / close buttons at its
 * right: not when the window controls are traffic lights at its left (native,
 * or drawn by the bar), nor when the native frame's title bar carries them. */
export function hasOwnWindowButtons(shell: ShellState): boolean {
  return !shell.isMac && !shell.isTrafficLightsDrawn && !shell.isNativeFrame;
}

/** What the bar puts at its left for the window controls: the drawn traffic
 * lights, the room under the native ones, or nothing. */
export function leftWindowControls(shell: ShellState): m.Children {
  if (shell.isTrafficLightsDrawn) return m(TrafficLights);
  return shell.isMac ? trafficLightSpacer() : null;
}

/** What Share's dialog says on a creation page, where the workspace does not exist yet. */
export const SHARE_NOT_YET_CREATED_MESSAGE = "Check back here after the workspace is created to share it.";

/**
 * The start flow's titlebar: the lockup, centered, in the brand color. Every
 * other control points at something that does not exist yet.
 */
function startTitlebar(shell: ShellState): m.Children {
  const isDesktop = electronBridge.isDesktop;
  return m(
    "div#minds-titlebar",
    {
      style: CHROME_BAR_STYLE,
      class: CHROME_BAR_CLASS,
      "data-titlebar-kind": "start",
    },
    [
      leftWindowControls(shell),
      m("div", { class: "flex-1" }),
      m("img", {
        id: "start-mark",
        // 24px tall, 1px below the bar's centre: electron/shell.html parks the loading screen's mark there too.
        class:
          "start-chrome-mark absolute left-1/2 top-[calc(50%+1px)] h-6 w-auto -translate-x-1/2 -translate-y-1/2 select-none",
        src: lockupUrl,
        alt: "Imbue Studio",
        draggable: false,
      }),
      m("div", { class: "flex" + (hasOwnWindowButtons(shell) ? "" : " hidden") }, [
        m(TitlebarButton, { variant: "control", "aria-label": "Minimize", hidden: !isDesktop, onclick: () => electronBridge.minimize() }, m(Icon12, { name: "minimize" })),
        m(TitlebarButton, { variant: "control", "aria-label": "Maximize", hidden: !isDesktop, onclick: () => electronBridge.maximize() }, m(Icon12, { name: "maximize" })),
        m(TitlebarButton, { variant: "control", tone: "danger", "aria-label": "Close", hidden: !isDesktop, onclick: () => electronBridge.close() }, m(Icon12, { name: "close" })),
      ]),
    ],
  );
}

/** A trail button's classes beyond the crumb's own: lit while its menu is open, and without its focus ring then
 * (it keeps focus from the click that opened the menu, and the ring would show around the raised copy over it). */
function trailButtonExtra(isOpen: boolean, extra: string): string {
  return `${TRAIL_BUTTON_INSET} ${isOpen ? "bg-fill-active !outline-none " : ""}${extra}`;
}

export function Titlebar(): m.Component<TitlebarAttrs> {
  return {
    view(vnode) {
      const { shell, routePath } = vnode.attrs;
      const context = classifyRoute(routePath, shell.currentRouteSearch());
      if (context.kind === "start") return startTitlebar(shell);
      const workspaces = shell.stores.workspaces;
      // A creation page wears the frame of the workspace it is making: the
      // attempt's list row carries the name and accent, and Share opens a
      // dialog instead of a workspace that is not there yet.
      const isCreating = context.kind === "creating";
      // The dialog belongs to the creation page: a route change (the page
      // entering the workspace, the home button) takes it down with the page.
      if (!isCreating) shell.isShareNotYetCreatedDialogOpen = false;
      const isWorkspace = context.kind === "workspace" || isCreating;
      // Someone else's workspace (specs/shared-with-me, S12): a guest cannot share it on.
      const workspaceEntry =
        context.kind === "workspace" ? workspaces.entryByAnyId(context.workspaceAnyId ?? "") : null;
      const isShared = workspaceEntry !== null && isSharedEntry(workspaceEntry);
      // The permission badge tracks pending requests, not dismissible reminders.
      const currentWorkspaceAgentId =
        context.kind === "workspace" ? workspaces.toAgentScopedId(context.workspaceAnyId ?? "") : null;
      const hasCurrentWorkspaceRequest = shell.stores.requests.hasPendingForWorkspace(currentWorkspaceAgentId);
      const isDesktop = electronBridge.isDesktop;
      const isHomeSelected = context.kind === "home";
      // An open flyout draws a raised copy of the bar's buttons at their
      // measured rects, so the real ones are hidden here (by visibility, which
      // keeps their boxes and so those rects true) or the two strips would
      // ghost through each other.
      const popupHiddenClass = isTitlebarFlyoutOpen(shell, routePath) ? "invisible" : "";
      // The workspace's own status, after its button. Not on a creation
      // page: the workspace it would speak for does not exist yet.
      const status =
        context.kind === "workspace" ? titlebarStatusFor(shell, context.workspaceAnyId ?? null) : null;
      // The bar names a workspace wherever one is in context, and offers to open one from Home.
      const isWorkspaceButtonShown = isWorkspace || isHomeSelected;
      const isAccountMenuOpen = shell.isAccountSwitcherOpen;
      const isWorkspaceMenuOpen = shell.isWorkspaceSwitcherOpen;

      const bar = m(
        "div#minds-titlebar",
        {
          style: CHROME_BAR_STYLE,
          class: CHROME_BAR_CLASS,
        },
        m("div", { class: "flex-1 flex items-center gap-0.5 min-w-0" }, [
          leftWindowControls(shell),
          // The wordmark is the way Home.
          m(
            TitlebarButton,
            {
              id: "home-btn",
              "aria-label": "Home",
              "data-tooltip": "Home",
              variant: "crumb",
              extra: "mr-1 shrink-0" + (isHomeSelected ? "" : " opacity-90 hover:opacity-100"),
              onclick: () => shell.routeToHub(),
            },
            // Drawn in the bar's text colour (the artwork's own is fixed), so it reads on every workspace accent.
            m("span", {
              role: "img",
              "aria-label": "Imbue Studio",
              class: "block h-[18px] w-[64px] bg-current text-primary",
              style:
                `mask: url("${lockupUrl}") no-repeat center / contain;` +
                ` -webkit-mask: url("${lockupUrl}") no-repeat center / contain;`,
            }),
          ),
          m(
            TitlebarButton,
            {
              id: ACCOUNT_BUTTON_ID,
              "aria-label": "Switch account",
              "data-tooltip": "Switch account",
              "aria-haspopup": "menu",
              "aria-expanded": isAccountMenuOpen ? "true" : "false",
              "data-account-id": shell.accountId,
              variant: "crumb",
              extra: trailButtonExtra(isAccountMenuOpen, popupHiddenClass),
              onclick: () => (isAccountMenuOpen ? shell.closeAccountSwitcher() : shell.openAccountSwitcher()),
            },
            accountButtonContent(shell, isAccountMenuOpen, "account-switcher-name"),
          ),
          m(
            "div#ws-crumb",
            { class: "flex items-center min-w-0", hidden: !isWorkspaceButtonShown },
            isWorkspaceButtonShown
              ? [
                  m("span", { class: "type-label text-tertiary px-1", "aria-hidden": "true" }, "/"),
                  m(
                    TitlebarButton,
                    {
                      id: WORKSPACE_BUTTON_ID,
                      "aria-label": isWorkspace ? "Switch workspace" : "Open a workspace",
                      "data-tooltip": isWorkspace ? "Switch workspace" : "Open a workspace",
                      "aria-haspopup": "menu",
                      "aria-expanded": isWorkspaceMenuOpen ? "true" : "false",
                      variant: "crumb",
                      extra: trailButtonExtra(
                        isWorkspaceMenuOpen,
                        popupHiddenClass + chromeAnchorRingClass(shell, "workspace-switcher"),
                      ),
                      onclick: () =>
                        isWorkspaceMenuOpen ? shell.closeWorkspaceSwitcher() : shell.openWorkspaceSwitcher(),
                    },
                    workspaceButtonContent(isWorkspace ? trailWorkspaceFor(shell, context.workspaceAnyId) : null, {
                      isOpen: isWorkspaceMenuOpen,
                      isOnBar: true,
                      nameId: "workspace-switcher-name",
                    }),
                  ),
                  // Hidden like the buttons while a flyout raises its copy over the bar.
                  status !== null ? m(TitlebarStatusPill, { status, extra: popupHiddenClass }) : null,
                ]
              : null,
          ),
          m(
            "div#page-crumb",
            {
              class: "flex items-center gap-0.5 min-w-0",
              hidden: context.kind !== "page",
            },
            context.kind === "page"
              ? [
                  m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
                  m("span#page-crumb-name", { class: "type-label text-primary truncate max-w-[240px] px-1" }, context.pageLabel),
                ]
              : null,
          ),
        ]),
        m("div", { class: "flex items-center justify-end shrink-0 gap-1 pr-1" }, [
          // This computer's resource warning: about the device, not the account. Only its place is kept
          // here; the live pill is drawn above the bar (see ResourceWarning.ts).
          m(DeviceResourceWarningSlot, { store: shell.stores.deviceResources }),
          notificationsBell(shell, popupHiddenClass),
          m(
            TitlebarButton,
            {
              id: "help-toggle",
              "aria-label": "Report a bug",
              "data-tooltip": "Report a bug",
              tone: "muted",
              extra: popupHiddenClass + chromeAnchorRingClass(shell, "help"),
              onclick: () => shell.openHelp(),
            },
            m(Icon16, { name: "bug" }),
          ),
          // The one way into settings, from Home and from a workspace alike. A workspace's permissions live
          // there: while its agents wait on an answer, the gear carries the dot and opens straight onto them.
          m(
            TitlebarButton,
            {
              id: SETTINGS_BUTTON_ID,
              "aria-label": "Settings",
              "data-tooltip": "Settings",
              tone: "muted",
              extra:
                "relative " +
                popupHiddenClass +
                chromeAnchorRingClass(shell, "settings") +
                chromeAnchorRingClass(shell, "permissions"),
              // Pointing at the gear starts the read the Permissions pane makes on its first mount, so opening
              // it on a waiting request usually lands on an answer instead of on "Loading permissions...".
              onpointerenter: () => {
                if (hasCurrentWorkspaceRequest && currentWorkspaceAgentId !== null)
                  warmPermissionsOverview(currentWorkspaceAgentId, defaultFetchJson);
              },
              onclick: () => shell.openSettingsFromBar(),
            },
            [
              m(Icon16, { name: "settings" }),
              hasCurrentWorkspaceRequest
                ? m(
                    "span",
                    // flex shrink-wraps this span to the dot's own 8px box instead of the surrounding
                    // line-height, which would otherwise push the dot past the glyph's corner.
                    { class: "pointer-events-none absolute top-0.5 right-0.5 flex" },
                    m(Badge, { id: "permissions-badge" }),
                  )
                : null,
            ],
          ),
          // Sharing, the workspace's one action in the bar. Not in someone else's workspace.
          isWorkspace && !isShared
            ? m(
                "button",
                {
                  type: "button",
                  id: SHARE_BUTTON_ID,
                  "aria-label": "Share",
                  class: `ml-1 ${SHARE_BUTTON_CLASS} ${popupHiddenClass}${chromeAnchorRingClass(shell, "share")}`,
                  onclick: () => shell.pressShare(),
                },
                shareButtonContent(),
              )
            : null,
          m("div", { class: "flex" + (hasOwnWindowButtons(shell) ? "" : " hidden") }, [
            m(
              TitlebarButton,
              {
                variant: "control",
                id: "min-btn",
                "aria-label": "Minimize",
                "data-tooltip": "Minimize",
                hidden: !isDesktop,
                onclick: () => electronBridge.minimize(),
              },
              m(Icon12, { name: "minimize" }),
            ),
            m(
              TitlebarButton,
              {
                variant: "control",
                id: "max-btn",
                "aria-label": "Maximize",
                "data-tooltip": "Maximize",
                hidden: !isDesktop,
                onclick: () => electronBridge.maximize(),
              },
              m(Icon12, { name: "maximize" }),
            ),
            m(
              TitlebarButton,
              {
                variant: "control",
                tone: "danger",
                id: "close-btn",
                "aria-label": "Close",
                "data-tooltip": "Close",
                hidden: !isDesktop,
                onclick: () => electronBridge.close(),
              },
              m(Icon12, { name: "close" }),
            ),
          ]),
        ]),
      );
      // A sibling of the bar, not a child: .titlebar-surface re-bases the text
      // tokens for the accent, which would paint the dialog's card white on white.
      const closeNotYet = () => (shell.isShareNotYetCreatedDialogOpen = false);
      const dialog = shell.isShareNotYetCreatedDialogOpen
        ? m(Modal, { isOpen: true, id: "creating-not-yet-dialog", onClose: closeNotYet }, [
            m("p", { class: "type-body text-primary mb-4" }, SHARE_NOT_YET_CREATED_MESSAGE),
            m("div", { class: "flex justify-end" }, m(Button, { variant: "secondary", onclick: closeNotYet }, "Close")),
          ])
        : null;
      const resourceWarning = m(DeviceResourceWarning, {
        store: shell.stores.deviceResources,
        onOpenSettings: () => shell.openSettings({ tab: "studio", section: "this-computer" }),
        extra: popupHiddenClass,
      });
      return [bar, resourceWarning, dialog];
    },
  };
}

/** The notification bell + its unresolved-count badge. Clicking opens the
 * feed (putting away whatever modal is up) over whatever the window shows. */
function notificationsBell(shell: ShellState, popupHiddenClass: string): m.Children {
  const unresolvedCount = shell.stores.notifications.unresolvedCount;
  const isOpen = shell.isNotificationsOpen;
  return m(
    TitlebarButton,
    {
      id: "notifications-toggle",
      "aria-label": "Notifications",
      "data-tooltip": "Notifications",
      "aria-expanded": isOpen ? "true" : "false",
      tone: "muted",
      // relative for the badge's own absolute positioning.
      extra: "relative " + popupHiddenClass,
      // The switch, not a bare open: a CENTERED app modal leaves this button
      // reachable, and the feed's backdrop draws under a later-DOM modal's at
      // the same z -- so the modal must be put away first or the feed raises
      // beneath it, dimmed and unclickable.
      onclick: () => shell.switchToNotifications(),
    },
    [
      m(Icon16, { name: "bell" }),
      unresolvedCount > 0
        ? m(
            "span",
            // flex (not a bare inline span) shrink-wraps the wrapper to the badge's own 14px box rather than
            // the surrounding line-height, which would inflate it and push the badge past the intended offset.
            { class: "pointer-events-none absolute -top-1 -right-1 flex" },
            m(Badge, { id: "notifications-badge", count: unresolvedCount }),
          )
        : null,
    ],
  );
}
