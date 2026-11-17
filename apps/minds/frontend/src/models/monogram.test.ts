import { describe, expect, it } from "vitest";
import { MONOGRAM_COLORS, monogramColor, monogramLetters } from "./monogram";

describe("monograms", () => {
  it("takes the first letters of the first two words, upper-cased, and a question mark for nothing", () => {
    expect(monogramLetters("Reading List")).toBe("RL");
    expect(monogramLetters("reading-list")).toBe("RL");
    expect(monogramLetters("eval_dashboard v2")).toBe("ED");
    expect(monogramLetters("zenbox")).toBe("Z");
    expect(monogramLetters("   ")).toBe("?");
  });

  it("gives the same name the same palette colour every time, and different names different ones in general", () => {
    expect(monogramColor("Reading List")).toBe(monogramColor("Reading List"));
    expect(MONOGRAM_COLORS).toContain(monogramColor("Reading List"));
    const colours = new Set(["alpha", "beta", "gamma", "delta", "epsilon"].map(monogramColor));
    expect(colours.size).toBeGreaterThan(1);
  });
});
