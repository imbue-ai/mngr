import m from "mithril";
import { describe, expect, it } from "vitest";
import type { ScrollTarget } from "./transcript";
import { TranscriptScroller, choiceTable } from "./transcript";

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
