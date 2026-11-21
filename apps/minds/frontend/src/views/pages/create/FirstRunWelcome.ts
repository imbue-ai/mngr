// The first run's welcome (specs/ftue-onboarding-v2): "Welcome, <name>!", the studio-type question with its
// pills, and whatever stands under them (the build's progress, then Continue). Drawn by the creation page while
// the workspace is built, and by the shell's cover over the workspace while it loads (views/shell/FirstRunCover.ts),
// so the two read as one screen.

import m from "mithril";
import type { StudioType } from "../../../models/firstRun";
import {
  STUDIO_TYPES,
  describeStudioType,
  firstNameOf,
  firstRun,
  toggleStudioType,
  welcomeHeading,
} from "../../../models/firstRun";

export const STUDIO_TYPE_QUESTION = "What type of studio do you want to make?";
export const SOMETHING_ELSE_LABEL = "Something else";
export const SOMETHING_ELSE_PLACEHOLDER = "Describe it…";
export const WORKSPACE_READY_LINE = "Your workspace is ready!";
export const CONTINUE_LABEL = "Continue";
const SOMETHING_ELSE_ARIA_LABEL = "Describe your studio";
const CHECK_ICON_PATH = "M20 6 9 17l-5-5";
const PLUS_ICON_PATH = "M5 12h14M12 5v14";
/** Each pill's small tilt, by its place in the field: loose, like pills pressed onto a poster. */
const PILL_TILTS_DEG = [-2.3, 1.4, -0.9, 2.3, -1.7, 0.9, 1.8, -2.0, 0.7, -1.3, 2.0, -0.7, 1.5, -2.4, 1.1, 1.3];

export interface FirstRunWelcomeAttrs {
  /** The element's id: the creation page's is `creating`. */
  id: string;
  attemptId: string;
  /** Whether the "Something else" field is open; the owner keeps it, so it survives a redraw. */
  isOtherOpen: boolean;
  setOtherOpen: (isOpen: boolean) => void;
  /** Run after the answer changed. */
  onChanged: () => void;
  /** What stands under the pills. */
  status: m.Children;
  /** The pills are already on the screen (the cover taking over from the page), so they do not drop in again. */
  isSettled?: boolean;
  extraClass?: string;
}

