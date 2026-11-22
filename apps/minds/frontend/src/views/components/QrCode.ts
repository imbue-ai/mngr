import m from "mithril";
import { QR_QUIET_ZONE_MODULES, qrModules, qrPathData } from "../../models/qrCode";

/** The code's side, as the class a row beside it uses to be at least as tall as the code. */
export const QR_CODE_ROW_MIN_HEIGHT_CLASS = "min-h-24";

interface QrCodeAttrs extends m.Attributes {
  /** What the code opens; null draws the empty box a code will appear in. */
  value: string | null;
  /** What a screen reader calls it. */
  label: string;
}

/** A QR code of a link, or the empty box one will appear in. Always black on white, whatever the colour scheme: a
 * camera reads the contrast, not the theme. */
export function QrCode(): m.Component<QrCodeAttrs> {
  // The modules of the value last drawn: a redraw with the same link draws the same code without encoding it again.
  let drawnValue: string | null = null;
  let pathData = "";
  let sideModules = 0;
  return {
    view(vnode) {
      const { value, label } = vnode.attrs;
      const boxClass = "shrink-0 w-24 h-24 rounded-md border border-subtle";
      if (value === null) {
        return m("div", { "data-qr-code": "empty", "aria-hidden": "true", class: `${boxClass} bg-fill-subtle` });
      }
      if (value !== drawnValue) {
        const modules = qrModules(value);
        pathData = qrPathData(modules);
        sideModules = modules.length + 2 * QR_QUIET_ZONE_MODULES;
        drawnValue = value;
      }
      return m(
        "svg",
        {
          "data-qr-code": "ready",
          role: "img",
          "aria-label": label,
          class: `${boxClass} bg-white`,
          viewBox: `0 0 ${sideModules} ${sideModules}`,
          "shape-rendering": "crispEdges",
        },
        m("path", { d: pathData, fill: "#000000" }),
      );
    },
  };
}
