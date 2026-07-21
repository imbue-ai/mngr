// The floating workspace-switcher menu interior: grouped workspace rows
// (Private first, then account groups alphabetically, headers when more than
// one group) plus the "New workspace" CTA that used to live in
// SidebarBottom.jinja. Mounted into the positioned #sidebar-menu shell by
// both hosts (the browser-mode inline menu and Electron's overlay sidebar
// page); the shell owns position, this component owns content.
import m from "mithril";

import type { ChromeWorkspaceEntry } from "../chrome_state";
import type { Host } from "../host";
import { getHost } from "../host";
import { ICONS_16 } from "../icons";
import { mountPersistent, requireElement } from "../mount";
import {
  connect,
  getActiveRowAgentId,
  getBackupWarning,
  getEffectiveLiveness,
  getWorkspaces,
  setAccentScopeAgentId,
  setDisplayedWorkspaceAgentId,
} from "../store";
import { WorkspaceRow } from "./WorkspaceRow";

export interface WorkspaceMenuAttrs {
  host: Host;
  // The ``mngr forward`` plugin's bare origin; workspace links target the
  // plugin (``/goto/<agent>/``), not minds.
  mngrForwardOrigin: string;
  // Electron supports multi-window, so its rows carry the open-in-new arrow.
  withOpenNew: boolean;
  // Called after any selection so the shell can dismiss the menu. Electron's
  // main process closes the modal itself on navigate/open-new IPCs, so its
  // shell passes a no-op; the browser shell hides the backdrop.
  onDismiss: () => void;
}

interface WorkspaceGroup {
  label: string;
  workspaces: ChromeWorkspaceEntry[];
}

// Group by owning account with the account-less "Private" group first, then
// accounts alphabetically (ported from the deleted renderWorkspaces twins in
// sidebar.js / chrome.js).
export function groupWorkspaces(workspaces: ChromeWorkspaceEntry[]): WorkspaceGroup[] {
  const byLabel = new Map<string, ChromeWorkspaceEntry[]>();
  workspaces.forEach((workspace) => {
    const label = workspace.account ?? "Private";
    const group = byLabel.get(label);
    if (group === undefined) byLabel.set(label, [workspace]);
    else group.push(workspace);
  });
  const labels = Array.from(byLabel.keys()).sort((a, b) => {
    if (a === "Private") return -1;
    if (b === "Private") return 1;
    return a.localeCompare(b);
  });
  return labels.map((label) => ({ label, workspaces: byLabel.get(label) ?? [] }));
}

export function WorkspaceMenu(): m.Component<WorkspaceMenuAttrs> {
  return {
    view(vnode) {
      const { host, mngrForwardOrigin, withOpenNew, onDismiss } = vnode.attrs;
      const groups = groupWorkspaces(getWorkspaces());
      const activeAgentId = getActiveRowAgentId();

      const selectWorkspace = (agentId: string): void => {
        host.navigate(`${mngrForwardOrigin}/goto/${agentId}/`);
        onDismiss();
      };

      const rows: m.Children[] = [];
      groups.forEach((group, groupIdx) => {
        if (groupIdx > 0 || groups.length > 1) {
          rows.push(m("div", { class: "px-2 pt-2 pb-1 type-section text-tertiary" }, group.label));
        }
        group.workspaces.forEach((workspace) => {
          rows.push(
            // No keys: headers and rows are siblings, and mixing keyed and
            // unkeyed siblings is the classic mithril pitfall. Rows hold no
            // internal state, so positional diffing is correct here.
            m(WorkspaceRow, {
              workspace,
              isCurrent: workspace.id === activeAgentId,
              withOpenNew,
              liveness: getEffectiveLiveness(workspace),
              backupWarning: getBackupWarning(workspace.id),
              onSelect: selectWorkspace,
              onOpenNew: (agentId) => {
                host.openWorkspaceInNewWindow(agentId);
                onDismiss();
              },
              onContextMenu: (agentId, x, y) => host.showWorkspaceContextMenu(agentId, x, y),
            }),
          );
        });
      });

      return [
        m("div", { class: "flex flex-col gap-0.5", "data-menu-workspaces": "" }, rows),
        // The "New workspace" CTA (formerly SidebarBottom.jinja). A direct
        // flex child of #sidebar-menu, so the panel's gap supplies spacing.
        m(
          "button",
          {
            id: "sidebar-new-workspace",
            type: "button",
            class:
              "sidebar-action group flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
            onclick: () => {
              host.navigate("/create");
              onDismiss();
            },
          },
          [
            m(
              "svg",
              { class: "w-4 h-4 shrink-0", viewBox: "0 0 16 16", fill: "currentColor", "aria-hidden": "true" },
              m.trust(ICONS_16["plus"]),
            ),
            m("span", "New workspace"),
          ],
        ),
      ];
    },
  };
}

export interface MountWorkspaceMenuOptions {
  // Electron's overlay sidebar page: wire a document-level click listener
  // that closes the modal via host.closeModal() when a click lands outside
  // the #sidebar-menu panel (the transparent body IS the backdrop there).
  // Escape stays handled by main's modal before-input-event listener.
  isOverlayModal?: boolean;
  // Called after any selection. The browser shell passes its hide-backdrop
  // routine; Electron's main process already closes the modal on the
  // navigate / open-in-new-window IPCs, so the overlay page omits this
  // (a follow-up close IPC would race main's and re-open the modal).
  onDismiss?: () => void;
}

// Mount the switcher-menu interior into the positioned #sidebar-menu shell.
// PERSISTENT mount (no swap-engine teardown): the browser-mode menu lives in
// the chrome shell outside #local-page-root, and the Electron overlay page
// is its own document.
export function mountWorkspaceMenu(target: Element | null, options?: MountWorkspaceMenuOptions): void {
  const el = requireElement(target, "workspace menu container");
  const host = getHost();
  connect(host);
  // Electron pushes the current-row signals over IPC (replayed on view
  // load); browser mode's chrome.js pushes its breadcrumb workspace through
  // setWorkspaceMenuCurrentAgent instead.
  const bridge = window.minds;
  if (bridge !== undefined) {
    bridge.onAccentChanged((agentId) => setAccentScopeAgentId(agentId ?? null));
    bridge.onCurrentWorkspaceChanged((agentId) => setDisplayedWorkspaceAgentId(agentId ?? null));
  }
  if (options?.isOverlayModal === true) {
    document.addEventListener("click", (event) => {
      const target_ = event.target;
      if (target_ instanceof Element && target_.closest("#sidebar-menu") !== null) return;
      host.closeModal();
    });
  }
  const mngrForwardOrigin = document.body.dataset.mngrForwardOrigin ?? "";
  const onDismiss = options?.onDismiss ?? ((): void => undefined);
  mountPersistent(el, {
    view: () =>
      m(WorkspaceMenu, {
        host,
        mngrForwardOrigin,
        withOpenNew: host.kind === "electron",
        onDismiss,
      }),
  });
}
