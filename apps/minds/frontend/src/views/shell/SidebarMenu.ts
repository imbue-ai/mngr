// The workspace-switcher popover: the in-DOM port of the overlay-iframe
// sidebar (Sidebar.jinja + sidebar.js + sidebar_workspace_row.js). Rendered
// by the Shell while open; a full-window transparent backdrop dismisses on
// click, rows group by account, and the panel anchors below the titlebar's
// switcher button using the same offset math as the legacy iframe.

import m from "mithril";
import { Icon16 } from "../components/Icon";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { electronBridge } from "../../electron-bridge";
import { MIND_LIVENESS_LABELS } from "../../models/create";
import type { ShellState } from "./shell-state";

// Lines a row's workspace-name text up under the breadcrumb name: the row
// label sits 30px inside the menu's left edge, the breadcrumb name 6px
// inside the trigger; 30 - 6 = 24, nudged 2px below the trigger.
const MENU_OFFSET_X = -24;
const MENU_OFFSET_Y = 2;

const CREATE_ATTEMPT_BADGE_LABELS: Record<string, string> = {
  creating: "Creating…",
  interrupted: "Interrupted",
  failed: "Create failed",
};

// Lucide stroke icons for non-running liveness states (sidebar_workspace_row.js).
// Both transitional states share the loader arc.
const TRANSITION_ICON_PATH = "M21 12a9 9 0 1 1-6.219-8.56";
const STATUS_ICON_PATHS: Record<string, string> = {
  STOPPED:
    "m15 18-.722-3.25 M2 8a10.645 10.645 0 0 0 20 0 m20 15-1.726-2.05 m4 15 1.726-2.05 m9 18 .722-3.25",
  STOPPING: TRANSITION_ICON_PATH,
  STARTING: TRANSITION_ICON_PATH,
  UNKNOWN:
    "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3 M12 9v4 M12 17h.01",
};

function statusIcon(liveness: string): m.Children {
  const path = STATUS_ICON_PATHS[liveness];
  const title = MIND_LIVENESS_LABELS[liveness];
  if (path === undefined || title === undefined) return null;
  return m(
    "span",
    {
      class: "sidebar-status-icon shrink-0 inline-flex w-4 justify-center text-secondary",
      title,
    },
    m(
      "svg",
      {
        class: "w-3 h-3",
        viewBox: "0 0 24 24",
        fill: "none",
        stroke: "currentColor",
        "stroke-width": "2",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      },
      path.split(" M").map((segment, idx) => m("path", { d: (idx === 0 ? "" : "M") + segment })),
    ),
  );
}

interface SidebarRowAttrs {
  workspace: UiWorkspaceEntry;
  isCurrent: boolean;
  shell: ShellState;
}

function SidebarRow(): m.Component<SidebarRowAttrs> {
  return {
    view(vnode) {
      const { workspace, isCurrent, shell } = vnode.attrs;
      const createAttemptState = workspace.create_attempt_state || null;
      const isRemote = workspace.is_remote;
      const rowClass =
        "sidebar-item group flex items-center gap-2 h-8 px-2 rounded-md type-body " +
        (isRemote
          ? "is-remote text-secondary opacity-60 cursor-default"
          : "cursor-pointer text-primary" + (isCurrent ? " is-current bg-fill-active" : " hover:bg-fill-hover"));
      const onRowClick = () => {
        if (isRemote) return;
        shell.closeSidebar();
        if (createAttemptState !== null) {
          m.route.set(`/creating/${workspace.id}`);
          return;
        }
        shell.enterWorkspace(workspace.id);
      };
      return m("div", { class: rowClass, "data-agent-id": workspace.id, onclick: onRowClick }, [
        m("span", {
          class: "sidebar-dot w-2.5 h-2.5 rounded-full shrink-0",
          style: workspace.accent ? `background-color: ${workspace.accent};` : undefined,
        }),
        m(
          "span",
          { class: "flex-1 whitespace-nowrap overflow-hidden text-ellipsis" },
          workspace.name || workspace.id,
        ),
        createAttemptState !== null
          ? m(
              "span",
              {
                class:
                  "shrink-0 type-helper px-1.5 py-0.5 rounded " +
                  (createAttemptState === "failed" ? "text-important" : "text-tertiary"),
              },
              CREATE_ATTEMPT_BADGE_LABELS[createAttemptState] ?? createAttemptState,
            )
          : statusIcon(workspace.liveness ?? ""),
        createAttemptState === null && !isRemote && !isCurrent && electronBridge.isDesktop
          ? m(
              "button",
              {
                type: "button",
                class:
                  "sidebar-row-icon flex items-center justify-center w-6 h-6 bg-transparent border-none cursor-pointer text-secondary rounded-md hover:text-primary hover:bg-fill-hover",
                title: "Open in new window",
                tabindex: -1,
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  shell.closeSidebar();
                  electronBridge.openWorkspaceInNewWindow(workspace.id);
                },
              },
              m(
                "svg",
                { class: "w-4 h-4", viewBox: "0 0 16 16", fill: "currentColor" },
                m("path", {
                  d: "M12.9331 10.3336C12.9329 10.6648 12.6646 10.9331 12.3335 10.9333C12.0022 10.9333 11.7331 10.6649 11.7329 10.3336V5.1149L4.09033 12.7575C3.85606 12.9916 3.47695 12.9916 3.24268 12.7575C3.00836 12.5232 3.00836 12.1432 3.24268 11.9088L10.8853 4.26627H5.6665C5.33513 4.26627 5.06689 3.99803 5.06689 3.66666C5.06689 3.33529 5.33513 3.06705 5.6665 3.06705H12.3335C12.6647 3.06722 12.9331 3.33539 12.9331 3.66666V10.3336Z",
                }),
              ),
            )
          : null,
      ]);
    },
  };
}

