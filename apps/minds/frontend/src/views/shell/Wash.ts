// The wash layer: the workspace's accent color growing out of the creation
// page's loading box until it covers the window, then lifting off the finished
// workspace. Mounted by the Shell over everything else, titlebar and modals
// included, because the whole window has to be covered while the screen
// changes underneath it (see models/wash.ts).

import m from "mithril";
import { WASH_DISC_PX, WASH_TOTAL_MS, wash } from "../../models/wash";

export function Wash(): m.Component {
  return {
    view() {
      const active = wash.active;
      if (active === null) return null;
      return m(
        "div",
        { id: "wash-layer", class: "pointer-events-none fixed inset-0 z-[150] overflow-hidden" },
        m("div", {
          class: "wash-disc absolute rounded-full",
          style:
            `background-color: ${active.accent}; width: ${WASH_DISC_PX}px; height: ${WASH_DISC_PX}px; ` +
            `left: ${active.origin.x}px; top: ${active.origin.y}px; ` +
            `--wash-scale: ${active.scale}; --wash-ms: ${WASH_TOTAL_MS}ms;`,
        }),
      );
    },
  };
}
