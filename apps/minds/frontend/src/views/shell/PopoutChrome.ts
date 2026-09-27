// The bar across the top of a pulled-out window (the pull-out-window spec,
// section 4.3): the workspace's accent, the workspace name and the window's
// title, "Return to desktop", and a close control. It stands in for the
// titlebar on the /popout route; the workspace shell under it shows one
// window edge to edge, so this bar is the only chrome the popout has.
//
// Dragging the bar moves the OS window: a pointer-capture handler tells main
// where the bar was grabbed and main follows the cursor until the release
// (Electron cannot hand a native drag to a renderer-driven move), which is
// also how a drop back onto a main window is detected.

import m from "mithril";
import { Icon12 } from "../components/Icon";
import { TitlebarButton } from "../components/TitlebarButton";
import { CHROME_BAR_CLASS, CHROME_BAR_STYLE } from "./Titlebar";
import { electronBridge } from "../../electron-bridge";
import type { ShellState } from "./shell-state";

export interface PopoutChromeAttrs {
  shell: ShellState;
  workspaceAnyId: string;
  windowId: string;
}

// A press travels this far before it is a drag rather than a click.
const DRAG_THRESHOLD_PX = 4;

/** Whether a press on the bar starts a drag: not on a control, and only in
 * the desktop app (a plain browser has no OS window to move). */
export function isBarDragPress(target: EventTarget | null, isDesktop: boolean): boolean {
  if (!isDesktop) return false;
  return !(target instanceof Element && target.closest("button") !== null);
}

export function PopoutChrome(): m.Component<PopoutChromeAttrs> {
  let pressPoint: { x: number; y: number } | null = null;
  let isDragging = false;

  function endDrag(): void {
    pressPoint = null;
    if (!isDragging) return;
    isDragging = false;
    electronBridge.endPopoutBarDrag();
  }

  return {
    view(vnode) {
      const { shell, workspaceAnyId, windowId } = vnode.attrs;
      const accentEntry = shell.stores.workspaces.accentEntry(workspaceAnyId);
      const workspaceName = accentEntry?.name ?? "…";
      const windowTitle = shell.popoutWindowTitle(windowId);
      // The same self-theming recipe as the titlebar, and only once an accent
      // is painted: the recipe derives its contrast from --titlebar-bg.
      const surfaceClass = accentEntry?.accent ? "titlebar-surface " : "";
      return m(
        "div#minds-popout-bar",
        {
          style: CHROME_BAR_STYLE,
          class: surfaceClass + CHROME_BAR_CLASS + " cursor-grab",
          "data-popout-window-id": windowId,
          onpointerdown: (event: PointerEvent) => {
            if (event.button !== 0) return;
            if (!isBarDragPress(event.target, electronBridge.isDesktop)) return;
            pressPoint = { x: event.clientX, y: event.clientY };
            (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
          },
          onpointermove: (event: PointerEvent) => {
            if (pressPoint === null || isDragging) return;
            const travelled = Math.hypot(event.clientX - pressPoint.x, event.clientY - pressPoint.y);
            if (travelled < DRAG_THRESHOLD_PX) return;
            isDragging = true;
            // Frameless: the page's origin is the window's, so the press
            // point is where inside the window the bar is held.
            electronBridge.beginPopoutDrag({ grabX: pressPoint.x, grabY: pressPoint.y });
          },
          onpointerup: endDrag,
          onpointercancel: endDrag,
        },
        [
          m("div", { class: "flex-1 flex items-center gap-1 min-w-0 pl-2" }, [
            m(
              "span#popout-workspace-name",
              { class: "type-label text-secondary truncate max-w-[180px] shrink-0" },
              workspaceName,
            ),
            m(
              "span",
              { class: "type-label text-tertiary px-0.5", "aria-hidden": "true" },
              "/",
            ),
            m(
              "span#popout-window-title",
              { class: "type-label text-primary truncate min-w-0" },
              windowTitle,
            ),
          ]),
          m("div", { class: "flex items-center justify-end shrink-0 gap-1 pr-1" }, [
            m(
              TitlebarButton,
              {
                id: "popout-return-btn",
                variant: "crumb",
                tone: "muted",
                "aria-label": "Return to desktop",
                "data-tooltip": "Return this window to the workspace desktop",
                // The same way back as the close control: main returns the
                // window to the desktop through a desktop window showing the
                // workspace before this window goes. Off the desktop app the
                // popout's own shell takes it back and the page stays.
                onclick: () => {
                  if (electronBridge.isDesktop) electronBridge.close();
                  else void shell.returnPopoutToDesktop(null);
                },
              },
              m("span", { class: "type-label" }, "Return to desktop"),
            ),
            // Every popout is frameless, on macOS too, so the bar carries the
            // close control on every platform.
            m(
              TitlebarButton,
              {
                variant: "control",
                tone: "danger",
                id: "popout-close-btn",
                "aria-label": "Close",
                "data-tooltip": "Close (returns the window to the desktop)",
                hidden: !electronBridge.isDesktop,
                onclick: () => electronBridge.close(),
              },
              m(Icon12, { name: "close" }),
            ),
          ]),
        ],
      );
    },
  };
}
