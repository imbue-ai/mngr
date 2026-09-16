import { describe, expect, it } from "vitest";
import type { ScrollTarget } from "./transcript";
import { TranscriptScroller } from "./transcript";

function anchorAt(offsetTop: number, scrolls: number[]): ScrollTarget {
  return { offsetTop, scrollIntoView: () => scrolls.push(offsetTop) };
}

describe("TranscriptScroller", () => {
  it("scrolls on mount, then only when the anchor's layout position moves", () => {
    const scrolls: number[] = [];
    const scroller = new TranscriptScroller();
    scroller.mounted(anchorAt(400, scrolls));
    expect(scrolls).toEqual([400]);
    // A redraw with nothing added (a progress tick, or the reader scrolling
    // the column, which leaves offsetTop alone) must not scroll again.
    scroller.updated(anchorAt(400, scrolls));
    expect(scrolls).toEqual([400]);
    scroller.updated(anchorAt(520, scrolls));
    expect(scrolls).toEqual([400, 520]);
  });

  it("a fresh mount scrolls even at the position the last anchor rested at", () => {
    const scrolls: number[] = [];
    const scroller = new TranscriptScroller();
    scroller.mounted(anchorAt(400, scrolls));
    scroller.mounted(anchorAt(400, scrolls));
    expect(scrolls).toEqual([400, 400]);
  });

  it("copes with an anchor that cannot scroll itself into view", () => {
    const scroller = new TranscriptScroller();
    expect(() => scroller.mounted({ offsetTop: 10 })).not.toThrow();
  });
});
