import { describe, expect, it, vi } from "vitest";
import { appIconUrl, requestWallpaperRefresh, wallpaperUrl } from "./workspaceAssets";

describe("the workspace asset URLs", () => {
  it("names an app's icon under its version, and nothing for an app with no usable icon", () => {
    expect(
      appIconUrl("agent-aa11", { name: "reading list", display_name: "Reading List", label: "", icon_version: "abc123" }),
    ).toBe("/ui/api/workspaces/agent-aa11/apps/reading%20list/icon.svg?v=abc123");
    expect(appIconUrl("agent-aa11", { name: "zen", display_name: "Zen", label: "", icon_version: "" })).toBeNull();
    expect(appIconUrl("agent-aa11", { name: "zen", display_name: "Zen" })).toBeNull();
  });

  it("names the cached wallpaper under its version, and nothing when none is cached", () => {
    expect(wallpaperUrl("agent-aa11", "deadbeef0123")).toBe("/ui/api/workspaces/agent-aa11/wallpaper?v=deadbeef0123");
    expect(wallpaperUrl("agent-aa11", "")).toBeNull();
  });
});

describe("requestWallpaperRefresh", () => {
  it("posts the refresh under the workspace, and swallows a failure with a warning", async () => {
    const calls: [string, RequestInit | undefined][] = [];
    const fetchImpl = ((url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return Promise.resolve(new Response("{}", { status: 202 }));
    }) as unknown as typeof fetch;
    await requestWallpaperRefresh("agent-aa11", fetchImpl);
    expect(calls).toEqual([
      ["/ui/api/workspaces/agent-aa11/wallpaper/refresh", { method: "POST", credentials: "same-origin" }],
    ]);

    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const failing = (() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
    await expect(requestWallpaperRefresh("agent-aa11", failing)).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(1);
    // A refusal (no forward to read through, a shared row's id) is warned about too, not taken for a 202.
    const refusing = (() => Promise.resolve(new Response("{}", { status: 503 }))) as unknown as typeof fetch;
    await expect(requestWallpaperRefresh("agent-aa11", refusing)).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(2);
    expect(String(warn.mock.calls[1][0])).toContain("503");
    warn.mockRestore();
  });
});
