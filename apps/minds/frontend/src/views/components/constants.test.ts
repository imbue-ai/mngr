import { describe, expect, it } from "vitest";
import {
  BTN_BASE,
  BTN_SIZES,
  BTN_VARIANTS,
  buttonClass,
  INPUT_BASE,
} from "./constants";

describe("buttonClass", () => {
  it("composes base + size + variant in order, matching the JinjaX assembly", () => {
    const cls = buttonClass("secondary", "md", false, "");
    expect(cls.startsWith(BTN_BASE)).toBe(true);
    expect(cls).toContain(BTN_SIZES.md);
    expect(cls).toContain(BTN_VARIANTS.secondary);
    expect(cls).not.toContain("w-full");
  });

  it("adds the gentler full-width press scale for block buttons", () => {
    const cls = buttonClass("primary", "lg", true, "");
    expect(cls).toContain("w-full active:!scale-[0.99]");
  });

  it("appends caller extras last so they can override earlier utilities", () => {
    const cls = buttonClass("ghost", "icon", false, "mt-4");
    expect(cls.endsWith(" mt-4")).toBe(true);
  });
});

describe("ported constant strings", () => {
  it("keeps the legacy focus-ring recipe in BTN_BASE", () => {
    expect(BTN_BASE).toContain("focus-visible:outline-accent");
    expect(BTN_BASE).toContain("active:scale-[0.98]");
  });

  it("keeps the legacy input shell in INPUT_BASE", () => {
    expect(INPUT_BASE).toContain("border border-strong");
    expect(INPUT_BASE).toContain("placeholder:text-tertiary");
  });

  it("keeps all five button variants with a border on every variant", () => {
    for (const variant of Object.values(BTN_VARIANTS)) {
      expect(variant).toContain("border");
    }
  });
});
