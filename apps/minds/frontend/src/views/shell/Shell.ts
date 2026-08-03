// The persistent app shell: titlebar + switcher popover + the routed page
// body. Two body modes, matching ChromeShell.jinja:
//
// - Local page: content scrolls inside #local-page-scroll, the inset white
//   card below the fixed titlebar (accent bleeds around it).
// - Agent content surface (/workspace/<id>): the page IS the fixed iframe
//   surface; no scroll container. The options route (/workspace/<id>/options)
//   keeps the frame mounted and floats the routed content over it as a
//   dismissable overlay panel (the SPA heir of the legacy docked
//   WorkspaceOptionsModal).

import m from "mithril";
import type { ShellState } from "./shell-state";
import { isWorkspaceOverlayPath } from "./classify";
import { SidebarMenu } from "./SidebarMenu";
import { Titlebar } from "./Titlebar";
import { WorkspaceFrame } from "./WorkspaceFrame";

interface WorkspaceOverlayAttrs {
  shell: ShellState;
}

/** The floating options panel over the workspace surface: a click-away
 * backdrop below the titlebar (so the titlebar tabs stay clickable) and a
 * centered scrollable card. Esc and backdrop clicks dismiss back to the
 * bare workspace route. */
function WorkspaceOverlay(): m.Component<WorkspaceOverlayAttrs> {
  let onKeyDown: ((event: KeyboardEvent) => void) | null = null;

  return {
    oncreate(vnode) {
      onKeyDown = (event: KeyboardEvent) => {
        if (event.key !== "Escape") return;
        if (vnode.attrs.shell.closeWorkspaceOverlay()) {
          event.stopPropagation();
          m.redraw();
        }
      };
      document.addEventListener("keydown", onKeyDown);
    },
    onremove() {
      if (onKeyDown !== null) document.removeEventListener("keydown", onKeyDown);
      onKeyDown = null;
    },
    view(vnode) {
      const { shell } = vnode.attrs;
      return m(
        "div#workspace-overlay-backdrop",
        {
          class:
            "fixed left-0 right-0 top-[38px] bottom-0 z-[90] bg-black/20 " +
            "flex items-center justify-center p-4",
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) shell.closeWorkspaceOverlay();
          },
        },
        m(
          "div#workspace-overlay-panel",
          {
            class:
              "w-[720px] max-w-full max-h-full min-h-0 flex flex-col " +
              "rounded-[12px] border border-subtle bg-surface-primary " +
              "shadow-overlay overflow-y-auto px-6 py-4",
          },
          vnode.children,
        ),
      );
    },
  };
}

export interface ShellAttrs {
  shell: ShellState;
  routePath: string;
  workspaceParam: string | null;
  content: m.Children;
}

export function Shell(): m.Component<ShellAttrs> {
  return {
    view(vnode) {
      const { shell, routePath, workspaceParam, content } = vnode.attrs;
      const isAgentSurface = workspaceParam !== null;
      // The visual-diff harness captures with ?visual-diff=1 and no live
      // channel; suppress the indicator so screenshots stay deterministic.
      const isCaptureMode = new URLSearchParams(window.location.search).has("visual-diff");
      const isReconnecting = (shell.channel?.isVisiblyReconnecting ?? false) && !isCaptureMode;

      return m("div", { style: "display: contents" }, [
        m(Titlebar, { shell, routePath }),
        m(SidebarMenu, { shell }),
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
        m(
          "div#local-page-root",
          { style: "display: contents" },
          isAgentSurface && workspaceParam !== null
            ? [
                m(WorkspaceFrame, { shell, workspaceAnyId: workspaceParam }),
                isWorkspaceOverlayPath(routePath) ? m(WorkspaceOverlay, { shell }, content) : null,
              ]
            : m(
                "div#local-page-scroll",
                { class: "bg-surface-primary overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable]" },
                content,
              ),
        ),
      ]);
    },
  };
}
