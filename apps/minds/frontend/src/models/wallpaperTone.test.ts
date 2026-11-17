import { afterEach, describe, expect, it, vi } from "vitest";
import { lightnessOf, toneFor, wallpaperToneFor } from "./wallpaperTone";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the wallpaper tone", () => {
  it("measures lightness on the CIE scale: black is 0, white is 100, mid grey about 53", () => {
    expect(lightnessOf(0, 0, 0)).toBe(0);
    expect(lightnessOf(255, 255, 255)).toBeCloseTo(100, 0);
    expect(lightnessOf(128, 128, 128)).toBeCloseTo(53.6, 0);
  });

  it("pushes a light strip lighter (for black text) and anything else darker (for white text)", () => {
    expect(toneFor(240, 230, 200)).toBe("lch(from rgb(240 230 200) max(l, 85) c h)");
    expect(toneFor(100, 120, 140)).toBe("lch(from rgb(100 120 140) min(l, 20) c h)");
    // Mid-tones lean white text: a ray-lit photo must not get black text.
    expect(toneFor(160, 160, 160)).toContain("min(l, 20)");
  });

  it("answers nothing where no image can be loaded, without starting a load twice", () => {
    vi.stubGlobal("Image", undefined);
    const onLoaded = vi.fn();
    expect(wallpaperToneFor("/ui/api/workspaces/agent-x/wallpaper?v=1", onLoaded)).toBeNull();
    expect(onLoaded).not.toHaveBeenCalled();
  });

  it("remembers an image that failed to load, so a redraw does not fetch it again", () => {
    let loads = 0;
    class FailingImage {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(_url: string) {
        loads += 1;
        this.onerror?.();
      }
    }
    vi.stubGlobal("Image", FailingImage);
    const onLoaded = vi.fn();
    const url = "/ui/api/workspaces/agent-y/wallpaper?v=broken";
    expect(wallpaperToneFor(url, onLoaded)).toBeNull();
    expect(wallpaperToneFor(url, onLoaded)).toBeNull();
    expect(loads).toBe(1);
    expect(onLoaded).not.toHaveBeenCalled();
  });
});
