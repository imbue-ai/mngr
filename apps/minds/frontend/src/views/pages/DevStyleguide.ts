import m from "mithril";
import { Badge } from "../components/Badge";
import { Button, ButtonLink, ButtonSubmit } from "../components/Button";
import { Card } from "../components/Card";
import { ColorSwatch } from "../components/ColorSwatch";
import { CopyField, SectionHeader } from "../components/Layout";
import {
  FormLabel,
  Select,
  Textarea,
  TextInput,
} from "../components/FormControls";
import { Icon12, Icon16 } from "../components/Icon";
import { ICONS_12, ICONS_16 } from "../components/icons";
import { Link } from "../components/Link";
import { DialogCloseButton, Modal } from "../components/Modal";
import { Notice, type NoticeVariant } from "../components/Notice";
import { PresetCard } from "../components/PresetCard";
import { Spinner } from "../components/Spinner";
import {
  StatusBadge,
  type StatusBadgeVariant,
} from "../components/StatusBadge";
import { TitlebarButton } from "../components/TitlebarButton";
import { UpdateReadyCard } from "../shell/UpdateReadyCard";
import type { ButtonVariant } from "../components/constants";

const BUTTON_VARIANTS: ButtonVariant[] = [
  "primary",
  "secondary",
  "danger",
  "success",
  "ghost",
];
const STATUS_VARIANTS: StatusBadgeVariant[] = [
  "neutral",
  "success",
  "error",
  "warn",
  "info",
];
const NOTICE_VARIANTS: NoticeVariant[] = ["info", "warn", "success", "error"];

/** One catalog entry: a heading + body, jumped to from the sticky TOC. */
interface StyleguideSection {
  id: string;
  // Short label shown in the table of contents.
  toc: string;
  // Descriptive heading shown above the body in the catalog.
  title: string;
  // Optional one-line blurb under the header (type-helper text-secondary).
  desc?: m.Children;
  body: m.Children;
}

/** A top-level TOC group (Design System / Patterns & Components). */
interface StyleguideGroup {
  id: string;
  label: string;
  description: string;
  sections: StyleguideSection[];
}

function section(entry: StyleguideSection): m.Vnode {
  const hasDesc = entry.desc != null;
  return m("section", { id: entry.id, class: "scroll-mt-8 mb-12" }, [
    // When a blurb follows, the header hugs it (mb-1); otherwise it sits a
    // touch further off its demo (mb-2).
    m(
      "h2",
      { class: "type-heading text-primary " + (hasDesc ? "mb-1" : "mb-2") },
      entry.title,
    ),
    hasDesc
      ? m("p", { class: "type-helper text-secondary mb-4" }, entry.desc)
      : null,
    entry.body,
  ]);
}

/** Inline monospace token reference inside a prose blurb. */
function code(text: string): m.Vnode {
  return m("code", text);
}

/**
 * One type-ramp row: the role rendered at full size, with a monospace spec
 * line (size / weight) beneath it.
 */
function typeSample(
  sampleClass: string,
  sample: string,
  spec: string,
): m.Vnode {
  return m("div", [
    m("p", { class: sampleClass }, sample),
    m("p", { class: "type-helper text-tertiary font-mono mt-0.5" }, spec),
  ]);
}

function swatchRow(label: string, cls: string): m.Vnode {
  return m("div", { class: "flex items-center gap-3 mb-1" }, [
    m("span", {
      class: "inline-block w-8 h-8 rounded-md border border-default " + cls,
    }),
    m("code", { class: "type-helper text-secondary" }, label),
  ]);
}

function tocLink(
  href: string,
  label: string,
  extra: string,
  isActive: boolean,
): m.Vnode {
  return m(
    "a",
    {
      href,
      class: "styleguide-toc-link block hover:text-primary " + extra,
      "aria-current": isActive ? "page" : undefined,
    },
    label,
  );
}

