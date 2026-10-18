import m from "mithril";
import { electronBridge } from "../../electron-bridge";

/** The lights sit where macOS puts its own (main.js places the native ones
 * 12px in), in the same 72px the bar leaves for those. */
const CLUSTER_CLASS = "flex items-center gap-2 pl-3 w-[72px] shrink-0";
const LIGHT_CLASS = "w-3 h-3 rounded-full border border-black/15 cursor-default p-0";

/**
 * macOS's traffic lights, drawn by the bar itself: the window controls of a
 * window whose platform has no native ones but is made to look like a Mac
 * (the demo box). Each light drives the same window control as the bar's own
 * buttons do elsewhere.
 */
export function TrafficLights(): m.Component {
  return {
    view() {
      return m("div#traffic-lights", { class: CLUSTER_CLASS }, [
        m("button", {
          type: "button",
          id: "traffic-light-close",
          "aria-label": "Close",
          class: LIGHT_CLASS + " bg-[#ff5f57]",
          onclick: () => electronBridge.close(),
        }),
        m("button", {
          type: "button",
          id: "traffic-light-minimize",
          "aria-label": "Minimize",
          class: LIGHT_CLASS + " bg-[#febc2e]",
          onclick: () => electronBridge.minimize(),
        }),
        m("button", {
          type: "button",
          id: "traffic-light-maximize",
          "aria-label": "Maximize",
          class: LIGHT_CLASS + " bg-[#28c840]",
          onclick: () => electronBridge.maximize(),
        }),
      ]);
    },
  };
}
