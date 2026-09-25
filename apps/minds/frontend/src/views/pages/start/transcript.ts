// The chat primitives the start flow and the creation page are drawn with: a
// user turn (the grey bubble on the right), an agent turn (a bare paragraph
// streamed a character at a time), the row of buttons a question offers, the
// two-column comparison, and the undo control inside an answered bubble.
//
// Arrival is CSS: every turn carries its own delay as a custom property and a
// fresh `key`, so a newly mounted node runs its animation from the start and a
// redraw never restarts one (see the start-* rules in style.css).

import m from "mithril";
import { CHAT_BUBBLE_MS, CHAT_STREAM_FADE_MS, CHAT_STREAM_STEP_MS, streamDurationMs } from "../../../models/startFlow";
import type { DisclosurePoint, TableColumn } from "../../../models/startFlow";
import { Button } from "../../components/Button";
import { Disclosure } from "../../components/Disclosure";
import { Icon16 } from "../../components/Icon";

/** The column every transcript sits in: the chat's 720px measure, 48px between turns. */
export const TRANSCRIPT_COLUMN_CLASS = "mx-auto flex w-full max-w-[720px] flex-col px-6 py-[100px] type-body text-primary";

/**
 * Text revealed a character at a time. The whole string is in the DOM from the
 * first frame, so the block is its final size at once and nothing reflows as
 * it fills.
 */
export function streamedText(text: string, startAtMs: number, isInstant: boolean): m.Children {
  if (isInstant) return text;
  return [...text].map((character, index) =>
    m(
      "span",
      {
        key: index,
        class: "start-char",
        style: `--start-char-delay: ${startAtMs + index * CHAT_STREAM_STEP_MS}ms; --start-char-fade: ${CHAT_STREAM_FADE_MS}ms;`,
      },
      character,
    ),
  );
}

interface ArrivalAttrs {
  /** Identity for the mount, so the arrival plays once per turn. */
  key: string;
  delayMs: number;
  /** Already on the page: no arrival animation (a reload, a transcript carried over). */
  isInstant?: boolean;
}

function arrivalStyle(attrs: ArrivalAttrs): string {
  return attrs.isInstant ? "" : `--start-chat-delay: ${attrs.delayMs}ms; --start-chat-fade: ${CHAT_BUBBLE_MS}ms;`;
}

function arrivalClass(attrs: ArrivalAttrs): string {
  return attrs.isInstant ? "" : " start-chat-in";
}

/**
 * A user turn: what you said, in the grey pill a sent message gets. The
 * bottom-right corner is the one that is not 18px, the corner nearest the
 * person who said it.
 */
export function userTurn(attrs: ArrivalAttrs & { text: string; onUndo?: () => void }): m.Children {
  return m(
    "div",
    { key: attrs.key, class: "mt-10 flex justify-end first:mt-0" + arrivalClass(attrs), style: arrivalStyle(attrs) },
    m(
      "div",
      {
        class:
          "max-w-[80%] rounded-[18px] rounded-br-[4px] bg-fill-subtle px-4 py-2.5 leading-[1.5] " +
          // break-words, not break-all: ordinary prose still wraps at spaces,
          // and only a token with nowhere to break is split.
          "whitespace-pre-wrap break-words" +
          (attrs.onUndo ? " flex items-center gap-2" : ""),
      },
      [
        attrs.text,
        attrs.onUndo
          ? m(
              "button",
              {
                type: "button",
                class:
                  "-mr-1 inline-flex shrink-0 cursor-pointer items-center opacity-60 transition-opacity hover:opacity-100",
                "aria-label": "Change answer",
                "data-tooltip": "Change answer",
                "data-tooltip-placement": "above",
                onclick: attrs.onUndo,
              },
              m(Icon16, { name: "undo" }),
            )
          : null,
      ],
    ),
  );
}

/**
 * An agent turn: a bare paragraph, streamed. 100px short of the column so its
 * right edge lands inside the user's rather than flush with it. `id` names the
 * paragraph and `textId` a span around the words, for turns something outside
 * the page reads.
 */
