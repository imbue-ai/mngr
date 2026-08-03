// The fixed 38px titlebar: breadcrumb (home / workspace / page), workspace
// icon-tabs, requests inbox badge, bug-report button, and non-mac window
// controls. Faithful port of ChromeShell.jinja's markup + chrome.js's
// context state machine; DOM ids are preserved for the Playwright
// renderer-contract specs.

import m from "mithril";
import { Badge } from "../components/Badge";
import { Icon12, Icon16 } from "../components/Icon";
import { TitlebarButton } from "../components/TitlebarButton";
import { electronBridge } from "../../electron-bridge";
import type { ShellState } from "./shell-state";
import type { TitlebarContext } from "./classify";
import { classifyRoute, isWorkspaceOverlayPath } from "./classify";

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
      const requestCount = shell.stores.requests.count;
      const isDesktop = electronBridge.isDesktop;
      const isHomeSelected = context.kind === "home";

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
                        id: "ws-tab-share",
                        "aria-label": "Share machine",
                        "data-tooltip": "Share machine",
                        tone: context.activeTab === "share" ? "default" : "muted",
                        extra: context.activeTab === "share" ? "bg-fill-active" : "",
                        onclick: () => toggleWorkspaceOptions(shell, routePath, context, "share"),
                      },
                      m(Icon16, { name: "share" }),
                    ),
                    m(
                      TitlebarButton,
                      {
                        id: "ws-tab-settings",
                        "aria-label": "Machine settings",
                        "data-tooltip": "Machine settings",
                        tone: context.activeTab === "settings" ? "default" : "muted",
                        extra: context.activeTab === "settings" ? "bg-fill-active" : "",
                        onclick: () => toggleWorkspaceOptions(shell, routePath, context, "settings"),
                      },
                      m(Icon16, { name: "settings" }),
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
              id: "requests-toggle",
              "aria-label": "Requests",
              "data-tooltip": "Requests",
              extra: "gap-[3px]",
              onclick: () => m.route.set("/inbox"),
            },
            [m(Icon16, { name: "inbox" }), m(Badge, { id: "requests-badge", count: requestCount, hidden: requestCount === 0 })],
          ),
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
  tab: "share" | "settings",
): void {
  if (context.workspaceAnyId === null) return;
  const agentScoped = shell.stores.workspaces.toAgentScopedId(context.workspaceAnyId);
  if (isWorkspaceOverlayPath(routePath) && context.activeTab === tab) {
    m.route.set(`/workspace/${agentScoped}`);
    return;
  }
  m.route.set(`/workspace/${agentScoped}/options?tab=${tab}`);
}
