import m from "mithril";
import { isSlotClaimed, getSlotRenderCallback } from "../slots";
import { runHook } from "../hooks";
import { AgentSelector } from "./ConversationSelector";
import { getSidebarItems } from "../sidebar-items";
import type { SidebarItemDefinition } from "../sidebar-items";

function invokeSlotRendered(slotName: string, container: HTMLElement): void {
  const renderCallback = getSlotRenderCallback(slotName);
  if (renderCallback) {
    renderCallback(container);
  }
  runHook("slot_rendered", { slotName, container });
}

const ICON_PANEL_LEFT_CLOSE = '<path d="M3 3h18v18H3z"/><path d="M9 3v18"/><path d="M16 9l-3 3 3 3"/>';
const ICON_PANEL_LEFT_OPEN = '<path d="M3 3h18v18H3z"/><path d="M9 3v18"/><path d="M14 9l3 3-3 3"/>';

const SIDEBAR_COLLAPSED_KEY = "sidebar-collapsed";

let collapsed = localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";

function toggle(): void {
  collapsed = !collapsed;
  localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed));
}

function inlineSvg(svgPath: string, className?: string): m.Vnode {
  return m.trust(
    `<svg${className ? ` class="${className}"` : ""} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${svgPath}</svg>`,
  );
}

function collapsedIconButton(label: string, onclick: () => void, svgPath: string): m.Vnode {
  return m(
    "button",
    {
      class: "sidebar-collapsed-icon-button",
      onclick,
      "aria-label": label,
      title: label,
    },
    inlineSvg(svgPath),
  );
}

function collapsedSidebarItemIcon(item: SidebarItemDefinition): m.Vnode {
  return collapsedIconButton(item.name, () => m.route.set(item.route), item.icon);
}

function actionRow(label: string, onclick: () => void, svgPath: string): m.Vnode {
  return m(
    "a",
    {
      class: "sidebar-action-row",
      href: "javascript:void(0)",
      title: label,
      onclick(event: Event) {
        event.preventDefault();
        onclick();
      },
    },
    [inlineSvg(svgPath, "sidebar-action-row-icon"), m("span", { class: "sidebar-action-row-label" }, label)],
  );
}

function sidebarItemActionRow(item: SidebarItemDefinition): m.Vnode {
  return actionRow(item.name, () => m.route.set(item.route), item.icon);
}

function inlineIconButton(label: string, onclick: () => void, svgPath: string): m.Vnode {
  return m(
    "button",
    {
      class: "sidebar-inline-icon-button",
      onclick,
      "aria-label": label,
      title: label,
    },
    inlineSvg(svgPath),
  );
}

export const Sidebar: m.Component = {
  view() {
    const sidebarClass = ["app-sidebar", collapsed ? "app-sidebar--collapsed" : ""].filter(Boolean).join(" ");

    if (isSlotClaimed("sidebar")) {
      return m("aside", {
        class: sidebarClass,
        "data-slot": "sidebar",
        oncreate(vnode: m.VnodeDOM) {
          invokeSlotRendered("sidebar", vnode.dom as HTMLElement);
        },
      });
    }

    return m("aside", { class: sidebarClass, "data-slot": "sidebar" }, [
      m("div", { class: "sidebar-collapsed-content" }, [
        collapsedIconButton("Expand sidebar", toggle, ICON_PANEL_LEFT_OPEN),
        ...getSidebarItems().map(collapsedSidebarItemIcon),
      ]),
      m("div", { class: "sidebar-expanded-content flex flex-col flex-1 min-h-0" }, [
        m(
          "div",
          {
            "data-slot": "sidebar-header",
            oncreate(vnode: m.VnodeDOM) {
              if (isSlotClaimed("sidebar-header")) {
                invokeSlotRendered("sidebar-header", vnode.dom as HTMLElement);
              }
            },
          },
          isSlotClaimed("sidebar-header")
            ? null
            : [
                m("div", { class: "sidebar-branding-row" }, [
                  m(
                    "div",
                    {
                      class: "sidebar-branding",
                      "data-slot": "sidebar-branding",
                      oncreate(vnode: m.VnodeDOM) {
                        if (isSlotClaimed("sidebar-branding")) {
                          invokeSlotRendered("sidebar-branding", vnode.dom as HTMLElement);
                        }
                      },
                      onbeforeupdate() {
                        return !isSlotClaimed("sidebar-branding");
                      },
                    },
                    isSlotClaimed("sidebar-branding")
                      ? null
                      : m("span", { class: "sidebar-branding-title" }, "Claude Web Chat"),
                  ),
                  inlineIconButton("Collapse sidebar", toggle, ICON_PANEL_LEFT_CLOSE),
                ]),
                m("div", { class: "sidebar-action-rows" }, [
                  ...getSidebarItems().map(sidebarItemActionRow),
                ]),
              ],
        ),
        m(AgentSelector),
      ]),
    ]);
  },
};
