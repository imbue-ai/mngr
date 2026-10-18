import m from "mithril";
import { describe, expect, it } from "vitest";
import type { AnyVnode } from "../../../testing";
import { attrsOf, classesOf, collectText, collectVnodes } from "../../../testing";
import type { ScrollTarget } from "./transcript";
import { TranscriptScroller, agentTurn, choiceTable } from "./transcript";

function anchor(scrolls: string[], label: string): ScrollTarget {
  return { scrollIntoView: () => scrolls.push(label) };
}

describe("TranscriptScroller", () => {
  it("scrolls on mount, then only when the number of turns changes", () => {
    const scrolls: string[] = [];
    const scroller = new TranscriptScroller();
    scroller.mounted(anchor(scrolls, "mount"), 3);
    expect(scrolls).toEqual(["mount"]);
    // A redraw with no new turn (a creation-page progress tick, or the reader
    // scrolling the column) must not drag them back to the end.
    scroller.updated(anchor(scrolls, "tick"), 3);
    expect(scrolls).toEqual(["mount"]);
    scroller.updated(anchor(scrolls, "answered"), 4);
    expect(scrolls).toEqual(["mount", "answered"]);
  });

  it("stays put when a disclosure opens part-way up the transcript", () => {
    // Opening one grows the column and pushes the anchor down without adding a
    // turn. Scrolling to the end there yanks the reader off the row they just
    // opened.
    const scrolls: string[] = [];
    const scroller = new TranscriptScroller();
    scroller.mounted(anchor(scrolls, "mount"), 6);
    scroller.updated(anchor(scrolls, "disclosure-opened"), 6);
    scroller.updated(anchor(scrolls, "disclosure-closed"), 6);
    expect(scrolls).toEqual(["mount"]);
  });

  it("scrolls when an undo removes a turn, not just when one arrives", () => {
    const scrolls: string[] = [];
    const scroller = new TranscriptScroller();
    scroller.mounted(anchor(scrolls, "mount"), 5);
    scroller.updated(anchor(scrolls, "undone"), 4);
    expect(scrolls).toEqual(["mount", "undone"]);
  });

  it("a fresh mount scrolls even at the turn count the last one rested at", () => {
    const scrolls: string[] = [];
    const scroller = new TranscriptScroller();
    scroller.mounted(anchor(scrolls, "first"), 4);
    scroller.mounted(anchor(scrolls, "second"), 4);
    expect(scrolls).toEqual(["first", "second"]);
  });

  it("copes with an anchor that cannot scroll itself into view", () => {
    const scroller = new TranscriptScroller();
    expect(() => scroller.mounted({}, 1)).not.toThrow();
  });
});

describe("choiceTable", () => {
  /** The <th> cells of the header row. */
  function headerCells(table: m.Vnode): m.Vnode[] {
    const tableEl = (table.children as m.Vnode[])[0];
    const thead = (tableEl.children as m.Vnode[])[0];
    const row = (thead.children as m.Vnode[])[0];
    return row.children as m.Vnode[];
  }

  /** The badge pill inside a header cell, whose other child is the bare title. */
  function badgeClassOf(cell: m.Vnode): string {
    const badge = (cell.children as (m.Vnode | string | null)[]).find(
      (child) => child !== null && typeof child === "object" && child.tag === "span",
    ) as m.Vnode | undefined;
    return String((badge?.attrs as Record<string, unknown> | undefined)?.className ?? "");
  }

  it("paints the emphasized column's badge in the accent and the rest in grayscale", () => {
    // Both columns carry a badge; only the recommended one is blue, so the
    // other reads as a qualifier rather than a second recommendation.
    const table = choiceTable({
      key: "t",
      delayMs: 0,
      columns: [
        { title: "Imbue Cloud", badge: "Recommended", isEmphasized: true, points: ["a"] },
        { title: "Custom setup", badge: "Advanced", isEmphasized: false, points: ["b"] },
      ],
    }) as m.Vnode;
    const [cloud, custom] = headerCells(table);

    expect(badgeClassOf(cloud)).toContain("text-accent");
    expect(badgeClassOf(custom)).toContain("text-secondary");
    expect(badgeClassOf(custom)).not.toContain("text-accent");
  });
});

describe("agentTurn", () => {
  const OPENER = "Imbue Studio is honest software.";

  /** The turn's text, straight through, however many runs it is drawn in. */
  function textOf(turn: unknown): string {
    return collectText(turn).join("");
  }

  /** Each character's own delay, in order: the stream's schedule. */
  function delays(turn: unknown): number[] {
    return collectVnodes(turn)
      .filter((vnode) => classesOf(vnode).includes("start-char"))
      .map((vnode) => Number(/--start-char-delay: ([\d.]+)ms/.exec(String(attrsOf(vnode).style ?? ""))?.[1]));
  }

  function emOf(turn: unknown): AnyVnode | undefined {
    return collectVnodes(turn).find((vnode) => vnode.tag === "em");
  }

  it("draws the emphasised run in italics and leaves the rest of the sentence alone", () => {
    const turn = agentTurn({ key: "opener", text: OPENER, emphasis: "honest software", startAtMs: 0 });

    expect(textOf(emOf(turn))).toBe("honest software");
    expect(textOf(turn)).toBe(OPENER);
  });

  it("does not move the stream: every character lands when it would have anyway", () => {
    // The emphasis is how the turn is drawn, not when any of it arrives, so a
    // phrase picked out mid-sentence must not shift the characters after it.
    const plain = agentTurn({ key: "opener", text: OPENER, startAtMs: 150 });
    const emphasised = agentTurn({ key: "opener", text: OPENER, emphasis: "honest software", startAtMs: 150 });

    expect(delays(emphasised)).toEqual(delays(plain));
  });

  it("falls back to the plain stream when the phrase is not in the text", () => {
    const turn = agentTurn({ key: "opener", text: OPENER, emphasis: "not in there", startAtMs: 0 });

    expect(emOf(turn)).toBeUndefined();
    expect(textOf(turn)).toBe(OPENER);
  });
});