export function agentTurn(attrs: {
  key: string;
  text: string;
  startAtMs: number;
  isInstant?: boolean;
  id?: string;
  textId?: string;
  /** A bold first line, streamed ahead of the text. */
  lead?: string;
  /** A type class for the whole turn, when it is not body text. */
  class?: string;
}): m.Children {
  const isInstant = attrs.isInstant ?? false;
  const lead = attrs.lead ?? "";
  const textAt = lead === "" ? attrs.startAtMs : attrs.startAtMs + streamDurationMs(lead) + CHAT_STREAM_STEP_MS;
  const text = streamedText(attrs.text, textAt, isInstant);
  const body = attrs.textId !== undefined ? m("span", { id: attrs.textId }, text) : text;
  return m(
    "p",
    {
      key: attrs.key,
      id: attrs.id,
      class:
        "mt-10 max-w-[calc(100%-100px)] leading-[1.5] whitespace-pre-wrap break-words first:mt-0" +
        (attrs.class !== undefined ? ` ${attrs.class}` : ""),
      "data-agent-turn": "",
    },
    lead === "" ? body : [m("strong", streamedText(lead, attrs.startAtMs, isInstant)), "\n", body],
  );
}

/**
 * An agent turn's list of points, each behind a chevron that opens a longer
 * explanation. The labels stream in one after another as the agent's own
 * words would; what opens is simply there, since the reader asked for it.
 */
export function disclosureList(attrs: {
  key: string;
  /** When the first label starts streaming; the rest follow as if one text. */
  startAtMs: number;
  /** Already on the page: the labels are simply there. */
  isInstant?: boolean;
  /** Fade the whole list in at this moment instead of streaming its labels. */
  arriveAtMs?: number;
  points: DisclosurePoint[];
  openIds: ReadonlySet<string>;
  onToggle: (id: string) => void;
  /** What opens under a point, in place of its plain detail text. */
  detailFor?: (point: DisclosurePoint) => m.Children;
}): m.Children {
  let labelAt = attrs.startAtMs;
  const arrival: ArrivalAttrs = { key: attrs.key, delayMs: attrs.arriveAtMs ?? 0, isInstant: attrs.arriveAtMs === undefined };
  const isLabelInstant = (attrs.isInstant ?? false) || attrs.arriveAtMs !== undefined;
  return m(
    "ul",
    {
      key: attrs.key,
      class: "mt-3 flex max-w-[calc(100%-100px)] flex-col gap-1.5" + arrivalClass(arrival),
      style: arrivalStyle(arrival),
      "data-disclosure-list": "",
    },
    attrs.points.map((point) => {
      const startAt = labelAt;
      labelAt += streamDurationMs(point.label) + CHAT_STREAM_STEP_MS;
      return m(
        "li",
        { key: point.id, "data-point": point.id },
        m(
          Disclosure,
          {
            isOpen: attrs.openIds.has(point.id),
            onToggle: () => attrs.onToggle(point.id),
            summary: streamedText(point.label, startAt, isLabelInstant),
            markerStartAtMs: isLabelInstant ? undefined : startAt,
            markerFadeMs: CHAT_STREAM_FADE_MS,
          },
          attrs.detailFor ? attrs.detailFor(point) : point.detail,
        ),
      );
    }),
  );
}

export interface AnswerButton {
  id: string;
  label: string;
  isEmphasized: boolean;
  onPress: () => void;
}

/**
 * The buttons a question offers, as the user's turn: right-aligned, a full
 * turn below the question. The emphasized answer is filled in the success
 * green; the quieter one is a ghost.
 */
export function answerRow(
  attrs: ArrivalAttrs & { buttons: AnswerButton[]; aside?: { label: string; onPress: () => void } },
): m.Children {
  const buttons = m(
    "div",
    { class: "flex items-stretch gap-3" },
    attrs.buttons.map((button) =>
      m(
        Button,
        {
          key: button.id,
          variant: button.isEmphasized ? "success" : "secondary",
          size: "lg",
          "data-answer": button.id,
          // The quieter answer is lighter in weight, but not in color: the
          // variant's own text-primary stands, so it does not read as disabled.
          extra: button.isEmphasized ? "" : "font-normal",
          onclick: button.onPress,
        },
        button.label,
      ),
    ),
  );
  // The quieter, agent-side way out sits at the row's left end, the answers at
  // its right: one line, the two sides of the conversation.
  return m(
    "div",
    {
      key: attrs.key,
      class: "mt-10 flex items-center " + (attrs.aside ? "justify-between gap-6" : "justify-end") + arrivalClass(attrs),
      style: arrivalStyle(attrs),
    },
    attrs.aside ? [asideButton(attrs.aside.label, attrs.aside.onPress), buttons] : buttons,
  );
}