/** A drawn icon the size of a pill's label. */
function pillIcon(path: string, strokeWidth: number, extra: string): m.Children {
  return m(
    "svg",
    {
      class: extra,
      width: 18,
      height: 18,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": strokeWidth,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    m("path", { d: path }),
  );
}

/** A pill in its place in the field: tilted, dropping in after the ones before it. */
function pillSlot(attrs: FirstRunWelcomeAttrs, key: string, index: number, pill: m.Children): m.Children {
  return m(
    "span",
    {
      key,
      class: attrs.isSettled === true ? "inline-block" : "start-chat-in inline-block",
      style: `rotate: ${PILL_TILTS_DEG[index % PILL_TILTS_DEG.length]}deg; --start-chat-delay: ${index * 45}ms;`,
    },
    pill,
  );
}

function studioTypePill(attrs: FirstRunWelcomeAttrs, type: StudioType, index: number): m.Children {
  const isPicked = firstRun.studioTypes.pickedIds.includes(type.id);
  return pillSlot(
    attrs,
    type.id,
    index,
    m(
      "button",
      {
        type: "button",
        "data-studio-type": type.id,
        "aria-pressed": isPicked ? "true" : "false",
        class: `studio-type-pill studio-type-pill--${type.category} px-8`,
        onclick: () => {
          firstRun.studioTypes = toggleStudioType(firstRun.studioTypes, type.id);
          attrs.onChanged();
        },
      },
      [
        // The check sits in the pill's own left padding and the label eases right, so a pick never changes the pill's width.
        pillIcon(CHECK_ICON_PATH, 3, "studio-type-check"),
        m("span", { class: "studio-type-label" }, type.label),
      ],
    ),
  );
}

/** "Something else": an outlined pill that opens into a field in the same shape, and fills once described. */
function somethingElse(attrs: FirstRunWelcomeAttrs): m.Children {
  const index = STUDIO_TYPES.length;
  const isFilled = firstRun.studioTypes.otherText.trim() !== "";
  if (!attrs.isOtherOpen && !isFilled) {
    return pillSlot(
      attrs,
      "something-else",
      index,
      m(
        "button",
        {
          type: "button",
          id: "studio-type-other",
          class: "studio-type-pill studio-type-pill--other px-6",
          onclick: () => {
            attrs.setOtherOpen(true);
          },
        },
        [pillIcon(PLUS_ICON_PATH, 2.5, "-ml-1"), SOMETHING_ELSE_LABEL],
      ),
    );
  }
  return pillSlot(
    attrs,
    "something-else",
    index,
    m(
      "label",
      {
        id: "studio-type-other",
        "data-filled": isFilled ? "true" : "false",
        class: "studio-type-pill studio-type-pill--other studio-type-pill--field px-6",
      },
      [
        isFilled ? pillIcon(CHECK_ICON_PATH, 3, "-ml-1 shrink-0") : null,
        m("input", {
          id: "studio-type-other-field",
          type: "text",
          "aria-label": SOMETHING_ELSE_ARIA_LABEL,
          placeholder: SOMETHING_ELSE_PLACEHOLDER,
          value: firstRun.studioTypes.otherText,
          class: "w-[220px] border-0 bg-transparent p-0 text-[18px] font-semibold outline-none",
          oncreate: (vnode: m.VnodeDOM) => {
            if (attrs.isOtherOpen) (vnode.dom as HTMLInputElement).focus();
          },
          oninput: (event: Event) => {
            firstRun.studioTypes = describeStudioType(firstRun.studioTypes, (event.target as HTMLInputElement).value);
          },
          onblur: () => {
            attrs.setOtherOpen(false);
            attrs.onChanged();
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter" || event.key === "Escape") (event.target as HTMLInputElement).blur();
          },
        }),
      ],
    ),
  );
}

/** The welcome, whole. */
export function firstRunWelcome(attrs: FirstRunWelcomeAttrs): m.Children {
  return m(
    "div",
    {
      id: attrs.id,
      "data-agent-id": attrs.attemptId,
      "data-first-run": "true",
      class: `first-run flex min-h-full w-full flex-col items-center justify-center px-6 py-16 ${attrs.extraClass ?? ""}`,
    },
    [
      m(
        "h1",
        { id: "creating-welcome", class: "first-run-headline text-center text-[40px]" },
        welcomeHeading(firstNameOf(firstRun.account)),
      ),
      m("p", { id: "studio-type-question", class: "mt-5 text-center type-heading" }, STUDIO_TYPE_QUESTION),
      m(
        "div",
        {
          id: "studio-types",
          role: "group",
          "aria-label": STUDIO_TYPE_QUESTION,
          class: "mt-[52px] flex max-w-[960px] flex-wrap items-center justify-center gap-x-3 gap-y-5",
        },
        [...STUDIO_TYPES.map((type, index) => studioTypePill(attrs, type, index)), somethingElse(attrs)],
      ),
      // One height for whatever stands here (progress, opening, Continue), so the welcome above it never moves
      // as one gives way to the next.
      m("div", { class: "flex min-h-[152px] w-full flex-col items-center" }, attrs.status),
    ],
  );
}

/** The thin bar under its line, as wide as the line, while something is still being readied, with anything more
 * under them. */
export function settingUpStatus(line: string, percent: number, extra: m.Children[] = []): m.Children {
  return m("div", { class: "mt-[68px] flex flex-col items-center" }, [
    m("div", { class: "flex w-fit flex-col" }, [
      m("p", { id: "creating-setting-up", class: "text-[17px]" }, line),
      m(
        "div",
        { class: "first-run-bar mt-4 h-1 w-full overflow-hidden rounded-full" },
        m("div", {
          id: "creating-welcome-bar",
          class: "h-full rounded-full transition-[width] duration-300 ease-out",
          style: `width: ${percent.toFixed(1)}%`,
        }),
      ),
    ]),
    ...extra,
  ]);
}

/** "Your workspace is ready!" and Continue. */
export function readyStatus(onContinue: () => void): m.Children {
  return m("div", { class: "mt-[68px] flex flex-col items-center gap-4" }, [
    m("p", { id: "creating-ready", class: "text-[18px]" }, WORKSPACE_READY_LINE),
    m(
      "button",
      {
        id: "creating-continue",
        type: "button",
        class: "first-run-button type-heading",
        onclick: onContinue,
      },
      CONTINUE_LABEL,
    ),
  ]);
}