export interface SidebarMenuAttrs {
  shell: ShellState;
}

export function SidebarMenu(): m.Component<SidebarMenuAttrs> {
  return {
    view(vnode) {
      const { shell } = vnode.attrs;
      if (!shell.isSidebarOpen || shell.sidebarAnchor === null) return null;
      const anchor = shell.sidebarAnchor;
      const left = anchor.x + MENU_OFFSET_X;
      const top = anchor.y + anchor.height + MENU_OFFSET_Y;

      // Group rows by owning account, "Private" first (sidebar.js parity).
      const workspaces = shell.stores.workspaces.workspaces;
      const groups = new Map<string, UiWorkspaceEntry[]>();
      for (const workspace of workspaces) {
        const key = workspace.account || "Private";
        const group = groups.get(key) ?? [];
        group.push(workspace);
        groups.set(key, group);
      }
      const keys = [...groups.keys()].sort((a, b) => {
        if (a === "Private") return -1;
        if (b === "Private") return 1;
        return a.localeCompare(b);
      });
      const currentAnyId = shell.displayedWorkspaceAnyId;
      const currentAgentId =
        currentAnyId === null ? null : shell.stores.workspaces.toAgentScopedId(currentAnyId);

      return m(
        "div#sidebar-backdrop",
        { class: "fixed inset-0 z-[200]", onclick: () => shell.closeSidebar() },
        m(
          "div#sidebar-menu",
          {
            style: `left:${left}px; top:${top}px;`,
            class:
              "dark absolute w-[280px] flex flex-col gap-0.5 p-1 rounded-lg border border-subtle bg-surface-primary shadow-overlay overflow-hidden",
            onclick: (event: MouseEvent) => event.stopPropagation(),
          },
          [
            m(
              "div#sidebar-workspaces",
              { class: "flex flex-col gap-0.5" },
              keys.flatMap((key, keyIdx) => {
                const rows: m.Children[] = [];
                if (keyIdx > 0 || keys.length > 1) {
                  rows.push(m("div", { class: "px-2 pt-2 pb-1 type-section text-tertiary" }, key));
                }
                for (const workspace of groups.get(key) ?? []) {
                  rows.push(
                    m(SidebarRow, {
                      workspace,
                      isCurrent: currentAgentId !== null && workspace.id === currentAgentId,
                      shell,
                    }),
                  );
                }
                return rows;
              }),
            ),
            m(
              "button#sidebar-new-workspace",
              {
                type: "button",
                class:
                  "sidebar-action group flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
                onclick: () => {
                  shell.closeSidebar();
                  m.route.set("/create");
                },
              },
              [m(Icon16, { name: "plus", extra: "shrink-0" }), m("span", "New machine")],
            ),
          ],
        ),
      );
    },
  };
}