function asideButton(label: string, onPress: () => void): m.Children {
  return m(
    "button",
    {
      type: "button",
      class: "type-body text-tertiary hover:text-primary hover:underline cursor-pointer bg-transparent border-0 p-0 text-left",
      "data-aside": "",
      onclick: onPress,
    },
    label,
  );
}

/**
 * The two answers side by side, as an agent would lay them out: markdown's
 * furniture and no more, a rule under the headings, 14px against the chat's
 * body. Each point carries a check, filled in the recommended column.
 */
export function choiceTable(attrs: ArrivalAttrs & { columns: TableColumn[] }): m.Children {
  return m(
    "div",
    { key: attrs.key, class: "mt-5 w-full max-w-[calc(100%-100px)]" + arrivalClass(attrs), style: arrivalStyle(attrs) },
    m("table", { class: "w-full table-fixed border-collapse text-left type-body" }, [
      m(
        "thead",
        m(
          "tr",
          { class: "border-b border-default" },
          attrs.columns.map((column) =>
            m("th", { key: column.title, class: "py-2 pr-6 align-top font-semibold" }, [
              column.title,
              column.badge
                ? m(
                    "span",
                    {
                      class:
                        "ml-2 inline-flex items-center rounded-md px-2 py-0.5 type-helper font-bold uppercase tracking-wide " +
                        (column.isEmphasized ? "bg-accent/15 text-accent" : "bg-fill-subtle text-secondary"),
                    },
                    column.badge,
                  )
                : null,
            ]),
          ),
        ),
      ),
      m(
        "tbody",
        m(
          "tr",
          attrs.columns.map((column) =>
            m(
              "td",
              { key: column.title, class: "py-3 pr-6 align-top leading-[1.45]" },
              m(
                "ul",
                { class: "flex flex-col gap-1.5" },
                column.points.map((point) =>
                  m("li", { key: point, class: "flex items-start gap-2" }, [
                    m(
                      "span",
                      { class: "shrink-0 leading-[1.45]", "aria-hidden": "true" },
                      column.isEmphasized ? "\u2705" : "\u2713",
                    ),
                    point,
                  ]),
                ),
              ),
            ),
          ),
        ),
      ),
    ]),
  );
}

/** What the scroller reads off the anchor; a real element is one, and tests pass a stub. */
export interface ScrollTarget {
  scrollIntoView?: (options: ScrollIntoViewOptions) => void;
}

/**
 * Keeps the transcript's end in view: on mount, and then once per change in
 * the number of turns.
 *
 * The count, rather than the anchor's layout position, is what says a turn
 * arrived or was undone. Position moves for reasons that are not a new turn --
 * opening a disclosure part-way up the transcript grows the column and pushes
 * the anchor down, and scrolling to the end there yanks the reader away from
 * the row they just opened. It also holds still through the creation page's
 * per-tick redraws.
 */
export class TranscriptScroller {
  private lastTurnCount: number | null = null;

  mounted(anchor: ScrollTarget, turnCount: number): void {
    this.lastTurnCount = turnCount;
    this.scrollTo(anchor);
  }

  updated(anchor: ScrollTarget, turnCount: number): void {
    const isUnchanged = turnCount === this.lastTurnCount;
    this.lastTurnCount = turnCount;
    // Either direction: a turn arriving scrolls, and so does an undo removing
    // one, which is how the undone row lands back in view.
    if (isUnchanged) return;
    this.scrollTo(anchor);
  }

  private scrollTo(anchor: ScrollTarget): void {
    anchor.scrollIntoView?.({ block: "end", behavior: "smooth" });
  }
}

const transcriptScroller = new TranscriptScroller();

/**
 * A sentinel the column scrolls to whenever `turnCount` changes. Its height is
 * the clearance ``block: "end"`` leaves under the last turn: without it an
 * answer row (or an undo button on the final bubble) comes to rest flush
 * against the bottom edge.
 */
export function scrollAnchor(turnCount: number): m.Children {
  return m("div", {
    key: "scroll-anchor",
    class: "h-8 shrink-0",
    "aria-hidden": "true",
    oncreate: (vnode: m.VnodeDOM) => transcriptScroller.mounted(vnode.dom as HTMLElement, turnCount),
    onupdate: (vnode: m.VnodeDOM) => transcriptScroller.updated(vnode.dom as HTMLElement, turnCount),
  });
}
