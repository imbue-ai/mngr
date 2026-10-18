import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Electron unions the window's drag regions, so an overlay painted ABOVE the
// titlebar does not become clickable just by winning the z-order -- it has to
// subtract the titlebar's drag region itself. A backdrop that forgets to
// leaves everything it draws in the top 38px dead on macOS: the raised icon
// strip paints, it hovers, and the click drags the window instead. Nothing in
// a browser-based test can see that, which is exactly how it shipped once.

const SRC_DIR = join(import.meta.dirname, "..", "..");

/** Every source file under src/, so a new overlay is found wherever it lands. */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(path, out);
    else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".test.ts"))
      out.push(path);
  }
  return out;
}

describe("Electron drag regions", () => {
  it("subtracts the titlebar strip for every backdrop the app can raise", () => {
    const backdropIds = new Set<string>();
    for (const file of sourceFiles(SRC_DIR)) {
      const source = readFileSync(file, "utf8");
      for (const match of source.matchAll(
        /backdropId\s*:\s*"([a-z0-9-]+)"/g,
      ))
        backdropIds.add(match[1]);
    }
    // A refactor that renames the attr must not quietly empty this test.
    expect(backdropIds.size).toBeGreaterThan(0);

    const css = readFileSync(join(SRC_DIR, "style.css"), "utf8");
    const noDragRule = css.match(
      /((?:#[a-z0-9-]+,\s*)*#[a-z0-9-]+)\s*\{\s*-webkit-app-region:\s*no-drag;/g,
    );
    expect(noDragRule, "no -webkit-app-region: no-drag rule in style.css").not
      .toBeNull();
    const declared = (noDragRule ?? []).join(" ");

    const missing = [...backdropIds].filter(
      (id) => !declared.includes(`#${id}`),
    );
    expect(
      missing,
      `backdrops that would swallow clicks in the titlebar strip: ${missing.join(", ")}`,
    ).toEqual([]);
  });
});
