import { afterEach, describe, expect, it, vi } from "vitest";
import { stubSessionStorage } from "../testing";
import { isHomeTab, readRememberedHomeTab, rememberHomeTab } from "./homeTab";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the remembered home tab", () => {
  it("names the three tabs and nothing else", () => {
    expect(isHomeTab("workspaces")).toBe(true);
    expect(isHomeTab("apps")).toBe(true);
    expect(isHomeTab("invitations")).toBe(true);
    expect(isHomeTab("settings")).toBe(false);
    expect(isHomeTab(null)).toBe(false);
  });

  it("reads back what was remembered, and Workspaces for nothing, nonsense, or no storage at all", () => {
    expect(readRememberedHomeTab()).toBe("workspaces");
    const store = stubSessionStorage();
    expect(readRememberedHomeTab()).toBe("workspaces");
    rememberHomeTab("apps");
    expect(store.get("home-tab")).toBe("apps");
    expect(readRememberedHomeTab()).toBe("apps");
    store.set("home-tab", "nonsense");
    expect(readRememberedHomeTab()).toBe("workspaces");
  });

  it("survives a storage that refuses writes", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota");
      },
    });
    expect(() => rememberHomeTab("apps")).not.toThrow();
    expect(readRememberedHomeTab()).toBe("workspaces");
  });
});
