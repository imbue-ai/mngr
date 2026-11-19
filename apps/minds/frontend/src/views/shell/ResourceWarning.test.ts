import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import { DeviceResourcesStore } from "../../models/deviceResources";
import type { AnyVnode } from "../../testing";
import {
  attrsOf,
  classTokensOf,
  deviceResourcesWireView as wireView,
  jsonResponse,
  renderDeep,
  renderedText,
} from "../../testing";
import { DeviceResourceWarning, DeviceResourceWarningSlot } from "./ResourceWarning";

const DISK_LOW = { resource: "disk", severity: "warning", headline: "Disk space is low", detail: "8.0 GB left" } as const;
const MEMORY_FULL = { resource: "memory", severity: "warning", headline: "Memory is nearly full", detail: "" } as const;

function storeWith(view: ReturnType<typeof wireView> | null): DeviceResourcesStore {
  const store = new DeviceResourcesStore(async () => jsonResponse(view));
  store.applyMessage({ view });
  return store;
}

function renderWarning(store: DeviceResourcesStore, onOpenSettings: () => void = () => {}): AnyVnode {
  return renderDeep(m(DeviceResourceWarning, { store, onOpenSettings })) as AnyVnode;
}

describe("the titlebar's resource warning", () => {
  it("draws nothing while no warning stands, or before the first frame", () => {
    expect(renderWarning(storeWith(wireView()))).toBeNull();
    expect(renderWarning(storeWith(null))).toBeNull();
  });

  it("says the most severe warning in words, counting the rest", () => {
    const pill = renderWarning(storeWith(wireView({ alerts: [DISK_LOW, MEMORY_FULL] })));
    expect(renderedText(pill as unknown as m.Vnode)).toContain("Disk space is low");
    expect(renderedText(pill as unknown as m.Vnode)).toContain("+1");
    expect(attrsOf(pill)["data-severity"]).toBe("warning");
  });

  it("opens the resource warning settings on a click", () => {
    const onOpenSettings = vi.fn();
    const pill = renderWarning(storeWith(wireView({ alerts: [DISK_LOW] })), onOpenSettings);
    (attrsOf(pill).onclick as () => void)();
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it("is drawn above the titlebar, over an invisible copy that keeps its place in the bar", () => {
    const store = storeWith(wireView({ alerts: [DISK_LOW] }));
    expect(classTokensOf(renderWarning(store))).toEqual(expect.arrayContaining(["fixed", "z-[120]"]));
    const slot = renderDeep(m(DeviceResourceWarningSlot, { store })) as AnyVnode;
    expect(attrsOf(slot)["aria-hidden"]).toBe("true");
    expect(classTokensOf(slot)).toContain("invisible");
  });
});
