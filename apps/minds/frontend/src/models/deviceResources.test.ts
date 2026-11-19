import { afterEach, describe, expect, it, vi } from "vitest";
import { deviceResourcesWireView as wireView, jsonResponse } from "../testing";
import { DeviceResourcesStore, STALE_AFTER_MS, describeUsage, viewFromWire } from "./deviceResources";

afterEach(() => {
  vi.useRealTimers();
});

describe("this computer's resource readings in words", () => {
  it("says each resource's use out of its whole", () => {
    const readings = viewFromWire(wireView()).readings;
    expect(describeUsage(readings, "disk")).toBe("400 GB of 500 GB disk");
    expect(describeUsage(readings, "memory")).toBe("12 GB of 16 GB RAM");
    expect(describeUsage(readings, "cpu")).toBe("25% of CPU");
  });

  it("says when a resource could not be read", () => {
    const readings = viewFromWire(wireView({ readings: { cpu: null, memory: null, disk: null } })).readings;
    expect(describeUsage(readings, "disk")).toBe("Disk use unknown");
    expect(describeUsage(readings, "cpu")).toBe("CPU use unknown");
  });
});

describe("DeviceResourcesStore", () => {
  it("reads as stale once the backend has been silent too long", () => {
    let now = 1_000;
    const store = new DeviceResourcesStore(async () => jsonResponse({}), () => now);
    store.applyMessage({ view: wireView() });
    expect(store.isStale).toBe(false);
    now += STALE_AFTER_MS + 1;
    expect(store.isStale).toBe(true);
    store.applyMessage({ view: null });
    expect(store.view).toBeNull();
  });

  it("saves the levels and takes the backend's answer, or throws its reason", async () => {
    const requests: { url: string; body: unknown }[] = [];
    let answer = jsonResponse(wireView({ settings: { ...wireView().settings, cpu_warning_percent: 80 } }));
    const store = new DeviceResourcesStore(async (url, init) => {
      requests.push({ url, body: JSON.parse(String(init?.body)) });
      return answer;
    });
    store.applyMessage({ view: wireView() });
    const redraw = vi.fn();

    await store.saveSettings({ ...store.view!.settings, cpuWarningPercent: 80 }, redraw);

    expect(requests).toEqual([
      {
        url: "/ui/api/device-resources/settings",
        body: { disk_warning_free_gb: 10, memory_warning_percent: null, cpu_warning_percent: 80 },
      },
    ]);
    expect(store.view!.settings.cpuWarningPercent).toBe(80);
    expect(redraw).toHaveBeenCalled();

    answer = jsonResponse({ error: "The warning levels must be within range." }, 400);
    await expect(store.saveSettings(store.view!.settings, redraw)).rejects.toThrow(
      "The warning levels must be within range.",
    );
  });

  it("refreshes the readings every few seconds while asked, and stops", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const store = new DeviceResourcesStore(async () => {
      calls += 1;
      return jsonResponse(wireView());
    });
    store.startLiveRefresh(() => {});
    store.startLiveRefresh(() => {});
    await vi.advanceTimersByTimeAsync(10_000);
    expect(calls).toBe(3);
    store.stopLiveRefresh();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(calls).toBe(3);
  });
});
