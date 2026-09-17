import { describe, expect, it } from "vitest";
import { computeTooltipPosition } from "./tooltips";

const VIEWPORT = { width: 1000, height: 800 };

describe("computeTooltipPosition", () => {
  it("centers the bubble under a trigger with room below", () => {
    // Trigger 20px wide at x=100, bottom at y=50; bubble 80px wide.
    const pos = computeTooltipPosition({ left: 100, top: 30, bottom: 50, width: 20 }, { width: 80, height: 24 }, VIEWPORT);
    // Centered: 100 + 10 - 40 = 70. Below the trigger with the 6px gap: 50 + 6.
    expect(pos.left).toBe(70);
    expect(pos.top).toBe(56);
  });

  it("flips the bubble above the trigger when it would overflow the bottom", () => {
    // Trigger bottom near the viewport floor; the bubble cannot fit below.
    const pos = computeTooltipPosition({ left: 500, top: 760, bottom: 790, width: 40 }, { width: 100, height: 24 }, VIEWPORT);
    // Flipped above: top - height - gap = 760 - 24 - 6.
    expect(pos.top).toBe(730);
  });

  it("keeps the bubble below when there is no room above either", () => {
    // A bubble taller than the space above the trigger: below overflows, but
    // above is off-screen too, so it stays below.
    const pos = computeTooltipPosition({ left: 10, top: 5, bottom: 30, width: 20 }, { width: 60, height: 790 }, VIEWPORT);
    expect(pos.top).toBe(30 + 6);
  });

  it("puts the bubble above the trigger when that placement is asked for", () => {
    // Room on both sides; "above" must not fall through to the default below.
    const pos = computeTooltipPosition(
      { left: 100, top: 400, bottom: 420, width: 20 },
      { width: 80, height: 24 },
      VIEWPORT,
      "above",
    );
    // Centered: 100 + 10 - 40 = 70. Above the trigger with the 6px gap: 400 - 24 - 6.
    expect(pos.left).toBe(70);
    expect(pos.top).toBe(370);
  });

  it("drops an above-placed bubble below when it does not fit above", () => {
    // Trigger near the ceiling: above is off-screen, and the bubble fits below.
    const pos = computeTooltipPosition(
      { left: 100, top: 10, bottom: 30, width: 20 },
      { width: 80, height: 24 },
      VIEWPORT,
      "above",
    );
    expect(pos.top).toBe(36);
  });

  it("keeps an above-placed bubble above when neither side has room", () => {
    // Taller than the space above AND than the space below, so the fallback is
    // refused and the clamp pins it to the top margin.
    const pos = computeTooltipPosition(
      { left: 100, top: 10, bottom: 30, width: 20 },
      { width: 80, height: 790 },
      VIEWPORT,
      "above",
    );
    expect(pos.top).toBe(6);
  });

  it("clamps the bubble to the right edge with a margin", () => {
    // A wide bubble under a trigger near the right edge is pulled left so it
    // ends 6px from the edge: 1000 - 6 - 300.
    const pos = computeTooltipPosition({ left: 950, top: 30, bottom: 50, width: 20 }, { width: 300, height: 24 }, VIEWPORT);
    expect(pos.left).toBe(694);
  });

  it("clamps the bubble to the left edge with a margin", () => {
    // Centering under a trigger flush to the left edge yields a negative left;
    // it snaps to the 6px margin.
    const pos = computeTooltipPosition({ left: 0, top: 30, bottom: 50, width: 10 }, { width: 120, height: 24 }, VIEWPORT);
    expect(pos.left).toBe(6);
  });
});
