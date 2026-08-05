// One pure function per walkthrough step's graphic, port of the `.gfx`
// blocks in the legacy Creating.jinja. Each function returns the scene's
// vnode tree only -- OnboardingWalkthrough.ts owns when a scene mounts (see
// its header comment on why a fresh `key` per visit replaces the legacy
// remove-class/force-reflow/re-add-class dance for restarting a CSS
// animation on a *persistent* node: a freshly created node always starts
// its animations from the beginning, in every browser, with no such dance
// needed).
import m from "mithril";
import type { CloudWheel } from "../../../models/walkthrough";
import { WHEEL_POSITIONS } from "../../../models/walkthrough";
import { Icon16 } from "../../components/Icon";

// The cloud silhouette shared by the apps, connections and publishing
// steps. The viewBox is padded past the path's own bounds: drawn tight,
// half the outline falls outside it and is clipped away, which is what left
// the top, bottom and sides looking thinner than the rest. The aspect ratio
// is kept, too -- stretching it flattened the curves into ovals.
function cloudSvg(): m.Children {
  return m(
    "svg",
    { viewBox: "-1 3 26 18", class: "absolute inset-0 w-full h-full", "aria-hidden": "true" },
    m("path", {
      d: "M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z",
      fill: "var(--c-fill-subtle)",
      stroke: "var(--gfx-ink-soft)",
      "stroke-width": "2.5",
    }),
  );
}

// One big icon centered with a smaller one either side, spinning left ->
// center -> right; the arriving app's name pops in below it. Shared by the
// apps step and the connections step, each driven by its own CloudWheel.
function cloudWheelEl(wheel: CloudWheel): m.Children {
  // A flat array, not a nested one: Mithril requires every vnode in a given
  // children array to either all carry a key or none do, and a raw .map()
  // result nested as one array *element* gets wrapped in an unkeyed
  // fragment vnode, tripping that check against the keyed <span> beside it.
  return m("div", { class: "cloud-wheel" }, [
    ...wheel.renderItems.map((item) =>
      m("img", {
        key: item.id,
        class: "cloud-wheel-item " + WHEEL_POSITIONS[item.position],
        alt: "",
        src: item.url,
      }),
    ),
    m(
      "span",
      // A fresh key on every centered app restarts the name's pop-in.
      { class: "cloud-wheel-name is-shown", key: wheel.centerName, "aria-hidden": "true" },
      wheel.centerName,
    ),
  ]);
}

/** Step 1: the minds mark. */
export function mindsGraphic(key: string): m.Children {
  return m(
    "div",
    { key, id: "gfx-minds", class: "gfx flex flex-col items-center gap-2", "data-tooltip": "The app you are in now. It runs on your computer." },
    [
      m("svg", { viewBox: "0 0 1024 1024", class: "w-40 h-40 rounded-xl", "aria-label": "minds" }, m("use", { href: "#minds-mark" })),
      m("span", { class: "type-helper text-tertiary" }, "minds"),
    ],
  );
}

/**
 * Machine step: where the machine itself runs. A local machine is this very
 * laptop (the star on its screen marks it as the machine, not just a
 * computer); a remote one is a rack in Imbue's cloud, with this laptop
 * reaching it over a live connection.
 */
