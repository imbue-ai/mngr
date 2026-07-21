// The workspace-menu list item -- the single source of truth for a workspace
// row's markup (port of the deleted static/sidebar_workspace_row.js builder,
// class strings preserved verbatim).
//
// Composability rule: the row carries NO outer positioning (no margin) --
// spacing between rows is owned by the parent container's flex ``gap``.
//
// ``liveness`` (RUNNING / STOPPED / UNKNOWN, present on shutdown-capable
// local workspaces) renders a status icon on stopped / unknown rows; running
// rows show nothing. ``withOpenNew`` adds the "open in new window" arrow to
// rows for OTHER workspaces (Electron only -- browser mode has no
// multi-window concept and omits it); the current row and remote rows carry
// no action buttons. ``isCurrent`` marks the row selected.
import m from "mithril";

import type { ChromeWorkspaceEntry } from "../chrome_state";
import { ICONS_16, ROW_STATUS_ICONS_24 } from "../icons";

const ROW_STATUS_TITLES: Record<string, string> = {
  STOPPED: "Stopped",
  UNKNOWN: "Status unknown",
};

const STALE_TITLE = "This workspace's provider had a discovery error; its status is unverified (still usable).";

export interface WorkspaceRowAttrs {
  workspace: ChromeWorkspaceEntry;
  isCurrent?: boolean;
  withOpenNew?: boolean;
  // The effective liveness to render (the caller resolves optimistic
  // transients through the store); defaults to the entry's own value.
  liveness?: string | null;
  // Backup warning tooltip, or null for no badge (the caller reads the
  // store's backup-health bridge).
  backupWarning?: string | null;
  onSelect?: (agentId: string) => void;
  onOpenNew?: (agentId: string) => void;
  onContextMenu?: (agentId: string, x: number, y: number) => void;
}

// Non-interactive liveness indicator: a 16px-wide slot holding a 12px stroke
// icon with a tooltip. Running minds render nothing.
function statusIcon(liveness: string | null): m.Children {
  if (liveness === null) return null;
  const pathSvg = ROW_STATUS_ICONS_24[liveness];
  if (pathSvg === undefined) return null;
  return m(
    "span",
    { class: "sidebar-status-icon shrink-0 inline-flex w-4 justify-center text-secondary", title: ROW_STATUS_TITLES[liveness] },
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
      // Static path data from icons.ts, never interpolated.
      m.trust(pathSvg),
    ),
  );
}

export function WorkspaceRow(): m.Component<WorkspaceRowAttrs> {
  return {
    view(vnode) {
      const { workspace, onSelect, onOpenNew, onContextMenu } = vnode.attrs;
      const isCurrent = vnode.attrs.isCurrent === true;
      const withOpenNew = vnode.attrs.withOpenNew === true;
      const isRemote = workspace.is_remote === "true";
      const liveness = vnode.attrs.liveness !== undefined ? vnode.attrs.liveness : (workspace.liveness ?? null);
      const backupWarning = vnode.attrs.backupWarning ?? null;

      const rowClass =
        "sidebar-item group flex items-center gap-2 h-8 px-2 rounded-md type-body " +
        (isRemote
          ? "is-remote text-secondary opacity-60 cursor-default"
          : "cursor-pointer text-primary" + (isCurrent ? " is-current bg-fill-active" : " hover:bg-fill-hover"));

      return m(
        "div",
        {
          class: rowClass + (backupWarning !== null ? " has-backup-warning" : "") + (workspace.is_stale === "true" ? " is-stale" : ""),
          "data-agent-id": workspace.id,
          style: { "--workspace-accent": workspace.accent },
          onclick: () => {
            // Rows for workspaces on another device are informational only.
            if (isRemote) return;
            onSelect?.(workspace.id);
          },
          oncontextmenu:
            onContextMenu !== undefined
              ? (event: MouseEvent) => {
                  event.preventDefault();
                  onContextMenu(workspace.id, event.clientX, event.clientY);
                }
              : undefined,
        },
        [
          m("span", {
            class: "sidebar-dot w-2.5 h-2.5 rounded-full shrink-0",
            style: { background: workspace.accent },
          }),
          m("span", { class: "flex-1 whitespace-nowrap overflow-hidden text-ellipsis" }, workspace.name || workspace.id),
          isRemote ? null : statusIcon(liveness),
          // A workspace hosted on another device: greyed, non-navigable, with
          // a location badge instead of action icons.
          isRemote
            ? m(
                "span",
                { class: "inline-flex items-center px-1.5 py-0.5 rounded-md type-label bg-fill-subtle text-tertiary shrink-0" },
                `on ${workspace.location ?? "another device"}`,
              )
            : null,
          // Backup-service problem detected (one badge style for all causes;
          // the tooltip carries the distinction).
          backupWarning !== null
            ? m("span", {
                class: "sidebar-backup-dot inline-block w-1.5 h-1.5 rounded-full bg-warning shrink-0",
                title: backupWarning,
              })
            : null,
          // Retained-but-unverified workspace: amber dot, row stays clickable.
          workspace.is_stale === "true"
            ? m("span", {
                class: "sidebar-stale-dot inline-block w-1.5 h-1.5 rounded-full bg-warning/80 shrink-0",
                title: STALE_TITLE,
              })
            : null,
          // Row action icon, always visible: the open-in-new arrow, only on
          // rows for OTHER local workspaces (Electron only).
          withOpenNew && !isCurrent && !isRemote
            ? m(
                "button",
                {
                  type: "button",
                  class:
                    "sidebar-row-icon inline-flex items-center justify-center w-6 h-6 bg-transparent border-none cursor-pointer text-secondary rounded-md hover:text-primary hover:bg-fill-hover",
                  title: "Open in new window",
                  tabindex: -1,
                  "data-open-new": workspace.id,
                  onclick: (event: MouseEvent) => {
                    event.stopPropagation();
                    onOpenNew?.(workspace.id);
                  },
                },
                m(
                  "svg",
                  { class: "w-4 h-4", viewBox: "0 0 16 16", fill: "currentColor" },
                  m.trust(ICONS_16["arrow-up-right"]),
                ),
              )
            : null,
        ],
      );
    },
  };
}
