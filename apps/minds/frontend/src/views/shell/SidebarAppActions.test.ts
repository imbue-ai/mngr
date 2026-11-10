// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import { AppShortcutsModel } from "../../models/appShortcuts";
import { createEmptyStores } from "../../models/boot";
import { ShellState } from "./shell-state";
import { SidebarAppActions } from "./SidebarMenu";

// main's zoom-change listeners, kept across tests: the display zoom fan-out
// registers with the first desktop surface it meets and keeps that listener.
const zoomChangedListeners: ((percent: number) => void)[] = [];

let root: HTMLElement | null = null;

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  delete window.mindsNative;
});

interface NativeCalls {
  steps: string[];
  newWindows: number;
  zoomReads?: number;
}

function onPlatform(platform: string, calls: NativeCalls, zoomPercent = 100): void {
  window.mindsNative = {
    platform,
    openNewWindow: () => void (calls.newWindows += 1),
    getDisplayZoom: () => {
      calls.zoomReads = (calls.zoomReads ?? 0) + 1;
      return Promise.resolve(zoomPercent);
    },
    stepDisplayZoom: (direction: string) => {
      calls.steps.push(direction);
      return Promise.resolve(direction === "in" ? 110 : direction === "out" ? 90 : 100);
    },
    onDisplayZoomChanged: (callback: (percent: number) => void) => void zoomChangedListeners.push(callback),
    setAppShortcuts: () => undefined,
  } as unknown as NonNullable<Window["mindsNative"]>;
}

async function flushPromises(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await Promise.resolve();
}

async function mountActions(shell: ShellState = new ShellState(createEmptyStores())): Promise<HTMLElement> {
  const mounted = document.createElement("div");
  document.body.appendChild(mounted);
  root = mounted;
  m.mount(mounted, { view: () => m(SidebarAppActions, { shell }) });
  await flushPromises();
  m.redraw.sync();
  return mounted;
}

function zoomPercentShown(mounted: HTMLElement): string {
  return mounted.querySelector("#sidebar-zoom-reset")?.textContent ?? "";
}

describe("SidebarAppActions", () => {
  it("offers New window with its shortcut and opens one", async () => {
    const calls: NativeCalls = { steps: [], newWindows: 0 };
    onPlatform("win32", calls);
    const shell = new ShellState(createEmptyStores());
    shell.isSidebarOpen = true;
    const mounted = await mountActions(shell);

    const button = mounted.querySelector("#sidebar-new-window") as HTMLButtonElement;
    const label = button.textContent;
    button.click();

    expect(label).toBe("New windowCtrl+N");
    expect(calls.newWindows).toBe(1);
    expect(shell.isSidebarOpen).toBe(false);
  });

  it("shows no shortcut beside New window when it is Off", async () => {
    onPlatform("linux", { steps: [], newWindows: 0 });
    const route = async (): Promise<Response> =>
      new Response(JSON.stringify({ keybindings: { "imbue-studio": { "new-window": [] } } }), { status: 200 });
    const appShortcuts = new AppShortcutsModel({ fetchImpl: route });
    appShortcuts.followAccount("local");
    await flushPromises();
    const mounted = await mountActions(new ShellState(createEmptyStores(), undefined, undefined, appShortcuts));

    expect(mounted.querySelector("#sidebar-new-window")?.textContent).toBe("New window");
  });

  it("steps the zoom and keeps the percent current wherever it changes", async () => {
    const calls: NativeCalls = { steps: [], newWindows: 0 };
    onPlatform("win32", calls, 125);
    const mounted = await mountActions();
    const percentAtFirst = zoomPercentShown(mounted);

    (mounted.querySelector('[aria-label="Zoom in"]') as HTMLButtonElement).click();
    await flushPromises();
    m.redraw.sync();
    const percentAfterStep = zoomPercentShown(mounted);
    for (const listener of zoomChangedListeners) listener(175);
    m.redraw.sync();
    const percentAfterChangeElsewhere = zoomPercentShown(mounted);
    (mounted.querySelector('[aria-label="Zoom out"]') as HTMLButtonElement).click();
    (mounted.querySelector("#sidebar-zoom-reset") as HTMLButtonElement).click();
    await flushPromises();

    expect(percentAtFirst).toBe("125%");
    expect(percentAfterStep).toBe("110%");
    expect(percentAfterChangeElsewhere).toBe("175%");
    expect(calls.steps).toEqual(["in", "out", "reset"]);
  });

  it("offers neither on a Mac, whose menu bar has them", async () => {
    const calls: NativeCalls = { steps: [], newWindows: 0 };
    onPlatform("darwin", calls);
    const mounted = await mountActions();

    expect(mounted.querySelector("#sidebar-new-window")).toBeNull();
    expect(mounted.querySelector("#sidebar-zoom")).toBeNull();
    expect(calls.zoomReads).toBeUndefined();
  });

  it("offers neither in a browser", async () => {
    const mounted = await mountActions();

    expect(mounted.querySelector("#sidebar-new-window")).toBeNull();
    expect(mounted.querySelector("#sidebar-zoom")).toBeNull();
  });
});
