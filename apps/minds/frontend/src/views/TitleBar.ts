// The titlebar interior: breadcrumb (home / workspace / page crumbs +
// icon-tabs), the requests badge, the report-a-bug button and the non-mac
// window controls. Mounts INTO the server-rendered #minds-titlebar bar --
// the bar element itself (fixed positioning, --titlebar-bg background, drag
// regions, titlebar-surface class) stays shell/effect-owned; this component
// owns only the interior and must render the exact same markup as the
// ChromeShell skeleton so the mount takeover cannot change the first paint.
//
// Every visible state is a selector over the store: the content URL
// (classifyContent), the accent/name cache, the requests count, and the
// displayed-workspace health signals for the help button's assist gating.
import m from "mithril";

import type { Host } from "../host";
import { getHost } from "../host";
import { ICONS_12, ICONS_16 } from "../icons";
import { mountPersistent, requireElement } from "../mount";
import {
  connect,
  getAccentCacheEntry,
  getDisplayedWorkspaceAgentId,
  getLastContentUrl,
  getRequestsCount,
  getSystemInterfaceStatus,
  isDisplayedContentReady,
  seedAccentCacheHint,
  setAccentScopeAgentId,
  setContentUrl,
  setDisplayedContentReady,
  setDisplayedWorkspaceAgentId,
} from "../store";
import { classifyContent } from "../titlebar";

// TitlebarButton.jinja's class recipe, preserved verbatim.
const TITLEBAR_BUTTON_BASE =
  "inline-flex items-center justify-center cursor-pointer hover:bg-fill-hover active:bg-fill-active " +
  "focus-visible:outline-2 focus-visible:outline-accent";
const VARIANT_CLASSES: Record<string, string> = {
  nav: "p-1.5 rounded-md",
  crumb: "px-1.5 py-1 rounded-md",
  control: "w-9 h-[38px] rounded-none",
};
const TONE_CLASSES: Record<string, string> = {
  default: "text-primary",
  muted: "text-secondary hover:text-primary",
  danger: "text-primary titlebar-btn-danger",
};

interface TitlebarButtonAttrs {
  id?: string;
  variant?: "nav" | "crumb" | "control";
  tone?: "default" | "muted" | "danger";
  extra?: string;
  ariaLabel?: string;
  tooltip?: string;
  ariaCurrent?: string;
  isHidden?: boolean;
  onclick?: () => void;
}

function titlebarButton(attrs: TitlebarButtonAttrs, children: m.Children): m.Children {
  const variant = VARIANT_CLASSES[attrs.variant ?? "nav"];
  const tone = TONE_CLASSES[attrs.tone ?? "default"];
  const extra = attrs.extra !== undefined ? ` ${attrs.extra}` : "";
  return m(
    "button",
    {
      type: "button",
      id: attrs.id,
      class: `${TITLEBAR_BUTTON_BASE} ${variant} ${tone}${extra}`,
      "aria-label": attrs.ariaLabel,
      "data-tooltip": attrs.tooltip,
      "aria-current": attrs.ariaCurrent,
      hidden: attrs.isHidden === true ? true : undefined,
      onclick: attrs.onclick,
    },
    children,
  );
}

function icon16(name: string, extraClass?: string): m.Children {
  return m(
    "svg",
    {
      class: extraClass !== undefined ? `w-4 h-4 ${extraClass}` : "w-4 h-4",
      viewBox: "0 0 16 16",
      fill: "currentColor",
      "aria-hidden": "true",
    },
    m.trust(ICONS_16[name]),
  );
}

