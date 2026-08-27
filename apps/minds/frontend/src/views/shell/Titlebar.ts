// The fixed 38px titlebar: breadcrumb (home / workspace / page), workspace
// icon-tabs, notification bell, bug-report button, and non-mac window
// controls. Faithful port of ChromeShell.jinja's markup + chrome.js's context
// state machine; DOM ids are preserved for the Playwright renderer-contract
// specs.
//
// The bell is the chrome's one "something needs you" signal: its badge counts
// UNRESOLVED notifications (a request counts until it is approved, denied, or
// closed -- resolution-based, never "unseen"), and its feed is where an ask is
// found again out of context. This supersedes the earlier "deliberately no
// requests entry" stance: the popup is still the only review surface, but the
// bell is the way back to it -- so requests no longer have to dot every
// breadcrumb and switcher row to be found.

import m from "mithril";
import { Badge } from "../components/Badge";
import { Icon12, Icon16 } from "../components/Icon";
import { TitlebarButton } from "../components/TitlebarButton";
import { electronBridge } from "../../electron-bridge";
import type { OptionsTab } from "../../models/workspaceOptions";
import type { ShellState } from "./shell-state";
import type { TitlebarContext } from "./classify";
import { classifyRoute, isWorkspaceOverlayPath } from "./classify";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { warmPermissionsOverview } from "../../models/permissionsPrefetch";

export interface TitlebarAttrs {
  shell: ShellState;
  routePath: string;
}