export function machineGraphic(key: string, isRemote: boolean): m.Children {
  if (isRemote) {
    return m("div", { key, id: "gfx-machine", class: "gfx" }, [
      m("div", { class: "flex items-end justify-center gap-4" }, [
        m("div", { class: "flex flex-col items-center gap-2" }, [
          m(
            "svg",
            { viewBox: "0 0 168 116", class: "w-44 h-32", "aria-label": "this computer", "data-tooltip": "This computer, where you work." },
            m("use", { href: "#laptop" }),
          ),
          m("span", { class: "type-helper text-tertiary" }, "Your device"),
        ]),
        // The link runs both ways: the device asks, the machine answers.
        m(
          "svg",
          {
            viewBox: "0 0 64 24",
            class: "w-16 h-6 mb-[84px] gfx-arrow",
            fill: "none",
            stroke: "currentColor",
            "aria-hidden": "true",
            "data-tooltip": "Your device talks to it over this connection.",
          },
          [
            m("path", { d: "M16 12 H48", "stroke-width": "2.5", "stroke-linecap": "round" }),
            m("path", { d: "M4 12 L17 6 L17 18 Z", fill: "currentColor", stroke: "none" }),
            m("path", { d: "M60 12 L47 6 L47 18 Z", fill: "currentColor", stroke: "none" }),
          ],
        ),
        m("div", { class: "flex flex-col items-center gap-2" }, [
          // A server rack, drawn in the machine's language: the same
          // 116-unit height as the laptop, its base on the laptop's
          // baseline, three units separated by 1.5px lines, each with a
          // vent line and an indicator dot.
          m(
            "svg",
            {
              viewBox: "0 0 96 116",
              class: "w-[96px] h-32",
              "aria-label": "your machine in Imbue’s cloud",
              "data-tooltip": "Your machine, hosted in Imbue’s secure cloud.",
            },
            [
              m("rect", { x: "12", y: "6", width: "72", height: "100", rx: "8", fill: "none", stroke: "currentColor", "stroke-width": "2.5" }),
              m("path", { d: "M12 39.3 H84 M12 72.7 H84", fill: "none", stroke: "currentColor", "stroke-width": "1.5" }),
              m("path", { d: "M24 22.7 H50 M24 56 H50 M24 89.3 H50", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round" }),
              m("circle", { cx: "70", cy: "22.7", r: "2.5", fill: "currentColor" }),
              m("circle", { cx: "70", cy: "56", r: "2.5", fill: "currentColor" }),
              m("circle", { cx: "70", cy: "89.3", r: "2.5", fill: "currentColor" }),
            ],
          ),
          m("span", { class: "type-helper text-tertiary" }, "Imbue’s secure cloud"),
        ]),
      ]),
    ]);
  }
  // A local machine is simply this laptop, so it stands alone: the shell
  // without the miniature interface, a star centered on the screen instead.
  return m("div", { key, id: "gfx-machine", class: "gfx" }, [
    m(
      "svg",
      { viewBox: "0 0 168 116", class: "w-44 h-32", "aria-label": "your machine on this computer", "data-tooltip": "Your machine runs right here, on this computer." },
      [
        m("use", { href: "#laptop-shell" }),
        m("path", {
          d: "M84 26 L90.5 43.1 L108.7 44 L94.5 55.4 L99.3 73 L84 63 L68.7 73 L73.5 55.4 L59.3 44 L77.5 43.1 Z",
          fill: "none",
          stroke: "currentColor",
          "stroke-width": "2.5",
          "stroke-linejoin": "round",
        }),
      ],
    ),
  ]);
}

/**
 * Chat step: one exchange, played out. The request types itself out,
 * backspaces, and tries other things to build; `typedText` is driven by a
 * ChatTypewriter held in OnboardingWalkthrough's state.
 */
export function chatGraphic(key: string, typedText: string): m.Children {
  return m("div", { key, id: "gfx-chat", class: "gfx is-playing w-full max-w-md flex flex-col gap-3" }, [
    m(
      "div",
      { class: "flex justify-end" },
      m("div", { class: "chat-bubble chat-bubble-user rounded-xl", "data-tooltip": "Ask for whatever you want, in plain language." }, [
        m("span", { id: "chat-typed" }, typedText),
        m("span", { class: "chat-caret", "aria-hidden": "true" }),
        // Holds the width of the longest request the typing will reach, so
        // the bubble does not grow and shrink under the text as one option
        // is swapped for another.
        m("span", { class: "chat-reserve", "aria-hidden": "true" }, "I want you to filter my emails."),
      ]),
    ),
    m(
      "div",
      { class: "flex justify-start" },
      m("div", { class: "chat-bubble chat-bubble-agent rounded-xl chat-reply", "data-tooltip": "Your agent builds it -- interface and all." }, "You’ve got it!"),
    ),
  ]);
}

const BAR_HEIGHTS = [38, 62, 48, 80, 58, 92, 70];

/**
 * Browser/tabs step: the agent pulls the dashboard up beside the chat --
 * the chat column narrows, the app opens in the right half behind a
 * divider, and its tab joins the strip already selected. `isForming`
 * (arriving from the chat step) plays the window materializing around the
 * conversation that was already on screen; jumping here directly (a dot
 * click) skips that entrance and shows the window already formed.
 */
export function browserGraphic(key: string, isForming: boolean): m.Children {
  return m("div", { key, id: "gfx-browser", class: "gfx w-full max-w-xl" + (isForming ? " is-forming" : "") }, [
    m("div", { id: "theme-demo", class: "relative w-full rounded-xl overflow-hidden border border-subtle shadow-overlay text-left is-split" }, [
      m("div", { id: "demo-titlebar", class: "demo-titlebar relative flex items-center" }, [
        m("div", { class: "flex gap-1.5 px-3" }, [
          m("span", { class: "demo-dot w-2.5 h-2.5 rounded-full" }),
          m("span", { class: "demo-dot w-2.5 h-2.5 rounded-full" }),
          m("span", { class: "demo-dot w-2.5 h-2.5 rounded-full" }),
        ]),
        m("span", { class: "demo-tab demo-tab-active", "data-tab": "chat", "data-tooltip": "Talk to an agent and watch it work." }, "Chat"),
        m("span", { class: "demo-tab demo-tab-pending", "data-tab": "app", "data-tooltip": "An app your agent built, in its own tab." }, "My app"),
      ]),
      m("div", { class: "demo-body p-4 h-[212px] overflow-hidden flex gap-3" }, [
        // The very bubbles from the chat step, at a smaller size, so the
        // window can simply form around them rather than anything having
        // to line up. When the app opens, this column narrows to make room
        // beside it.
        m(
          "div",
          { class: "demo-pane demo-chat-col flex flex-col gap-2", "data-pane": "chat", "data-tooltip": "Ask in plain language; the agent does the work." },
          [
            m("div", { class: "flex justify-end" }, m("div", { id: "demo-request", class: "chat-bubble chat-bubble-user chat-bubble-sm rounded-xl" }, "I want to build a dashboard.")),
            m("div", { class: "flex justify-start" }, m("div", { class: "chat-bubble chat-bubble-agent chat-bubble-sm rounded-xl" }, "You’ve got it!")),
            // New on this screen (the chat step ends on "You've got it"),
            // so it lands with a small entrance as the app opens.
            m(
              "div",
              { class: "flex justify-start" },
              m("div", { class: "chat-bubble chat-bubble-agent chat-bubble-sm rounded-xl demo-bubble-in" }, "Pulling it up in a new tab."),
            ),
          ],
        ),
        // The divider between the halves: each pane gets its own half, and
        // its own tab above.
        m("span", { class: "demo-split-divider", "aria-hidden": "true" }),
        // The app the agent built from that request: a small dashboard
        // pulled up beside the chat. The two charts pop up as the pane
        // mounts; everything inside the window is tinted by the workspace
        // accent via color-mix, so the colour reads as the app's content
        // rather than the window's chrome.
        m("div", { class: "demo-pane demo-app-col", "data-pane": "app", "data-tooltip": "Agents build and run real apps, not just answers." }, [
          m("div", { class: "flex items-center gap-2 mb-3" }, [
            m("span", { class: "demo-fill h-2 rounded w-16" }),
            m("span", { class: "w-4 h-4 rounded-md inline-block ml-auto", style: "background-color: color-mix(in srgb, var(--demo-accent) 55%, transparent);" }),
          ]),
          m("div", { class: "flex gap-3" }, [
            // Bar chart: seven bars at fixed heights, the tallest picked
            // out in the stronger accent tone the header chip carries.
            m("div", { class: "demo-chart demo-card rounded-lg p-3 flex-1 flex flex-col gap-2" }, [
              m("span", { class: "demo-fill h-1.5 rounded w-12" }),
              m(
                "div",
                { class: "flex items-end gap-1.5 h-[96px]" },
                BAR_HEIGHTS.map((height, index) =>
                  m("span", {
                    key: index,
                    class: "demo-chart-bar flex-1 rounded-sm",
                    style: `height: ${height}px; background-color: color-mix(in srgb, var(--demo-accent) ${index === 5 ? 55 : 30}%, transparent);`,
                  }),
                ),
              ),
            ]),
            // Line chart: the line draws itself on, the area under it
            // fades in, and the endpoint dot pops. The line's stroke-width
            // lives in CSS rather than an attribute on purpose: the
            // #graphic non-scaling-stroke rule keys on the attribute, and
            // non-scaling-stroke breaks the pathLength dash normalization
            // the draw-on animation relies on. The gridlines are static, so
            // they keep the attribute and stay a uniform 1px.
            m("div", { class: "demo-chart demo-card rounded-lg p-3 flex-1 flex flex-col gap-2" }, [
              m("span", { class: "demo-fill h-1.5 rounded w-12" }),
              m("svg", { class: "w-full h-[96px]", viewBox: "0 0 240 96", preserveAspectRatio: "none", "aria-hidden": "true" }, [
                m("path", { d: "M0 30 H240 M0 62 H240", fill: "none", stroke: "var(--demo-border)", "stroke-width": "1" }),
                m("path", {
                  class: "demo-chart-area",
                  d: "M8 78 L46 60 L84 68 L122 44 L160 52 L198 28 L232 18 L232 96 L8 96 Z",
                  style: "fill: color-mix(in srgb, var(--demo-accent) 14%, transparent);",
                }),
                m("path", {
                  class: "demo-chart-line",
                  pathLength: "1",
                  d: "M8 78 L46 60 L84 68 L122 44 L160 52 L198 28 L232 18",
                  style: "stroke: color-mix(in srgb, var(--demo-accent) 55%, transparent);",
                }),
                m("circle", {
                  class: "demo-chart-dot",
                  cx: "232",
                  cy: "18",
                  r: "3.5",
                  style: "fill: color-mix(in srgb, var(--demo-accent) 55%, transparent);",
                }),
              ]),
            ]),
          ]),
        ]),
      ]),
    ]),
  ]);
}

/** Apps step: the cloud of services agents can reach. */
export function appsGraphic(key: string, wheel: CloudWheel): m.Children {
  return m("div", { key, id: "gfx-apps", class: "gfx" }, [
    m("div", { class: "app-cloud relative w-[360px] h-[240px]", "data-tooltip": "Apps your agents can connect to." }, [cloudSvg(), cloudWheelEl(wheel)]),
  ]);
}

/**
 * Connections step: a permission request sits on the left; a pointer
 * approves it, and the green button then travels across and becomes the
 * link joining the cloud of apps to the machine.
 */
export function connectGraphic(key: string, wheel: CloudWheel): m.Children {
  return m("div", { key, id: "gfx-connect", class: "gfx is-playing" }, [
    m("div", { class: "connect-scene" }, [
      m("div", { class: "connect-card", "data-tooltip": "A permission request: agents act only on what you approve." }, [
        m("span", { class: "connect-card-title" }, "Permission request"),
        m("span", { class: "connect-card-line" }),
        m("span", { class: "connect-card-line is-short" }),
      ]),
      m("div", { class: "connect-cloud app-cloud", "data-tooltip": "Apps your agents can connect to." }, [cloudSvg(), cloudWheelEl(wheel)]),
      // The boundary the machine cannot cross on its own. It is there from
      // the start; approving is what opens a way through it.
      m("span", { class: "connect-boundary", "aria-hidden": "true", "data-tooltip": "Nothing reaches your apps until you allow it." }),
      m(
        "svg",
        { class: "connect-laptop", viewBox: "0 0 168 116", "aria-label": "your machine", "data-tooltip": "The machine your agents run on." },
        m("use", { href: "#laptop" }),
      ),
      // The approve button, which becomes the link once clicked.
      m("span", { class: "connect-approve", "aria-hidden": "true" }, [
        m("span", { class: "connect-approve-label" }, "Approve"),
        // Once the link has settled it carries traffic: a light pulse runs
        // up it and another back down, looping while the step shows.
        m("span", { class: "connect-flow" }, [m("span", { class: "connect-flow-pulse is-up" }), m("span", { class: "connect-flow-pulse is-down" })]),
      ]),
      // The other answer: the same request can be turned down, and the
      // link closes again when it is.
      m("span", { class: "connect-deny", "aria-hidden": "true" }, [
        m("span", { class: "connect-deny-label" }, "Deny"),
        m(Icon16, { name: "close", size: "sm", extra: "connect-deny-x" }),
      ]),
      m("span", { class: "connect-splash is-approve", "aria-hidden": "true" }),
      m("span", { class: "connect-splash is-deny", "aria-hidden": "true" }),
      m(
        "span",
        { class: "connect-cursor", "aria-hidden": "true" },
        m(
          "svg",
          { viewBox: "0 0 24 24" },
          m("path", { d: "M1 1 L1 18 L5.5 13.5 L8.5 20 L11 19 L8 12.5 L14.5 12.5 Z", fill: "#ffffff", stroke: "#202020", "stroke-width": "1.5", "stroke-linejoin": "round" }),
        ),
      ),
    ]),
  ]);
}

/** Sharing step: the machine on a laptop, an arrow drawing across to a phone. */
export function devicesGraphic(key: string): m.Children {
  return m("div", { key, id: "gfx-devices", class: "gfx is-playing flex items-end justify-center gap-4" }, [
    m(
      "svg",
      { viewBox: "0 0 168 116", class: "w-44 h-32 text-tertiary", "aria-label": "your machine on a laptop", "data-tooltip": "Your machine, on this computer." },
      m("use", { href: "#laptop" }),
    ),
    m("svg", { viewBox: "0 0 64 24", class: "w-16 h-6 mb-12 gfx-arrow", fill: "none", stroke: "currentColor", "aria-hidden": "true" }, [
      m("path", { class: "devices-arrow-line", d: "M4 12 H40", "stroke-width": "2.5", "stroke-linecap": "round" }),
      m("path", { class: "devices-arrow-head", d: "M52 12 L39 18 L39 6 Z", fill: "currentColor", stroke: "none" }),
    ]),
    m(
      "svg",
      {
        viewBox: "0 0 76 116",
        class: "w-20 h-32 text-tertiary",
        fill: "none",
        stroke: "currentColor",
        "aria-label": "the same machine on a phone",
        "data-tooltip": "The same machine, reached from your phone.",
      },
      [
        m("rect", { x: "10", y: "6", width: "56", height: "104", rx: "10", "stroke-width": "2.5" }),
        m("path", { d: "M31 16 H45", "stroke-width": "1.5", "stroke-linecap": "round" }),
        m("use", { class: "devices-phone-ui", href: "#app-ui", x: "16", y: "32", width: "44", height: "30" }),
      ],
    ),
  ]);
}

/**
 * Publishing step: your machine sends an app up to the cloud and someone
 * else's machine adapts the same app. All three copies are the same
 * simplified app drawing; the machines match and only the app is tinted,
 * so the colour reads as the app rather than the computer.
 */
export function publishGraphic(key: string): m.Children {
  return m("div", { key, id: "gfx-publish", class: "gfx is-playing" }, [
    m("div", { class: "publish-scene" }, [
      // Where a published app lives, in the neutral tone: the copy neither
      // machine has coloured.
      m("div", { class: "publish-cloud", "data-tooltip": "Apps people publish live here, ready for anyone to pick up." }, [
        cloudSvg(),
        m("svg", { class: "publish-cloud-app", viewBox: "0 0 100 64", "aria-label": "the published app" }, m("use", { href: "#publish-app" })),
      ]),
      // Both machines are the same drawing in the same colour: only the
      // app inside is tinted, so the colour reads as the app rather than
      // the computer it is running on.
      m(
        "svg",
        { class: "publish-laptop is-mine", viewBox: "0 0 168 116", "aria-label": "your machine, running an app you built", "data-tooltip": "An app you built on your machine." },
        [m("use", { href: "#laptop-shell" }), m("use", { class: "publish-app is-mine-app", href: "#publish-app", x: "34", y: "20", width: "100", height: "62" })],
      ),
      m(
        "svg",
        {
          class: "publish-laptop is-theirs",
          viewBox: "0 0 168 116",
          "aria-label": "someone else's machine, running the same app",
          "data-tooltip": "The same app, in someone else's colour.",
        },
        [m("use", { href: "#laptop-shell" }), m("use", { class: "publish-app is-theirs-app", href: "#publish-app", x: "34", y: "20", width: "100", height: "62" })],
      ),
      // Both arrows on one overlay, in scene coordinates, so their ends can
      // be aimed at the cloud and the machines directly.
      m("svg", { class: "publish-arrows", viewBox: "0 0 560 268", fill: "none", stroke: "currentColor", "aria-hidden": "true" }, [
        m("path", { class: "publish-arrow-line is-up", d: "M158 184 Q176 140 200 120", "stroke-width": "2.5", "stroke-linecap": "round" }),
        m("path", { class: "publish-arrow-head is-up", d: "M209 113 L203.5 125.5 L195.5 116.5 Z", fill: "currentColor", stroke: "none" }),
        m("path", { class: "publish-arrow-line is-down", d: "M352 114 Q382 138 397 174", "stroke-width": "2.5", "stroke-linecap": "round" }),
        m("path", { class: "publish-arrow-head is-down", d: "M403 185 L392.5 176.5 L402 171 Z", fill: "currentColor", stroke: "none" }),
      ]),
      m("span", { class: "publish-label is-up" }, "publish"),
      m("span", { class: "publish-label is-down" }, "adapt"),
    ]),
  ]);
}

/**
 * Step 9: the tips step has no illustration, so a headline takes the
 * graphic's place -- otherwise the reserved height (which keeps the dot
 * strip still) would sit there blank. The rotating tip itself is the step's
 * copy panel, not this graphic (see tipsCopyPanel below).
 */
export function tipsGraphic(key: string): m.Children {
  return m("div", { key, id: "gfx-tips", class: "gfx flex items-center justify-center px-6" }, [
    m("p", { class: "type-heading-lg text-primary text-center max-w-xl" }, "Hang tight — your machine is nearly ready."),
  ]);
}

/** Step 9's copy panel: the rotating tip, replacing the usual headline + supporting line. */
export function tipsCopyPanel(tipText: string, tipOpacity: 0 | 1): m.Children {
  return m("p", { id: "tip", class: "onboarding-copy text-primary min-h-[18px] transition-opacity", style: `opacity: ${tipOpacity}` }, tipText);
}
