// How an error looks, in the pieces every error surface is built from: the
// mark that leads it, the copy markup its sentence is written in, the marked
// line the two make together, and the rule that decides which of its buttons
// is the main one.
//
// Everything beside text is sized in em, so a mark beside helper-size text is
// smaller than one beside body text without either caller saying so.

import m from "mithril";
import { copyMarkupSpans } from "../../models/errors";
import { ICONS_16 } from "./icons";

/** "warn" for a condition the user fixes themselves or that clears on its own,
 * rather than a failure. */
export type ErrorTone = "error" | "warn";

export const TONE_TEXT_CLASS: Record<ErrorTone, string> = {
  error: "text-important",
  warn: "text-warning",
};

/** The tint the notice box sits in. */
export const TONE_SURFACE_CLASS: Record<ErrorTone, string> = {
  error: "bg-[var(--c-important-surface)]",
  warn: "bg-[var(--c-warning-surface)]",
};

/** What follows a marked sentence lines up with the sentence's text rather
 * than with its mark. */
export const MARK_INDENT_CLASS = "pl-[1.72em]";

/** An icon from the 16px set at the size of the text beside it. */
export function emIcon(name: keyof typeof ICONS_16, extra = ""): m.Children {
  return m(
    "svg",
    {
      viewBox: "0 0 16 16",
      fill: "currentColor",
      "aria-hidden": "true",
      class: "h-[1em] w-[1em] shrink-0 " + extra,
    },
    m.trust(ICONS_16[name]),
  );
}

/**
 * The mark an error leads with, in its tone's colour. It belongs in a row
 * aligned on baselines (`items-baseline`), which sets its bottom on the first
 * line's baseline; it then drops by half its size less half a capital, so it
 * centres on the capitals rather than on the line box, whose descender room
 * would leave it hanging low. Its negative top margin keeps it from making
 * the line any taller than the text does.
 */
export function errorMark(size = "1.15em", extra = ""): m.Children {
  return m(
    "svg",
    {
      width: size,
      height: size,
      viewBox: "0 0 16 16",
      fill: "currentColor",
      "aria-hidden": "true",
      class: "relative shrink-0 " + extra,
      style: {
        top: `calc(${size} / 2 - 0.5cap)`,
        marginTop: `calc(-1 * ${size})`,
      },
    },
    m.trust(ICONS_16["alert-filled"]),
  );
}

/**
 * An error sentence as written: `backticks` become inline code, and *the ask*
 * -- the instruction the user is to follow -- becomes semibold italic, so it
 * stands out from the generic "Try again. If this persists, report a bug."
 * around it.
 *
 * The markup is ours and never arrives from a server: a route's own sentence
 * is shown as the plain text it is, which is what these splits leave it as.
 */
export function richErrorText(text: string): m.Children {
  return copyMarkupSpans(text).map((span) => {
    switch (span.kind) {
      case "text":
        return span.text;
      case "code":
        return m(
          "code",
          {
            class:
              "rounded bg-fill-hover px-1 py-px font-mono font-normal " +
              "text-[0.92em] not-italic box-decoration-clone",
          },
          span.text,
        );
      // A command is set as it was written; an ask may still hold one.
      case "ask":
        return m(
          "em",
          { class: "italic font-semibold" },
          richErrorText(span.text),
        );
    }
  });
}

export interface MarkedLineOptions {
  tone?: ErrorTone;
  size?: "body" | "helper";
  /** A small error under a field or in a card's meta line: red text, and
   * tighter to its mark than a sentence in a notice is. */
  isInline?: boolean;
  /** The surface's own title already carries the mark (see `ErrorTitle`), so
   * the sentence under it is flush rather than marked a second time. */
  isTitleMarked?: boolean;
  extra?: string;
}

/** The mark and the sentence beside it, the shape every error leads with. */
export function markedLine(
  options: MarkedLineOptions,
  children: m.Children,
): m.Children {
  const {
    tone = "error",
    size = "body",
    isInline = false,
    isTitleMarked = false,
    extra = "",
  } = options;
  const typeClass = size === "body" ? "type-body" : "type-helper";
  if (isTitleMarked) {
    return m("div", { class: `text-primary ${typeClass} ${extra}` }, children);
  }
  return m(
    "div",
    {
      class: [
        "flex items-baseline",
        isInline ? TONE_TEXT_CLASS[tone] : "text-primary",
        isInline ? "gap-[0.33em]" : "gap-[0.57em]",
        typeClass,
        extra,
      ]
        .filter(Boolean)
        .join(" "),
    },
    [
      errorMark("1.15em", TONE_TEXT_CLASS[tone]),
      m("div", { class: "min-w-0 flex-1" }, children),
    ],
  );
}

interface InlineErrorAttrs extends m.Attributes {
  message: string;
  tone?: ErrorTone;
  extra?: string;
}

/** A small error of its own: under a field, or in a card's meta line. Too
 * small to be a notice, so it keeps red text rather than the tinted box. */
export function InlineError(): m.Component<InlineErrorAttrs> {
  return {
    view(vnode) {
      const { message, tone = "error", extra = "" } = vnode.attrs;
      return markedLine(
        { tone, size: "helper", isInline: true, extra },
        richErrorText(message),
      );
    },
  };
}

interface ErrorTitleAttrs extends m.Attributes {
  /** The type class the title is set in; its colour is its tone's. */
  typeClass: string;
  tone?: ErrorTone;
  extra?: string;
}

/**
 * A title that names what failed, carrying the mark. Only for a surface that
 * exists because of the error -- an error dialog, Help's "Couldn't start an
 * agent". A surface that was already there keeps its own heading and shows
 * the error as a notice inside it.
 *
 * Pass `isTitleMarked` to whatever draws the sentence under it, so the mark
 * is not drawn twice.
 */
export function ErrorTitle(): m.Component<ErrorTitleAttrs> {
  return {
    view(vnode) {
      const { typeClass, tone = "error", extra = "" } = vnode.attrs;
      return m(
        "div",
        {
          class: `${typeClass} flex items-baseline gap-[0.22em] ${TONE_TEXT_CLASS[tone]} ${extra}`,
        },
        [errorMark("1em"), vnode.children],
      );
    },
  };
}

/** The buttons that only confirm an instruction the copy already gave. */
const FOLLOW_UP_LABELS = new Set(["Retry", "Try again", "Check again"]);

/**
 * Whether the error's lead button merely confirms an ask the copy makes, as
 * in "*start Docker, then select Retry*". The user's next step is the ask, so
 * the button waiting on it is drawn secondary rather than as the main one.
 */
export function isFollowUpLed(
  message: string,
  firstLabel: string | undefined,
): boolean {
  return (
    hasAsk(message) &&
    firstLabel !== undefined &&
    FOLLOW_UP_LABELS.has(firstLabel)
  );
}

/** Whether the sentence asks the user for something, rather than merely
 * naming a command (whose own asterisks are the command's). */
function hasAsk(message: string): boolean {
  return copyMarkupSpans(message).some((span) => span.kind === "ask");
}