export function Titlebar(): m.Component<TitlebarAttrs> {
  return {
    view(vnode) {
      const { shell, routePath } = vnode.attrs;
      const routeSearch = (m.route.get() ?? "").split("?")[1] ?? "";
      const context = classifyRoute(routePath, routeSearch);
      const workspaces = shell.stores.workspaces;
      const isWorkspace = context.kind === "workspace";
      const workspaceName = isWorkspace
        ? (workspaces.accentEntry(context.workspaceAnyId ?? "")?.name ?? "…")
        : "";
      // A request from THIS workspace never toasts (the in-chat card already
      // shows it) and never counts toward the bell's badge alone standing out
      // here, so the key tab carries its own dot -- the one on-screen cue that
      // this machine is waiting on you, visible without opening the panel.
      const currentWorkspaceAgentId = isWorkspace
        ? workspaces.toAgentScopedId(context.workspaceAnyId ?? "")
        : null;
      const hasCurrentWorkspaceRequest =
        shell.stores.notifications.hasUnresolvedForWorkspace(
          currentWorkspaceAgentId,
        );
      const isDesktop = electronBridge.isDesktop;
      const isHomeSelected = context.kind === "home";
      // Every one of these five icons' surfaces (the docked options panel, the
      // request popup, the bell's feed, Get help) draws a raised copy of ALL
      // FIVE at their measured rects, so hide all five here (by visibility,
      // which keeps their boxes and so those rects true) or the two strips
      // would ghost through each other -- matching the legacy
      // body.ws-options-open rule. The panel is still up, frozen, while an app
      // modal (a request popup) floats over it, hence panelRouteBehindOverlay;
      // `/inbox` covers the popup opened from the in-chat card too, which
      // names no panel.
      //
      // The request popup hangs from #ws-tab-strip, and with no machine on
      // screen there is no strip: it falls back to a centered card that raises
      // nothing, so the real five stay painted (as they do under every other
      // centered app modal).
      const isTitlebarPopupOpen =
        isWorkspaceOverlayPath(routePath) ||
        (routePath === "/inbox" && isWorkspace) ||
        routePath === "/help" ||
        shell.panelRouteBehindOverlay !== null ||
        shell.isNotificationsOpen;
      const popupHiddenClass = isTitlebarPopupOpen ? "invisible" : "";

      return m(
        "div#minds-titlebar",
        {
          style:
            "background-color: var(--titlebar-bg, var(--c-surface-primary));",
          class:
            "fixed top-0 left-0 right-0 h-[38px] flex items-center select-none z-[100] px-1",
        },
        m("div", { class: "flex-1 flex items-center gap-0.5 min-w-0" }, [
          shell.isMac
            ? m("div", { class: "w-[72px] shrink-0", "aria-hidden": "true" })
            : null,
          m(
            TitlebarButton,
            {
              id: "home-btn",
              "aria-label": "Home",
              "data-tooltip": "Home",
              variant: "crumb",
              tone: isHomeSelected ? "default" : "muted",
              extra: "gap-1",
              hidden: context.kind === "welcome",
              onclick: () => m.route.set("/"),
            },
            [
              m(Icon16, { name: "home" }),
              m("span", { class: "type-label" }, "Minds"),
            ],
          ),
          m(
            "div#ws-crumb",
            { class: "flex items-center min-w-0", hidden: !isWorkspace },
            isWorkspace
              ? [
                  m(
                    "span",
                    {
                      class: "type-label text-tertiary px-0.5",
                      "aria-hidden": "true",
                    },
                    "/",
                  ),
                  m(
                    TitlebarButton,
                    {
                      id: "workspace-switcher-btn",
                      "aria-label": "Switch machine",
                      "data-tooltip": "Switch machine",
                      variant: "crumb",
                      extra: "gap-1 min-w-0",
                      onclick: (event: MouseEvent) => {
                        const button = (
                          event.currentTarget as HTMLElement
                        ).getBoundingClientRect();
                        shell.openSidebar({
                          x: button.left,
                          y: button.top,
                          width: button.width,
                          height: button.height,
                        });
                      },
                    },
                    [
                      m(
                        "span#workspace-switcher-name",
                        { class: "type-label truncate max-w-[180px]" },
                        workspaceName,
                      ),
                      m(Icon16, {
                        name: "chevron-down-small",
                        extra: "shrink-0 text-tertiary",
                      }),
                    ],
                  ),
                  m(
                    "span",
                    {
                      class: "type-label text-tertiary px-0.5",
                      "aria-hidden": "true",
                    },
                    "/",
                  ),
                  m(
                    "div#ws-tab-strip",
                    { class: "flex items-center gap-1 ml-1" },
                    [
                      m(
                        TitlebarButton,
                        {
                          id: "ws-tab-permissions",
                          "aria-label": "Permissions",
                          "data-tooltip": "Permissions",
                          tone:
                            context.activeTab === "permissions"
                              ? "default"
                              : "muted",
                          extra:
                            (isTitlebarPopupOpen
                              ? "invisible"
                              : context.activeTab === "permissions"
                                ? "bg-fill-active"
                                : "") + " relative",
                          // Pointing at the key starts the read the pane makes
                          // on its first mount, so opening it usually lands on
                          // an answer instead of on "Loading permissions...".
                          onpointerenter: () =>
                            warmPermissionsFor(shell, context),
                          onfocus: () => warmPermissionsFor(shell, context),
                          onclick: () =>
                            toggleWorkspaceOptions(
                              shell,
                              routePath,
                              context,
                              "permissions",
                            ),
                        },
                        [
                          m(Icon16, { name: "key" }),
                          hasCurrentWorkspaceRequest
                            ? m(
                                "span",
                                {
                                  // Centered on the key glyph's own top-right
                                  // corner (the icon sits inset from the
                                  // button's edges by its padding), not the
                                  // button's -- so the dot actually touches
                                  // the key rather than floating in the
                                  // button's outer padding. flex (rather than
                                  // the bare inline span the bell's badge
                                  // wrapper uses) shrink-wraps this span to
                                  // the dot's own 8px box instead of the
                                  // surrounding line-height, which otherwise
                                  // inflates it and vertical-centers the dot
                                  // well past the corner it is meant to sit on.
                                  class:
                                    "pointer-events-none absolute top-0.5 right-0.5 flex",
                                },
                                m(Badge, { id: "permissions-badge" }),
                              )
                            : null,
                        ],
                      ),
                      m(
                        TitlebarButton,
                        {
                          id: "ws-tab-settings",
                          "aria-label": "Machine settings",
                          "data-tooltip": "Machine settings",
                          tone:
                            context.activeTab === "settings"
                              ? "default"
                              : "muted",
                          extra: isTitlebarPopupOpen
                            ? "invisible"
                            : context.activeTab === "settings"
                              ? "bg-fill-active"
                              : "",
                          onclick: () =>
                            toggleWorkspaceOptions(
                              shell,
                              routePath,
                              context,
                              "settings",
                            ),
                        },
                        m(Icon16, { name: "settings" }),
                      ),
                      m(
                        TitlebarButton,
                        {
                          id: "ws-tab-share",
                          "aria-label": "Share machine",
                          "data-tooltip": "Share machine",
                          tone:
                            context.activeTab === "share" ? "default" : "muted",
                          extra: isTitlebarPopupOpen
                            ? "invisible"
                            : context.activeTab === "share"
                              ? "bg-fill-active"
                              : "",
                          onclick: () =>
                            toggleWorkspaceOptions(
                              shell,
                              routePath,
                              context,
                              "share",
                            ),
                        },
                        m(Icon16, { name: "user-plus" }),
                      ),
                    ],
                  ),
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
                  m(
                    "span",
                    {
                      class: "type-label text-tertiary px-0.5",
                      "aria-hidden": "true",
                    },
                    "/",
                  ),
                  m(
                    "span#page-crumb-name",
                    {
                      class:
                        "type-label text-primary truncate max-w-[240px] px-1",
                    },
                    context.pageLabel,
                  ),
                ]
              : null,
          ),
        ]),
        m(
          "div",
          { class: "flex items-center justify-end shrink-0 gap-1 pr-1" },
          [
            notificationsBell(shell, popupHiddenClass),
            m(
              TitlebarButton,
              {
                id: "help-toggle",
                "aria-label": "Report a bug",
                "data-tooltip": "Report a bug",
                tone: "muted",
                extra: popupHiddenClass,
                onclick: () => shell.openHelp(),
              },
              m(Icon16, { name: "bug" }),
            ),
            m("div", { class: "flex" + (shell.isMac ? " hidden" : "") }, [
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
          ],
        ),
      );
    },
  };
}

/** The notification bell + its unresolved-count badge, and the one-time
 * "enable system notifications" hint below it (browser mode only). Clicking
 * opens the feed overlay (putting away whatever modal is up), forwarding the
 * displayed workspace so the feed floats over it (kept mounted), exactly as
 * Get help forwards ?workspace. */
function notificationsBell(
  shell: ShellState,
  popupHiddenClass: string,
): m.Children {
  const unresolvedCount = shell.stores.notifications.unresolvedCount;
  const isOpen = shell.isNotificationsOpen;
  // `?? null` guards the fake shells tests cast in without the controller.
  const notificationsUi = shell.notificationsUi ?? null;
  const isOsHintShown =
    notificationsUi !== null && notificationsUi.shouldShowOsHint();
  return m("span", { class: "relative" }, [
    m(
      TitlebarButton,
      {
        id: "notifications-toggle",
        "aria-label": "Notifications",
        "data-tooltip": "Notifications",
        "aria-expanded": isOpen ? "true" : "false",
        tone: "muted",
        // relative for the badge's own absolute positioning.
        extra: "relative " + popupHiddenClass,
        // The switch, not a bare open: a CENTERED app modal (Minds settings,
        // Accounts) leaves this button reachable, and the feed's backdrop
        // draws under a later-DOM modal's at the same z -- so the modal must
        // be put away first or the feed raises beneath it, dimmed and
        // unclickable.
        onclick: () => shell.switchToNotifications(),
      },
      [
        m(Icon16, { name: "bell" }),
        unresolvedCount > 0
          ? m(
              "span",
              // -top-1: the titlebar only gives the button 5px of clearance
              // above it, less than the 14px-tall badge needs to fully clear
              // the icon -- a wider offset (e.g. -top-2.5) pushes the badge
              // above the window's own top edge, off screen. -top-1 is the
              // deepest offset that stays on screen, at the cost of a few px
              // of corner overlap with the icon (a common badge treatment,
              // and far short of the double-digit overlap the old
              // line-height bug caused). flex (not a bare inline span)
              // shrink-wraps this wrapper to the badge's own 14px box
              // instead of the surrounding line-height, which otherwise
              // inflates it and pushes the badge down past the intended
              // offset (the same bug the permissions-tab dot above had).
              { class: "pointer-events-none absolute -top-1 -right-1 flex" },
              m(Badge, { id: "notifications-badge", count: unresolvedCount }),
            )
          : null,
      ],
    ),
    isOsHintShown && notificationsUi !== null
      ? m(
          "div#notifications-os-hint",
          {
            class:
              "absolute top-full right-0 mt-1 flex items-center gap-1 rounded-md border border-subtle " +
              "bg-surface-primary px-2 py-1 whitespace-nowrap shadow-raised type-helper text-secondary",
          },
          [
            m(
              "button",
              {
                type: "button",
                class: "cursor-pointer hover:text-primary",
                onclick: () =>
                  void notificationsUi.requestOsPermissionFromHint(),
              },
              "Enable system notifications?",
            ),
            m(
              "button",
              {
                type: "button",
                "aria-label": "Dismiss",
                class:
                  "inline-flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded-sm " +
                  "text-tertiary hover:bg-fill-hover hover:text-primary",
                onclick: () => void notificationsUi.dismissOsHint(),
              },
              m(Icon16, { name: "close", size: "sm" }),
            ),
          ],
        )
      : null,
  ]);
}

/** Open the options overlay on `tab` -- or, when that tab's overlay is
 * already showing, close it back to the bare workspace (legacy toggle). */
function toggleWorkspaceOptions(
  shell: ShellState,
  routePath: string,
  context: TitlebarContext,
  tab: OptionsTab,
): void {
  if (context.workspaceAnyId === null) return;
  const agentScoped = shell.stores.workspaces.toAgentScopedId(
    context.workspaceAnyId,
  );
  if (isWorkspaceOverlayPath(routePath) && context.activeTab === tab) {
    m.route.set(`/workspace/${agentScoped}`);
    return;
  }
  m.route.set(`/workspace/${agentScoped}/options?tab=${tab}`);
}

/** Start reading this machine's permissions, if the route names one. */
function warmPermissionsFor(shell: ShellState, context: TitlebarContext): void {
  if (context.workspaceAnyId === null || context.workspaceAnyId === undefined)
    return;
  warmPermissionsOverview(
    shell.stores.workspaces.toAgentScopedId(context.workspaceAnyId),
    defaultFetchJson,
  );
}
