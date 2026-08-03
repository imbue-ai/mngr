import m from "mithril";
import { Badge } from "../components/Badge";
import { Button, ButtonLink, ButtonSubmit } from "../components/Button";
import { Card } from "../components/Card";
import { ColorSwatch } from "../components/ColorSwatch";
import { CopyField, PageContainer, SectionHeader } from "../components/Layout";
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

function section(
  id: string,
  title: string,
  ...children: m.Children[]
): m.Vnode {
  return m("section", { id, class: "scroll-mt-8 mb-12" }, [
    m("h2", { class: "type-heading text-primary mb-2" }, title),
    ...children,
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

/**
 * The living component catalog: every primitive in its variants, the SPA
 * successor of pages/DevStyleguide.jinja. Also the visual-diff harness's
 * first SPA scenario and the manual QA surface for the token layer.
 */
export function DevStyleguide(): m.Component {
  let isModalOpen = false;
  let isDark = false;
  return {
    view() {
      return m(PageContainer, { extra: "pb-24" }, [
        m("div", { class: "flex items-center justify-between mb-8" }, [
          m("h1", { class: "type-heading-lg text-primary" }, "Design System"),
          m(
            Button,
            {
              variant: "secondary",
              extra: "styleguide-toggle",
              onclick: () => {
                isDark = !isDark;
                document.documentElement.classList.toggle("dark", isDark);
              },
            },
            isDark ? "Light mode" : "Dark mode",
          ),
        ]),

        section(
          "type-ramp",
          "Type ramp",
          m("div", { class: "space-y-2" }, [
            m(
              "p",
              { class: "type-heading-lg text-primary" },
              "type-heading-lg -- large heading",
            ),
            m(
              "p",
              { class: "type-heading text-primary" },
              "type-heading -- regular heading",
            ),
            m(
              "p",
              { class: "type-label text-primary" },
              "type-label -- form + control labels",
            ),
            m(
              "p",
              { class: "type-body text-primary" },
              "type-body -- default body text",
            ),
            m(
              "p",
              { class: "type-helper text-secondary" },
              "type-helper -- captions, hints, metadata",
            ),
            m(
              "p",
              { class: "type-section text-tertiary" },
              "type-section -- section eyebrow",
            ),
          ]),
        ),

        section(
          "text-colors",
          "Text color tokens",
          m("div", { class: "space-y-1" }, [
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
        ),

        section(
          "borders",
          "Border tokens",
          m("div", { class: "flex gap-3" }, [
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
        ),

        section(
          "surfaces",
          "Surface & fill tokens",
          m("div", { class: "space-y-1" }, [
            swatchRow("bg-surface-primary", "bg-surface-primary"),
            swatchRow("bg-surface-inverse", "bg-surface-inverse"),
            swatchRow("bg-surface-overlay", "bg-surface-overlay"),
            swatchRow("bg-fill-subtle", "bg-fill-subtle"),
            swatchRow("bg-fill-hover", "bg-fill-hover"),
            swatchRow("bg-fill-active", "bg-fill-active"),
          ]),
        ),

        section(
          "status",
          "Status / feedback tokens",
          m("div", { class: "flex gap-4" }, [
            m("span", { class: "type-body text-important" }, "text-important"),
            m("span", { class: "type-body text-success" }, "text-success"),
            m("span", { class: "type-body text-warning" }, "text-warning"),
            m("span", { class: "type-body text-info" }, "text-info"),
            m("span", { class: "type-body text-accent" }, "text-accent"),
          ]),
        ),

        section(
          "radius",
          "Corner radius",
          m("div", { class: "flex gap-3 items-end" }, [
            ...(["sm", "md", "lg", "xl"] as const).map((step) =>
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
        ),

        section(
          "elevation",
          "Elevation",
          m("div", { class: "flex gap-6" }, [
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
        ),

        section(
          "icons-16",
          "Icons -- 16px (Icon16)",
          m("div", { class: "grid grid-cols-4 gap-3" }, [
            ...Object.keys(ICONS_16).map((name) =>
              m("div", { class: "flex items-center gap-2" }, [
                m(Icon16, { name }),
                m("code", { class: "type-helper text-secondary" }, name),
              ]),
            ),
          ]),
        ),

        section(
          "icons-12",
          "Icons -- 12x12 chrome glyphs (Icon12)",
          m("div", { class: "flex gap-6" }, [
            ...(
              Object.keys(ICONS_12) as ("minimize" | "maximize" | "close")[]
            ).map((name) =>
              m("div", { class: "flex items-center gap-2" }, [
                m(Icon12, { name }),
                m("code", { class: "type-helper text-secondary" }, name),
              ]),
            ),
          ]),
        ),

        section(
          "color-swatches",
          "Color swatches",
          m("div", { class: "flex gap-3", role: "radiogroup" }, [
            m(ColorSwatch, { hex: "#0b292b", name: "spruce", selected: true }),
            m(ColorSwatch, { hex: "#fcefd4", name: "clarity" }),
            m(ColorSwatch, { hex: "#8c3b6c", name: "orchid", size: "sm" }),
            m(ColorSwatch, { hex: "#3b8c56", name: "meadow", disabled: true }),
          ]),
        ),

        section(
          "titlebar-buttons",
          "Titlebar buttons (self-theming)",
          m("div", { class: "flex items-center gap-2" }, [
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
        ),

        section(
          "notification-badge",
          "Notification badge",
          m("div", { class: "flex items-center gap-4" }, [
            m(Badge),
            m(Badge, { count: 3 }),
            m(Badge, { count: 12 }),
            m(Badge, { count: 120 }),
          ]),
        ),

        section(
          "form-controls",
          "Form controls (TextInput / Select / Textarea)",
          m("div", { class: "space-y-4 max-w-[420px]" }, [
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
              m(Select, { name: "sg-select", id: "sg-select", width: "w-48" }, [
                m("option", "local"),
                m("option", "remote"),
              ]),
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
        ),

        section(
          "copy-field",
          "Copy field",
          m(
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
        ),

        section(
          "spinner",
          "Spinner",
          m("div", { class: "flex items-center gap-4" }, [
            m(Spinner, { size: "sm" }),
            m(Spinner, { size: "md" }),
            m(Spinner, { size: "lg" }),
            m(Spinner, { size: "md", tone: "accent" }),
            m(Button, { variant: "success" }, [
              m(Spinner, { size: "sm", tone: "inverse" }),
              "Approving...",
            ]),
          ]),
        ),

        section(
          "buttons",
          "Buttons -- variants",
          m("div", { class: "flex flex-wrap gap-2" }, [
            ...BUTTON_VARIANTS.map((variant) =>
              m(Button, { variant }, variant),
            ),
            m(Button, { variant: "primary", disabled: true }, "disabled"),
          ]),
        ),

        section(
          "button-sizes",
          "Buttons -- sizes",
          m("div", { class: "flex items-center gap-2" }, [
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
        ),

        section(
          "links",
          "Links",
          m("p", { class: "type-body text-primary" }, [
            "Body text with an ",
            m(Link, { href: "#" }, "inline link"),
            " and a ",
            m(Link, { href: "#", weight: "medium" }, "medium-weight link"),
            ".",
          ]),
        ),

        section(
          "status-badges",
          "Status badges",
          m("div", { class: "flex gap-2 items-center" }, [
            ...STATUS_VARIANTS.map((variant) =>
              m(StatusBadge, { variant }, variant),
            ),
            m(StatusBadge, { variant: "neutral", size: "xs" }, "Signed out"),
          ]),
        ),

        section(
          "notices",
          "Notices",
          m("div", [
            ...NOTICE_VARIANTS.map((variant) =>
              m(Notice, { variant }, `A ${variant} notice.`),
            ),
          ]),
        ),

        section(
          "cards",
          "Cards",
          m("div", { class: "space-y-3 max-w-[480px]" }, [
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
        ),

        section(
          "preset-cards",
          "Preset cards",
          m("div", { class: "flex gap-3", role: "radiogroup" }, [
            m(
              PresetCard,
              { preset: "remote", selected: true, extra: "flex-1" },
              [
                m("span", { class: "type-label text-primary" }, "Imbue Cloud"),
                m(
                  "span",
                  { class: "type-helper text-secondary" },
                  "Runs in the cloud",
                ),
              ],
            ),
            m(PresetCard, { preset: "local", extra: "flex-1" }, [
              m("span", { class: "type-label text-primary" }, "This computer"),
              m(
                "span",
                { class: "type-helper text-secondary" },
                "Runs in Docker locally",
              ),
            ]),
          ]),
        ),

        section(
          "modal",
          "Modal",
          m(Button, { onclick: () => (isModalOpen = true) }, "Open modal"),
          m(
            Modal,
            {
              isOpen: isModalOpen,
              onClose: () => (isModalOpen = false),
              cardExtra: "relative",
            },
            [
              m(DialogCloseButton, { onClose: () => (isModalOpen = false) }),
              m("h3", { class: "type-heading text-primary mb-2" }, "A modal"),
              m(
                "p",
                { class: "type-body text-secondary mb-4" },
                "In-DOM modal component; no overlay iframe.",
              ),
              m("div", { class: "flex justify-end gap-2" }, [
                m(Button, { onclick: () => (isModalOpen = false) }, "Cancel"),
                m(
                  Button,
                  { variant: "primary", onclick: () => (isModalOpen = false) },
                  "Confirm",
                ),
              ]),
            ],
          ),
        ),

        section(
          "section-headers",
          "Section headers",
          m("div", [
            m(SectionHeader, "Plain header"),
            m(SectionHeader, { divider: true }, "Divider header"),
          ]),
        ),
      ]);
    },
  };
}