/**
 * The living component catalog: every primitive in its variants, the SPA
 * successor of pages/DevStyleguide.jinja. Also the visual-diff harness's
 * first SPA scenario and the manual QA surface for the token layer.
 *
 * Layout: a sticky left table of contents (scrollspy below) beside the catalog
 * body, split into two top-level groups -- Design System (foundational tokens +
 * the icon set) and Patterns & Components (the composed UI primitives). Each
 * section carries a scroll-mt so a TOC jump lands the heading below the
 * viewport top rather than flush against it.
 */
export function DevStyleguide(): m.Component {
  let isModalOpen = false;
  let isDark = false;
  // Scrollspy: the TOC entry whose section is nearest the top of the scroll
  // viewport reads at full strength (styleguide-toc-link[aria-current]). We
  // drive it through Mithril state (not raw DOM writes) so a redraw can't wipe
  // the highlight.
  let activeId: string | null = null;
  let observer: IntersectionObserver | null = null;
  let observedTargets: Element[] = [];
  const sectionVisibility: Record<string, boolean> = {};

  function recomputeActive(): void {
    let nextActive: string | null = null;
    let nearestTop = Infinity;
    for (const el of observedTargets) {
      if (!sectionVisibility[el.id]) continue;
      const top = el.getBoundingClientRect().top;
      if (top < nearestTop) {
        nearestTop = top;
        nextActive = el.id;
      }
    }
    if (nextActive !== activeId) {
      activeId = nextActive;
      m.redraw();
    }
  }

  return {
    oncreate({ dom }) {
      if (!("IntersectionObserver" in window)) return;
      const links = Array.from(
        dom.querySelectorAll<HTMLAnchorElement>(".styleguide-toc-link"),
      );
      const targets: Element[] = [];
      for (const link of links) {
        const id = (link.getAttribute("href") || "").replace(/^#/, "");
        const el = id ? dom.querySelector("#" + CSS.escape(id)) : null;
        if (el) targets.push(el);
      }
      if (!targets.length) return;
      observedTargets = targets;
      // The routed page scrolls inside #local-page-scroll (Shell), so the
      // scrollspy band is measured against that container, not the viewport.
      const scrollRoot = dom.closest("#local-page-scroll");
      observer = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            sectionVisibility[entry.target.id] = entry.isIntersecting;
          }
          recomputeActive();
        },
        // A thin band near the top of the viewport: a section counts as active
        // only while it sits within it.
        { root: scrollRoot, rootMargin: "-10% 0px -80% 0px", threshold: 0 },
      );
      for (const el of targets) observer.observe(el);
    },
    onremove() {
      observer?.disconnect();
      observer = null;
      observedTargets = [];
    },
    view() {
      const groups: StyleguideGroup[] = [
        {
          id: "design-system",
          label: "Design System",
          description:
            "The foundational tokens -- type, color, border, surface, radius, elevation -- plus the shared icon set.",
          sections: [
            {
              id: "type-ramp",
              toc: "Type ramp",
              title: "Type ramp",
              desc: [
                "Six roles, one class each (size + weight + line-height bundled). Sizes reuse Tailwind's steps -- 24 / 18 / 14 / 12 px. Compose with a color token, e.g. ",
                code("type-label text-primary"),
                ".",
              ],
              body: m("div", { class: "flex flex-col gap-4" }, [
                typeSample(
                  "type-heading-lg text-primary",
                  "Large heading",
                  "type-heading-lg -- 24 / bold",
                ),
                typeSample(
                  "type-heading text-primary",
                  "Regular heading",
                  "type-heading -- 18 / semibold",
                ),
                typeSample(
                  "type-label text-primary",
                  "Label",
                  "type-label -- 14 / semibold",
                ),
                typeSample(
                  "type-body text-primary",
                  "Body -- the default readable paragraph text on a surface.",
                  "type-body -- 14 / regular",
                ),
                typeSample(
                  "type-helper text-primary",
                  "Helper -- captions, hints, and metadata sit at this size.",
                  "type-helper -- 12 / regular",
                ),
                typeSample(
                  "type-section text-secondary",
                  "Section title",
                  "type-section -- 12 / semibold / all caps",
                ),
              ]),
            },
            {
              id: "text-colors",
              toc: "Text colors",
              title: "Text color tokens",
              desc: [
                "Semantic, themeable text colors (",
                code("style.css"),
                "). ",
                code("text-primary"),
                " / ",
                code("-secondary"),
                " / ",
                code("-tertiary"),
                " for text on the current surface; ",
                code("text-inverse-*"),
                " for text on an inverted surface. Pure black / white with alpha; values flip between light and dark.",
              ],
              body: m("div", { class: "space-y-1" }, [
                m("p", { class: "type-body text-primary" }, "text-primary"),
                m("p", { class: "type-body text-secondary" }, "text-secondary"),
                m("p", { class: "type-body text-tertiary" }, "text-tertiary"),
                m(
                  "div",
                  { class: "bg-surface-inverse rounded-md p-3 space-y-1 mt-2" },
                  [
                    m(
                      "p",
                      { class: "type-body text-inverse-primary" },
                      "text-inverse-primary",
                    ),
                    m(
                      "p",
                      { class: "type-body text-inverse-secondary" },
                      "text-inverse-secondary",
                    ),
                    m(
                      "p",
                      { class: "type-body text-inverse-tertiary" },
                      "text-inverse-tertiary",
                    ),
                  ],
                ),
              ]),
            },
            {
              id: "borders",
              toc: "Borders",
              title: "Border tokens",
              desc: [
                "Themeable borders (",
                code("style.css"),
                "): ",
                code("border-subtle"),
                " (faint dividers), ",
                code("border-default"),
                " (standard card / input border), ",
                code("border-strong"),
                " (emphasis, the input rest edge), ",
                code("border-stronger"),
                " (the darker input-hover edge). Black in light, white in dark, same alpha steps.",
              ],
              body: m("div", { class: "flex gap-3" }, [
                ...["subtle", "default", "strong", "stronger"].map((step) =>
                  m("div", { class: "flex flex-col items-center gap-1" }, [
                    m("span", {
                      class: `inline-block w-16 h-10 rounded-md border-2 border-${step}`,
                    }),
                    m(
                      "code",
                      { class: "type-helper text-secondary" },
                      `border-${step}`,
                    ),
                  ]),
                ),
              ]),
            },
            {
              id: "surfaces",
              toc: "Surfaces & fills",
              title: "Surface & fill tokens",
              desc: [
                "Surfaces (",
                code("bg-surface-primary"),
                " / ",
                code("-inverse"),
                " / ",
                code("-overlay"),
                ") are solid backgrounds; primary and inverse mirror (white <-> black), overlay is the inverse color at 20%. Fills (",
                code("bg-fill-subtle"),
                " / ",
                code("-hover"),
                " / ",
                code("-active"),
                ") are translucent tints over a surface.",
              ],
              body: m("div", { class: "space-y-1" }, [
                swatchRow("bg-surface-primary", "bg-surface-primary"),
                swatchRow("bg-surface-inverse", "bg-surface-inverse"),
                swatchRow("bg-surface-overlay", "bg-surface-overlay"),
                swatchRow("bg-fill-subtle", "bg-fill-subtle"),
                swatchRow("bg-fill-hover", "bg-fill-hover"),
                swatchRow("bg-fill-active", "bg-fill-active"),
              ]),
            },
            {
              id: "status",
              toc: "Status / feedback",
              title: "Status / feedback tokens",
              desc: [
                "One hue per semantic (",
                code("important"),
                " / ",
                code("success"),
                " / ",
                code("warning"),
                " / ",
                code("info"),
                "), plus the interactive ",
                code("accent"),
                " (links, selection, focus). Dark mode lifts each hue to a brighter value so it stays legible on the black surface.",
              ],
              body: m("div", { class: "flex gap-4" }, [
                m(
                  "span",
                  { class: "type-body text-important" },
                  "text-important",
                ),
                m("span", { class: "type-body text-success" }, "text-success"),
                m("span", { class: "type-body text-warning" }, "text-warning"),
                m("span", { class: "type-body text-info" }, "text-info"),
                m("span", { class: "type-body text-accent" }, "text-accent"),
              ]),
            },
            {
              id: "radius",
              toc: "Corner radius",
              title: "Corner radius",
              desc: [
                "Five steps -- ",
                code("rounded-sm"),
                " / ",
                code("-md"),
                " / ",
                code("-lg"),
                " / ",
                code("-xl"),
                " / ",
                code("-2xl"),
                " (4 / 6 / 8 / 12 / 16 px). Bigger surfaces take the bigger steps; ",
                code("-2xl"),
                " (16px) is reserved for the largest floating cards.",
              ],
              body: m("div", { class: "flex gap-3 items-end" }, [
                ...(["sm", "md", "lg", "xl", "2xl"] as const).map((step) =>
                  m("div", { class: "flex flex-col items-center gap-1" }, [
                    m("span", {
                      class: `inline-block w-14 h-14 bg-fill-subtle border border-default rounded-${step}`,
                    }),
                    m(
                      "code",
                      { class: "type-helper text-secondary" },
                      `rounded-${step}`,
                    ),
                  ]),
                ),
              ]),
            },
            {
              id: "elevation",
              toc: "Elevation",
              title: "Elevation",
              desc: [
                "Two steps. ",
                code("shadow-raised"),
                " is the hover lift on interactive cards; ",
                code("shadow-overlay"),
                " is the soft floating shadow for surfaces above the page -- menus, modals, tooltips.",
              ],
              body: m("div", { class: "flex gap-6" }, [
                m(
                  "div",
                  {
                    class:
                      "w-32 h-16 rounded-lg bg-surface-primary border border-default shadow-raised flex items-center justify-center type-helper",
                  },
                  "shadow-raised",
                ),
                m(
                  "div",
                  {
                    class:
                      "w-32 h-16 rounded-lg bg-surface-primary border border-default shadow-overlay flex items-center justify-center type-helper",
                  },
                  "shadow-overlay",
                ),
              ]),
            },
            {
              id: "icons-16",
              toc: "Icons (16px)",
              title: "Icons -- 16px (Icon16)",
              body: m("div", { class: "grid grid-cols-4 gap-3" }, [
                ...Object.keys(ICONS_16).map((name) =>
                  m("div", { class: "flex items-center gap-2" }, [
                    m(Icon16, { name }),
                    m("code", { class: "type-helper text-secondary" }, name),
                  ]),
                ),
              ]),
            },
            {
              id: "icons-12",
              toc: "Icons (12px)",
              title: "Icons -- 12x12 chrome glyphs (Icon12)",
              body: m("div", { class: "flex gap-6" }, [
                ...(
                  Object.keys(ICONS_12) as ("minimize" | "maximize" | "close")[]
                ).map((name) =>
                  m("div", { class: "flex items-center gap-2" }, [
                    m(Icon12, { name }),
                    m("code", { class: "type-helper text-secondary" }, name),
                  ]),
                ),
              ]),
            },
          ],
        },
        {
          id: "patterns",
          label: "Patterns & Components",
          description: "The composed UI primitives built from those tokens.",
          sections: [
            {
              id: "color-swatches",
              toc: "Color swatches",
              title: "Color swatches",
              body: m("div", { class: "flex gap-3", role: "radiogroup" }, [
                m(ColorSwatch, {
                  hex: "#0b292b",
                  name: "spruce",
                  selected: true,
                }),
                m(ColorSwatch, { hex: "#fcefd4", name: "clarity" }),
                m(ColorSwatch, { hex: "#8c3b6c", name: "orchid", size: "sm" }),
                m(ColorSwatch, {
                  hex: "#3b8c56",
                  name: "meadow",
                  disabled: true,
                }),
              ]),
            },
            {
              id: "titlebar-buttons",
              toc: "Titlebar buttons",
              title: "Titlebar buttons (self-theming)",
              body: m("div", { class: "flex items-center gap-2" }, [
                m(
                  TitlebarButton,
                  { variant: "nav", "aria-label": "Home" },
                  m(Icon16, { name: "home" }),
                ),
                m(
                  TitlebarButton,
                  { variant: "nav", tone: "muted", "aria-label": "Inbox" },
                  m(Icon16, { name: "inbox" }),
                ),
                m(TitlebarButton, { variant: "crumb" }, "Minds"),
                m(
                  TitlebarButton,
                  { variant: "control", "aria-label": "Minimize" },
                  m(Icon12, { name: "minimize" }),
                ),
                m(
                  TitlebarButton,
                  { variant: "control", "aria-label": "Maximize" },
                  m(Icon12, { name: "maximize" }),
                ),
                m(
                  TitlebarButton,
                  { variant: "control", tone: "danger", "aria-label": "Close" },
                  m(Icon12, { name: "close" }),
                ),
              ]),
            },
            {
              id: "notification-badge",
              toc: "Notification badge",
              title: "Notification badge",
              body: m("div", { class: "flex items-center gap-4" }, [
                m(Badge),
                m(Badge, { count: 3 }),
                m(Badge, { count: 12 }),
                m(Badge, { count: 120 }),
              ]),
            },
            {
              id: "update-ready-card",
              toc: "Update ready",
              title: "Update ready card",
              body: m(
                "div",
                { class: "inline-flex" },
                m(UpdateReadyCard, {
                  version: "0.4.2",
                  onRestart: () => {},
                  onDismiss: () => {},
                }),
              ),
            },
            {
              id: "form-controls",
              toc: "Form controls",
              title: "Form controls (TextInput / Select / Textarea)",
              body: m("div", { class: "space-y-4 max-w-[420px]" }, [
                m("div", [
                  m(FormLabel, { target: "sg-input" }, "Text input"),
                  m(TextInput, {
                    name: "sg-input",
                    id: "sg-input",
                    placeholder: "you@example.com",
                  }),
                ]),
                m("div", [
                  m(FormLabel, { target: "sg-select" }, "Select"),
                  m(
                    Select,
                    { name: "sg-select", id: "sg-select", width: "w-48" },
                    [m("option", "local"), m("option", "remote")],
                  ),
                ]),
                m("div", [
                  m(FormLabel, { target: "sg-textarea" }, "Textarea"),
                  m(Textarea, {
                    name: "sg-textarea",
                    id: "sg-textarea",
                    rows: 3,
                    value: "Some text",
                  }),
                ]),
              ]),
            },
            {
              id: "copy-field",
              toc: "Copy field",
              title: "Copy field",
              body: m(
                CopyField,
                {
                  value: "https://example.localhost:8000/goto/host-0123/",
                  extra: "max-w-[480px]",
                },
                [
                  m(
                    Button,
                    { size: "icon", "aria-label": "Copy" },
                    m(Icon16, { name: "copy" }),
                  ),
                ],
              ),
            },
            {
              id: "spinner",
              toc: "Spinner",
              title: "Spinner",
              body: m("div", { class: "flex items-center gap-4" }, [
                m(Spinner, { size: "sm" }),
                m(Spinner, { size: "md" }),
                m(Spinner, { size: "lg" }),
                m(Spinner, { size: "md", tone: "accent" }),
                m(Button, { variant: "success" }, [
                  m(Spinner, { size: "sm", tone: "inverse" }),
                  "Approving...",
                ]),
              ]),
            },
            {
              id: "buttons",
              toc: "Buttons",
              title: "Buttons -- variants",
              body: m("div", { class: "flex flex-wrap gap-2" }, [
                ...BUTTON_VARIANTS.map((variant) =>
                  m(Button, { variant }, variant),
                ),
                m(Button, { variant: "primary", disabled: true }, "disabled"),
              ]),
            },
            {
              id: "button-sizes",
              toc: "Button sizes",
              title: "Buttons -- sizes",
              body: m("div", { class: "flex items-center gap-2" }, [
                m(Button, { size: "md" }, "md"),
                m(Button, { size: "lg" }, "lg"),
                m(
                  Button,
                  { size: "icon", "aria-label": "Restart" },
                  m(Icon16, { name: "restart" }),
                ),
                m(ButtonSubmit, { variant: "primary" }, "submit"),
                m(ButtonLink, { href: "#", variant: "secondary" }, "link"),
              ]),
            },
            {
              id: "links",
              toc: "Links",
              title: "Links",
              body: m("p", { class: "type-body text-primary" }, [
                "Body text with an ",
                m(Link, { href: "#" }, "inline link"),
                " and a ",
                m(Link, { href: "#", weight: "medium" }, "medium-weight link"),
                ".",
              ]),
            },
            {
              id: "status-badges",
              toc: "Status badges",
              title: "Status badges",
              body: m("div", { class: "flex gap-2 items-center" }, [
                ...STATUS_VARIANTS.map((variant) =>
                  m(StatusBadge, { variant }, variant),
                ),
                m(
                  StatusBadge,
                  { variant: "neutral", size: "xs" },
                  "Signed out",
                ),
              ]),
            },
            {
              id: "notices",
              toc: "Notices",
              title: "Notices",
              body: m("div", [
                ...NOTICE_VARIANTS.map((variant) =>
                  m(Notice, { variant }, `A ${variant} notice.`),
                ),
              ]),
            },
            {
              id: "cards",
              toc: "Cards",
              title: "Cards",
              body: m("div", { class: "space-y-3 max-w-[480px]" }, [
                m(Card, "Basic block card"),
                m(Card, { layout: "row-spread" }, [
                  m("span", { class: "type-body" }, "Row-spread card"),
                  m(Button, "Action"),
                ]),
                m(
                  Card,
                  {
                    as: "a",
                    href: "#",
                    layout: "row",
                    interactive: true,
                    padding: "tight",
                    extra: "accent-spine relative",
                  },
                  [
                    m(
                      "span",
                      { class: "type-body pl-2" },
                      "Interactive card with accent spine",
                    ),
                  ],
                ),
              ]),
            },
            {
              id: "preset-cards",
              toc: "Preset cards",
              title: "Preset cards",
              body: m("div", { class: "flex gap-3", role: "radiogroup" }, [
                m(
                  PresetCard,
                  { preset: "remote", selected: true, extra: "flex-1" },
                  [
                    m(
                      "span",
                      { class: "type-label text-primary" },
                      "Imbue Cloud",
                    ),
                    m(
                      "span",
                      { class: "type-helper text-secondary" },
                      "Runs in the cloud",
                    ),
                  ],
                ),
                m(PresetCard, { preset: "local", extra: "flex-1" }, [
                  m(
                    "span",
                    { class: "type-label text-primary" },
                    "This computer",
                  ),
                  m(
                    "span",
                    { class: "type-helper text-secondary" },
                    "Runs in Docker locally",
                  ),
                ]),
              ]),
            },
            {
              id: "modal",
              toc: "Modal",
              title: "Modal",
              body: [
                m(
                  Button,
                  { onclick: () => (isModalOpen = true) },
                  "Open modal",
                ),
                m(
                  Modal,
                  {
                    isOpen: isModalOpen,
                    onClose: () => (isModalOpen = false),
                    cardExtra: "relative",
                  },
                  [
                    m(DialogCloseButton, {
                      onClose: () => (isModalOpen = false),
                    }),
                    m(
                      "h3",
                      { class: "type-heading text-primary mb-2" },
                      "A modal",
                    ),
                    m(
                      "p",
                      { class: "type-body text-secondary mb-4" },
                      "In-DOM modal component; no overlay iframe.",
                    ),
                    m("div", { class: "flex justify-end gap-2" }, [
                      m(
                        Button,
                        { onclick: () => (isModalOpen = false) },
                        "Cancel",
                      ),
                      m(
                        Button,
                        {
                          variant: "primary",
                          onclick: () => (isModalOpen = false),
                        },
                        "Confirm",
                      ),
                    ]),
                  ],
                ),
              ],
            },
            {
              id: "section-headers",
              toc: "Section headers",
              title: "Section headers",
              body: m("div", [
                m(SectionHeader, "Plain header"),
                m(SectionHeader, { divider: true }, "Divider header"),
              ]),
            },
          ],
        },
      ];

      return m(
        "div",
        { class: "max-w-6xl mx-auto px-8 py-12 flex items-start gap-12" },
        [
          // Dev-only light/dark toggle. Fixed top-right so it stays reachable at
          // any scroll position; floats over the page on its own opaque surface
          // (the styleguide-toggle recipe in style.css). Sits below the Shell's
          // ~42px titlebar so it clears the inbox / bug-report icons there.
          m(
            Button,
            {
              variant: "secondary",
              extra:
                "styleguide-toggle fixed top-14 right-6 z-50 shadow-overlay",
              onclick: () => {
                isDark = !isDark;
                document.documentElement.classList.toggle("dark", isDark);
              },
            },
            isDark ? "Switch to light" : "Switch to dark",
          ),

          // Sticky table of contents. Hidden on narrow viewports (the catalog
          // just scrolls); shown from lg up where there's room beside the body.
          m(
            "nav",
            {
              "aria-label": "Styleguide sections",
              class:
                "styleguide-toc sticky top-8 shrink-0 w-48 hidden lg:block max-h-[calc(100vh-6rem)] overflow-y-auto",
            },
            groups.map((group) =>
              m("div", { class: "mb-6" }, [
                tocLink(
                  "#" + group.id,
                  group.label,
                  "type-section text-tertiary mb-2",
                  activeId === group.id,
                ),
                m(
                  "ul",
                  { class: "flex flex-col gap-0.5" },
                  group.sections.map((entry) =>
                    m(
                      "li",
                      tocLink(
                        "#" + entry.id,
                        entry.toc,
                        "type-helper text-secondary py-0.5",
                        activeId === entry.id,
                      ),
                    ),
                  ),
                ),
              ]),
            ),
          ),

          m("main", { class: "flex-1 min-w-0" }, [
            m("header", { class: "mb-12" }, [
              m(
                "h1",
                { class: "type-heading-lg text-primary" },
                "Minds Styleguide",
              ),
              m("p", { class: "type-body text-secondary mt-1" }, [
                "Tokens live in ",
                m(
                  "code",
                  { class: "text-primary" },
                  "apps/minds/frontend/src/style.css",
                ),
                ".",
              ]),
            ]),

            ...groups.map((group, groupIndex) => [
              m(
                "div",
                {
                  id: group.id,
                  // Groups after the first carry a top divider rule so the two
                  // read as distinct sections rather than running together.
                  class:
                    groupIndex === 0
                      ? "scroll-mt-8 mb-8"
                      : "scroll-mt-8 mt-16 pt-12 border-t border-default mb-8",
                },
                [
                  m(
                    "h2",
                    { class: "type-heading-lg text-primary" },
                    group.label,
                  ),
                  m(
                    "p",
                    { class: "type-helper text-secondary mt-1" },
                    group.description,
                  ),
                ],
              ),
              ...group.sections.map((entry) => section(entry)),
            ]),
          ]),
        ],
      );
    },
  };
}
