import { describe, expect, it } from "vitest";
import { QR_QUIET_ZONE_MODULES, qrModules, qrPathData } from "./qrCode";

// Nineteen bytes: more than a version 1 code holds at level M (fourteen), and
// within a version 2 code's twenty-six, so the code is 25 modules a side.
const SHORT_URL = "https://a.example/x";
const VERSION_2_SIDE = 25;

/** A finder pattern: a 7x7 dark ring, a light ring inside it, and a 3x3 dark core. */
const FINDER_PATTERN = [
  "#######",
  "#.....#",
  "#.###.#",
  "#.###.#",
  "#.###.#",
  "#.....#",
  "#######",
];

/** The 7x7 block whose top-left module is at (`top`, `left`), drawn as the pattern above is. */
function blockAt(modules: boolean[][], top: number, left: number): string[] {
  return modules
    .slice(top, top + 7)
    .map((row) => row.slice(left, left + 7).map((isDark) => (isDark ? "#" : ".")).join(""));
}

describe("qrModules", () => {
  it("is a square of the smallest version that holds the text", () => {
    const modules = qrModules(SHORT_URL);

    expect(modules).toHaveLength(VERSION_2_SIDE);
    for (const row of modules) expect(row).toHaveLength(VERSION_2_SIDE);
  });

  it("encodes the same text the same way every time, and another text differently", () => {
    expect(qrModules(SHORT_URL)).toEqual(qrModules(SHORT_URL));
    expect(qrModules(SHORT_URL)).not.toEqual(qrModules("https://a.example/y"));
  });

  it("has a finder pattern in three corners and none in the fourth", () => {
    const modules = qrModules(SHORT_URL);
    const far = VERSION_2_SIDE - 7;

    expect(blockAt(modules, 0, 0)).toEqual(FINDER_PATTERN);
    expect(blockAt(modules, 0, far)).toEqual(FINDER_PATTERN);
    expect(blockAt(modules, far, 0)).toEqual(FINDER_PATTERN);
    expect(blockAt(modules, far, far)).not.toEqual(FINDER_PATTERN);
  });

  it("holds what error correction level M holds: fourteen bytes in a version 1 code, twenty-six in a version 2", () => {
    const sideFor = (byteCount: number): number => qrModules("x".repeat(byteCount)).length;

    // Level L would keep fifteen bytes in version 1, and level Q would need version 2 for fourteen.
    expect([14, 15].map(sideFor)).toEqual([21, VERSION_2_SIDE]);
    // Level Q would need version 3 for twenty-six bytes, and level L would keep twenty-seven in version 2.
    expect([26, 27].map(sideFor)).toEqual([VERSION_2_SIDE, 29]);
  });

  it("says it is level M in its format information", () => {
    const modules = qrModules(SHORT_URL);

    // The level's two bits lead the format information, drawn beside the top-left finder at row 8, columns 0 and
    // 1; as the standard masks them, M reads dark then light (L is dark dark, Q light dark, H light light).
    expect([modules[8][0], modules[8][1]]).toEqual([true, false]);
  });

  it("grows with the text", () => {
    const long = `https://a.example/${"x".repeat(200)}`;

    expect(qrModules(long).length).toBeGreaterThan(VERSION_2_SIDE);
  });
});

describe("qrPathData", () => {
  it("draws one unit square per dark module, offset by the quiet zone", () => {
    const path = qrPathData([
      [true, false],
      [false, true],
    ]);

    expect(QR_QUIET_ZONE_MODULES).toBe(4);
    expect(path).toBe("M4 4h1v1h-1zM5 5h1v1h-1z");
  });

  it("draws nothing for a matrix with no dark module", () => {
    expect(qrPathData([[false, false]])).toBe("");
  });

  it("draws as many squares as a code has dark modules", () => {
    const modules = qrModules(SHORT_URL);
    const darkCount = modules.flat().filter((isDark) => isDark).length;

    expect(qrPathData(modules).split("M").length - 1).toBe(darkCount);
  });
});
