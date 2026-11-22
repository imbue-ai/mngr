import { describe, expect, it } from "vitest";
import { FirstRunChromeModel, PAGE_DOWN_SETTLE_MS } from "./firstRunChrome";
import type { WashOrigin } from "./wash";

interface StartedWash {
  accent: string;
  origin: WashOrigin;
  onSwap: () => void;
  isInstant: boolean;
}

function harness(isInstant = false) {
  const chromeClass: boolean[] = [];
  const washes: StartedWash[] = [];
  const delays: number[] = [];
  const model = new FirstRunChromeModel({
    setChromeClass: (isOn) => chromeClass.push(isOn),
    startWash: (accent, origin, _viewport, onSwap, _coverUntil, instant) =>
      washes.push({ accent, origin, onSwap, isInstant: instant }),
    viewport: () => ({ width: 1200, height: 800 }),
    isInstant: () => isInstant,
    delay: (ms) => {
      delays.push(ms);
      return Promise.resolve();
    },
    redraw: () => undefined,
  });
  return { model, chromeClass, washes, delays };
}

describe("FirstRunChromeModel", () => {
  it("holds the chrome cream while the page is up and washes the accent from the center when it ends", () => {
    const { model, chromeClass, washes, delays } = harness();
    model.hold("agent-1");
    expect(chromeClass).toEqual([true]);
    expect(model.holder).toBe("agent-1");

    model.exit("agent-1", "#492222", 560);
    expect(washes).toHaveLength(1);
    expect(washes[0]).toMatchObject({ accent: "#492222", origin: { x: 600, y: 400 }, isInstant: false });
    // The cream holds until the color covers the window.
    expect(chromeClass).toEqual([true]);
    expect(delays).toEqual([560 + PAGE_DOWN_SETTLE_MS]);

    washes[0].onSwap();
    expect(chromeClass).toEqual([true, false]);
    expect(model.holder).toBeNull();
  });

  it("keeps the color up for the page to come down even when motion is reduced", () => {
    const { model, washes, delays } = harness(true);
    model.hold("agent-1");
    model.exit("agent-1", "#492222", 560);
    expect(washes[0].isInstant).toBe(true);
    expect(delays).toEqual([560 + PAGE_DOWN_SETTLE_MS]);
  });

  it("washes nothing for a workspace that did not hold the chrome, and releases one with no known accent", () => {
    const { model, chromeClass, washes } = harness();
    model.exit("agent-1", "#492222", 560);
    expect(washes).toEqual([]);

    model.hold("agent-1");
    model.exit("agent-2", "#492222", 560);
    expect(washes).toEqual([]);
    expect(model.holder).toBe("agent-1");

    model.exit("agent-1", null, 560);
    expect(washes).toEqual([]);
    expect(chromeClass).toEqual([true, false]);
  });

  it("releases only for the workspace that holds it", () => {
    const { model, chromeClass } = harness();
    model.hold("agent-1");
    model.release("agent-2");
    expect(chromeClass).toEqual([true]);
    model.release("agent-1");
    expect(chromeClass).toEqual([true, false]);
  });
});