function icon12(name: string): m.Children {
  return m(
    "svg",
    {
      class: "w-3 h-3",
      viewBox: "0 0 12 12",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    m.trust(ICONS_12[name]),
  );
}

export interface TitleBarAttrs {
  host: Host;
  isMac: boolean;
  mngrForwardOrigin: string;
  // chrome.js's switcher toggle (anchor math + per-mode show); the switcher
  // name button calls it.
  onToggleSwitcher: () => void;
}

export function TitleBar(): m.Component<TitleBarAttrs> {
  return {
    view(vnode) {
      const { host, isMac, mngrForwardOrigin, onToggleSwitcher } = vnode.attrs;
      const context = classifyContent(getLastContentUrl() ?? "/");
      const isWorkspace = context.kind === "workspace";
      const crumbAgentId = isWorkspace ? (context.agentId ?? null) : null;
      const crumbName = crumbAgentId !== null ? (getAccentCacheEntry(crumbAgentId)?.name ?? "…") : "";
      const requestsCount = getRequestsCount();

      // The home button is "selected" (text-primary at rest) only on the
      // landing page itself; everywhere else it rests muted. The welcome
      // splash hides it entirely (the user must resolve the account choice).
      const homeTone = context.kind === "home" ? "default" : "muted";

      const workspaceTab = (tab: "workspace" | "settings", iconName: string, label: string): m.Children => {
        const isActive = context.activeTab === tab;
        return titlebarButton(
          {
            id: `ws-tab-${tab}`,
            ariaLabel: label,
            tooltip: label,
            tone: isActive ? "default" : "muted",
            extra: isActive ? "ml-1 bg-fill-active" : "ml-1",
            ariaCurrent: isActive ? "page" : undefined,
            onclick: () => {
              if (crumbAgentId === null) return;
              if (tab === "workspace") host.navigate(`${mngrForwardOrigin}/goto/${crumbAgentId}/`);
              else host.navigate(`/workspace/${crumbAgentId}/settings`);
            },
          },
          icon16(iconName),
        );
      };

      return [
        m("div", { class: "flex-1 flex items-center gap-0.5 min-w-0" }, [
          isMac ? m("div", { class: "w-[72px] shrink-0", "aria-hidden": "true" }) : null,
          // Visibility via the native ``hidden`` attribute (display:none
          // !important), never a ``hidden`` class: these elements carry
          // flex/inline-flex display classes that would beat a .hidden
          // utility in the cascade.
          titlebarButton(
            {
              id: "back-btn",
              ariaLabel: "Back",
              tooltip: "Back",
              isHidden: context.showBack !== true,
              onclick: () => host.goBack(),
            },
            icon16("chevron-left"),
          ),
          titlebarButton(
            {
              id: "home-btn",
              ariaLabel: "Home",
              tooltip: "Home",
              variant: "crumb",
              tone: homeTone,
              extra: "gap-1",
              isHidden: context.kind === "welcome",
              onclick: () => host.navigate("/"),
            },
            [icon16("home"), m("span", { class: "type-label" }, "Minds")],
          ),
          m("div", { id: "ws-crumb", class: "flex items-center min-w-0", hidden: !isWorkspace ? true : undefined }, [
            m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
            titlebarButton(
              {
                id: "workspace-switcher-btn",
                ariaLabel: "Switch workspace",
                tooltip: "Switch workspace",
                variant: "crumb",
                extra: "gap-1 min-w-0",
                onclick: onToggleSwitcher,
              },
              [
                m(
                  "span",
                  {
                    id: "workspace-switcher-name",
                    class: "type-label truncate max-w-[180px]",
                    "data-agent-id": crumbAgentId ?? undefined,
                  },
                  crumbName,
                ),
                icon16("chevron-down-small", "shrink-0 text-tertiary"),
              ],
            ),
            m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
            workspaceTab("workspace", "panels-top-left", "Workspace"),
            workspaceTab("settings", "settings", "Workspace Settings"),
          ]),
          m(
            "div",
            { id: "page-crumb", class: "flex items-center gap-0.5 min-w-0", hidden: context.kind !== "page" ? true : undefined },
            [
              m("span", { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" }, "/"),
              m(
                "span",
                { id: "page-crumb-name", class: "type-label text-primary truncate max-w-[240px] px-1" },
                context.pageLabel ?? "",
              ),
            ],
          ),
        ]),
        m("div", { class: "flex items-center justify-end shrink-0" }, [
          titlebarButton(
            {
              id: "requests-toggle",
              ariaLabel: "Requests",
              tooltip: "Requests",
              extra: "gap-[3px]",
              onclick: () => {
                // ``keep_open=1`` (the browser host's inbox URL) marks this
                // as an intentional open of the whole inbox, so resolving a
                // request advances to the next pending one rather than
                // dismissing the window.
                host.openModal({ kind: "inbox" });
              },
            },
            [
              icon16("inbox"),
              // Badge.jinja's count pill with its 99+ cap; rendered only
              // while non-zero (the native hidden attribute's equivalent).
              requestsCount > 0
                ? m(
                    "span",
                    {
                      id: "requests-badge",
                      class:
                        "inline-flex items-center justify-center align-middle min-w-[16px] px-1 py-px rounded-full " +
                        "bg-important text-white type-badge whitespace-nowrap overflow-hidden",
                    },
                    requestsCount > 99 ? "99+" : String(requestsCount),
                  )
                : null,
            ],
          ),
          titlebarButton(
            {
              id: "help-toggle",
              ariaLabel: "Report a bug",
              tooltip: "Report a bug",
              onclick: () => {
                // Agent-help spawns an /assist chat INSIDE the displayed
                // workspace, so it is only usable when that workspace is
                // actually reachable: gate on both the health tracker (a
                // truthy status means stuck/restarting) and the
                // content-ready signal (false while the "Loading workspace"
                // proxy loader shows, which the stuck signal doesn't cover
                // during startup) -- while still passing the workspace id so
                // a bug report stays scoped to it even when it's down.
                // Read the store AT CLICK TIME (not from the render closure)
                // so a push that hasn't redrawn yet is still honored.
                const agentId = getDisplayedWorkspaceAgentId() ?? "";
                const isAssistAvailable =
                  agentId !== "" && getSystemInterfaceStatus(agentId) === null && isDisplayedContentReady();
                host.openModal({
                  kind: "help",
                  workspaceAgentId: agentId === "" ? undefined : agentId,
                  isAssistAvailable,
                });
              },
            },
            icon16("bug"),
          ),
          m("div", { class: isMac ? "flex hidden" : "flex" }, [
            titlebarButton(
              { id: "min-btn", variant: "control", ariaLabel: "Minimize", tooltip: "Minimize", onclick: () => host.minimizeWindow() },
              icon12("minimize"),
            ),
            titlebarButton(
              { id: "max-btn", variant: "control", ariaLabel: "Maximize", tooltip: "Maximize", onclick: () => host.maximizeWindow() },
              icon12("maximize"),
            ),
            titlebarButton(
              { id: "close-btn", variant: "control", tone: "danger", ariaLabel: "Close", tooltip: "Close", onclick: () => host.closeWindow() },
              icon12("close"),
            ),
          ]),
        ]),
      ];
    },
  };
}

// Mount the titlebar interior into the server-rendered #minds-titlebar bar.
// PERSISTENT mount: the bar lives in the shell outside #local-page-root, so
// hub swaps never tear it down. Before replacing the skeleton, its
// server-seeded crumb (workspace name + accent) is folded into the store so
// the component's first render is pixel-identical to the skeleton.
export function mountTitleBar(target: Element | null, options: { onToggleSwitcher: () => void }): void {
  const el = requireElement(target, "titlebar container");
  const host = getHost();
  connect(host);

  // Seed from the skeleton: the server-rendered crumb carries the workspace
  // name (and the body style carries the accent) before any SSE tick.
  const skeletonName = el.querySelector("#workspace-switcher-name");
  const skeletonAgentId = skeletonName?.getAttribute("data-agent-id") ?? null;
  if (skeletonAgentId !== null) {
    const seededAccent = document.body.style.getPropertyValue("--titlebar-bg").trim();
    seedAccentCacheHint(skeletonAgentId, skeletonName?.textContent || null, seededAccent === "" ? null : seededAccent);
  }

  // Electron pushes the titlebar inputs over IPC (replayed on view load);
  // browser mode's chrome.js pushes them through the MindsUI hooks instead.
  const bridge = window.minds;
  if (bridge !== undefined) {
    bridge.onContentURLChange((url) => setContentUrl(url));
    bridge.onAccentChanged((agentId) => setAccentScopeAgentId(agentId ?? null));
    bridge.onCurrentWorkspaceChanged((agentId, isContentReady) => {
      setDisplayedWorkspaceAgentId(agentId ?? null);
      setDisplayedContentReady(isContentReady);
    });
  }

  const isMac = document.body.dataset.isMac === "true";
  const mngrForwardOrigin = document.body.dataset.mngrForwardOrigin ?? "";
  mountPersistent(el, {
    view: () =>
      m(TitleBar, { host, isMac, mngrForwardOrigin, onToggleSwitcher: options.onToggleSwitcher }),
  });
}
