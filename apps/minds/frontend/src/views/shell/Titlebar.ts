// The fixed 38px titlebar: breadcrumb (home / workspace / page), workspace
// icon-tabs, bug-report button, and non-mac window controls. Faithful port of
// ChromeShell.jinja's markup + chrome.js's context state machine; DOM ids are
// preserved for the Playwright renderer-contract specs.
//
// There is deliberately no requests entry here: a pending permission request
// surfaces only as the centered popup the Shell floats over whatever is on
// screen, never as a place the user has to remember to go and look.

import m from "mithril";
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
      const isDesktop = electronBridge.isDesktop;
      const isHomeSelected = context.kind === "home";
      // While the docked options panel is up it draws its own tab strip at this
      // strip's measured rect, so hide ours (by visibility, keeping the crumb's
      // box and the rect true) or the two would ghost through each other --
      // matching the legacy body.ws-options-open rule. The panel is still up,
      // frozen, while an app modal (a request popup) floats over it.
      // Also while the request popup is up: it draws the same strip at this
      // strip's measured rect, so leaving ours painted would ghost two strips
      // through each other -- the reason the panel hides them in the first
      // place. `/inbox` covers the popup opened from the in-chat card too,
      // which names no panel.
      const isOptionsOverlayOpen =
        isWorkspaceOverlayPath(routePath) || routePath === "/inbox" || shell.panelRouteBehindOverlay !== null;

      return m(
        "div#minds-titlebar",
        {
          style: "background-color: var(--titlebar-bg, var(--c-surface-primary));",
          class: "fixed top-0 left-0 right-0 h-[38px] flex items-center select-none z-[100] px-1",
        },
        m("div", { class: "flex-1 flex items-center gap-0.5 min-w-0" }, [
          shell.isMac ? m("div", { class: "w-[72px] shrink-0", "aria-hidden": "true" }) : null,
          m(
            TitlebarButton,
            {
              id: "back-btn",
              "aria-label": "Back",
              "data-tooltip": "Back",
              hidden: !context.isBackShown,
              onclick: () => history.back(),
            },
            m(Icon16, { name: "chevron-left" }),
          ),
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
            [m(Icon16, { name: "home" }), m("span", { class: "type-label" }, "Minds")],
          ),
          m(
            "div#ws-crumb",
            { class: "flex items-center min-w-0", hidden: !isWorkspace },
            isWorkspace
              ? [
                  m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
                  m(
                    TitlebarButton,
                    {
                      id: "workspace-switcher-btn",
                      "aria-label": "Switch machine",
                      "data-tooltip": "Switch machine",
                      variant: "crumb",
                      extra: "gap-1 min-w-0",
                      onclick: (event: MouseEvent) => {
                        const button = (event.currentTarget as HTMLElement).getBoundingClientRect();
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
                      m(Icon16, { name: "chevron-down-small", extra: "shrink-0 text-tertiary" }),
                    ],
                  ),
                  m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
                  m("div#ws-tab-strip", { class: "flex items-center gap-1 ml-1" }, [
                    m(
                      TitlebarButton,
                      {
                        id: "ws-tab-permissions",
                        "aria-label": "Permissions",
                        "data-tooltip": "Permissions",
                        tone: context.activeTab === "permissions" ? "default" : "muted",
                        extra: isOptionsOverlayOpen
                          ? "invisible"
                          : context.activeTab === "permissions"
                            ? "bg-fill-active"
                            : "",
                        // Pointing at the key starts the read the pane makes
                        // on its first mount, so opening it usually lands on
                        // an answer instead of on "Loading permissions...".
                        onpointerenter: () => warmPermissionsFor(shell, context),
                        onfocus: () => warmPermissionsFor(shell, context),
                        onclick: () => toggleWorkspaceOptions(shell, routePath, context, "permissions"),
                      },
                      m(Icon16, { name: "key" }),
                    ),
                    m(
                      TitlebarButton,
                      {
                        id: "ws-tab-settings",
                        "aria-label": "Machine settings",
                        "data-tooltip": "Machine settings",
                        tone: context.activeTab === "settings" ? "default" : "muted",
                        extra: isOptionsOverlayOpen
                          ? "invisible"
                          : context.activeTab === "settings"
                            ? "bg-fill-active"
                            : "",
                        onclick: () => toggleWorkspaceOptions(shell, routePath, context, "settings"),
                      },
                      m(Icon16, { name: "settings" }),
                    ),
                    m(
                      TitlebarButton,
                      {
                        id: "ws-tab-share",
                        "aria-label": "Share machine",
                        "data-tooltip": "Share machine",
                        tone: context.activeTab === "share" ? "default" : "muted",
                        extra: isOptionsOverlayOpen
                          ? "invisible"
                          : context.activeTab === "share"
                            ? "bg-fill-active"
                            : "",
                        onclick: () => toggleWorkspaceOptions(shell, routePath, context, "share"),
                      },
                      m(Icon16, { name: "share" }),
                    ),
                  ]),
                ]
              : null,
          ),
          m(
            "div#page-crumb",
            { class: "flex items-center gap-0.5 min-w-0", hidden: context.kind !== "page" },
            context.kind === "page"
              ? [
                  m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
                  m(
                    "span#page-crumb-name",
                    { class: "type-label text-primary truncate max-w-[240px] px-1" },
                    context.pageLabel,
                  ),
                ]
              : null,
          ),
        ]),
        m("div", { class: "flex items-center justify-end shrink-0" }, [
          m(
            TitlebarButton,
            {
              id: "help-toggle",
              "aria-label": "Report a bug",
              "data-tooltip": "Report a bug",
              // Carry the displayed workspace so the help page can offer the
              // in-workspace /assist flow (only when its interface is healthy,
              // mirroring the legacy titlebar's assist gating).
              onclick: () => {
                const displayed = shell.displayedWorkspaceAnyId;
                if (displayed === null) {
                  m.route.set("/help");
                  return;
                }
                const agentScoped = shell.stores.workspaces.toAgentScopedId(displayed);
                const isHealthy = shell.stores.health.isContentAssumedReady(agentScoped);
                m.route.set("/help", { workspace: agentScoped, assist: isHealthy ? "1" : "0" });
              },
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
        ]),
      );
    },
  };
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
  const agentScoped = shell.stores.workspaces.toAgentScopedId(context.workspaceAnyId);
  if (isWorkspaceOverlayPath(routePath) && context.activeTab === tab) {
    m.route.set(`/workspace/${agentScoped}`);
    return;
  }
  m.route.set(`/workspace/${agentScoped}/options?tab=${tab}`);
}

/** Start reading this machine's permissions, if the route names one. */
function warmPermissionsFor(shell: ShellState, context: TitlebarContext): void {
  if (context.workspaceAnyId === null || context.workspaceAnyId === undefined) return;
  warmPermissionsOverview(shell.stores.workspaces.toAgentScopedId(context.workspaceAnyId), defaultFetchJson);
}
