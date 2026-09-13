// Shared SVG <symbol> defs for the onboarding walkthrough's graphics, port
// of the <symbol>/<use> sprite sheet at the top of the legacy
// Creating.jinja's #graphic block. There is no earlier <symbol>/<use>
// precedent in this codebase (the Icon16/Icon12 catalog renders each glyph
// into its own <svg> via m.trust instead) -- symbol ids only need to exist
// once, anywhere in the document, for <use href="#id"/> to find them from
// wherever a graphic references one, so this component is mounted exactly
// once by OnboardingWalkthrough, as a sibling of the per-step graphics
// rather than inside any of them, so it never remounts as steps change.
import m from "mithril";

export const Symbols: m.ClosureComponent = () => ({
  view: () =>
    m("svg", { width: "0", height: "0", "aria-hidden": "true", class: "absolute" }, [
      // The minds mark, defined once and referenced where it is shown.
      m("symbol", { id: "minds-mark", viewBox: "0 0 1024 1024" }, [
        m("rect", { width: "1024", height: "1024", rx: "200", fill: "#492222" }),
        m("path", {
          d: "M239.393 346.233C213.333 388.701 181.957 578.826 170.21 692.964C168.726 707.387 179.615 719.956 194.083 720.906L243.5 724.15C254.832 724.894 264.36 732.926 267.01 743.968L287.114 827.735C289.477 837.581 297.325 845.189 307.312 846.857C387.978 860.334 533.31 868.318 620.93 820.192C992.068 616.341 865.659 254.427 642.258 194.566C418.857 134.706 303.377 241.962 239.393 346.233Z",
          fill: "#E9ECD9",
        }),
        m("path", {
          d: "M719.991 411.97C744.539 411.97 762.39 420.422 774.234 434.21C786.236 448.181 792.898 468.606 792.898 493.876C792.898 542.499 753.481 581.916 704.858 581.916C656.236 581.916 616.819 542.499 616.819 493.876C616.819 470.597 629.516 450.292 649.247 435.491C669.022 420.657 695.154 411.97 719.991 411.97Z",
          stroke: "#492222",
          "stroke-width": "17.28",
          fill: "none",
        }),
        m("path", {
          d: "M583.295 282.074C621.147 282.074 649.061 294.27 667.574 314.33C686.184 334.495 696.216 363.607 696.216 399.073C696.216 467.817 636.155 524.583 560.738 524.583C485.322 524.583 425.26 467.817 425.26 399.073C425.26 365.723 444.83 336.66 475.056 315.555C505.279 294.451 545.21 282.074 583.295 282.074Z",
          stroke: "#492222",
          "stroke-width": "17.28",
          fill: "none",
        }),
        m("path", {
          d: "M727.416 125.17C765.268 125.17 793.181 137.366 811.695 157.426C830.305 177.591 840.336 206.703 840.336 242.169C840.336 310.913 780.275 367.679 704.859 367.679C629.442 367.679 569.38 310.913 569.38 242.169C569.38 208.819 588.951 179.756 619.176 158.651C649.4 137.547 689.331 125.17 727.416 125.17Z",
          stroke: "#E9ECD9",
          "stroke-width": "17.28",
          fill: "none",
        }),
      ]),
      // A miniature of the app pane, so the laptop and the phone show the
      // same interface rather than a logo.
      m("symbol", { id: "app-ui", viewBox: "0 0 100 64" }, [
        m("rect", { x: "6", y: "6", width: "11", height: "11", rx: "3", fill: "currentColor", opacity: "0.75" }),
        m("rect", { x: "22", y: "9", width: "30", height: "5", rx: "2.5", fill: "currentColor", opacity: "0.18" }),
        m("rect", {
          x: "74",
          y: "6",
          width: "9",
          height: "9",
          rx: "2.5",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "1.5",
          opacity: "0.35",
          "vector-effect": "non-scaling-stroke",
        }),
        m("rect", { x: "86", y: "6", width: "9", height: "9", rx: "2.5", fill: "currentColor", opacity: "0.75" }),
        m("rect", { x: "6", y: "26", width: "27", height: "17", rx: "3", fill: "currentColor", opacity: "0.18" }),
        m("rect", { x: "37", y: "26", width: "27", height: "17", rx: "3", fill: "currentColor", opacity: "0.18" }),
        m("rect", { x: "68", y: "26", width: "27", height: "17", rx: "3", fill: "currentColor", opacity: "0.32" }),
        m("rect", { x: "6", y: "50", width: "24", height: "9", rx: "4", fill: "currentColor", opacity: "0.75" }),
        m("rect", {
          x: "34",
          y: "50",
          width: "24",
          height: "9",
          rx: "4",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "1.5",
          opacity: "0.35",
          "vector-effect": "non-scaling-stroke",
        }),
      ]),
      // The machine, drawn the same way wherever it appears.
      m("symbol", { id: "laptop", viewBox: "0 0 168 116" }, [
        m("use", { href: "#laptop-shell" }),
        m("use", { href: "#app-ui", x: "28", y: "16", width: "112", height: "70" }),
      ]),
      // The machine on its own, for where the app inside is drawn separately
      // (so it can be tinted without tinting the machine).
      m("symbol", { id: "laptop-shell", viewBox: "0 0 168 116" }, [
        m("rect", {
          x: "18",
          y: "6",
          width: "132",
          height: "88",
          rx: "8",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "2.5",
          "vector-effect": "non-scaling-stroke",
        }),
        m("path", {
          d: "M6 106 H162",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "2.5",
          "stroke-linecap": "round",
          "vector-effect": "non-scaling-stroke",
        }),
      ]),
      // A simpler app than #app-ui, for the publishing step: there the point
      // is that two copies match, not what is in them.
      m("symbol", { id: "publish-app", viewBox: "0 0 100 64" }, [
        m("rect", { x: "8", y: "8", width: "34", height: "8", rx: "4", fill: "currentColor", opacity: "0.85" }),
        m("rect", { x: "8", y: "26", width: "84", height: "14", rx: "4", fill: "currentColor", opacity: "0.3" }),
        m("rect", { x: "8", y: "48", width: "30", height: "10", rx: "5", fill: "currentColor", opacity: "0.85" }),
      ]),
    ]),
});
