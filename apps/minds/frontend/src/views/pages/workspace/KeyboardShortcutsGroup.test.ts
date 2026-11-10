// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  getAppContext,
  registerAppContext,
} from "../../../app-context";
import { AppShortcutsModel } from "../../../models/appShortcuts";
import { createEmptyStores } from "../../../models/boot";
import { ShellState } from "../../shell/shell-state";
import {
  CHROME_SHORTCUT_SAVE_FAILED_MESSAGE,
  CHROME_SHORTCUT_UNKNOWN_MESSAGE,
  KEYBOARD_SHORTCUTS_READY_TIMEOUT_MS,
  KeyboardShortcutsGroup,
  type KeyboardShortcutsContract,
} from "./KeyboardShortcutsGroup";

const AGENT_ID = "agent-" + "c".repeat(8);
const WORKSPACE_ORIGIN = `http://${AGENT_ID}.localhost:8421`;
const PAGE_REPORT = "minds:keyboard-shortcuts-page";
const CAPABILITIES = "minds:embedder-capabilities";
const RECORD_CHORD = "minds:record-chord";
const CHROME_SHORTCUTS = "minds:chrome-shortcuts";
const SAVE_CHROME_SHORTCUT = "minds:save-chrome-shortcut";

// What the group sent the framed page, through the stand-in contract below.
let sentToPage: { type: string; payload?: Record<string, unknown> }[] = [];

// A stand-in for the Flask-served contract module (which the test bundle cannot import): it hands a message on
// only from the frame's window at an origin the group accepts, as the module does; the module's own validation
// is covered by apps/minds/test/unit/embed-contract.test.js.
function standInContract(): KeyboardShortcutsContract {
  return {
    KEYBOARD_SHORTCUTS_PAGE: PAGE_REPORT,
    EMBEDDER_CAPABILITIES: CAPABILITIES,
    RECORD_CHORD,
    CHROME_SHORTCUTS,
    SAVE_CHROME_SHORTCUT,
    createEmbedderEndpoint: ({
      getFrameWindow,
      isExpectedOrigin,
      handlers,
    }) => {
      const onMessage = (event: MessageEvent): void => {
        if (
          event.source !== getFrameWindow() ||
          !isExpectedOrigin(event.origin)
        )
          return;
        const data = event.data as Record<string, unknown>;
        handlers[String(data.type)]?.(data);
      };
      window.addEventListener("message", onMessage);
      return {
        send: (type, payload) => void sentToPage.push({ type, payload }),
        dispose: () => window.removeEventListener("message", onMessage),
      };
    },
  };
}

async function flushPromises(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await Promise.resolve();
}

let root: HTMLElement | null = null;

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  clearAppContextForTests();
  vi.useRealTimers();
  sentToPage = [];
  delete window.mindsNative;
});

async function mountGroup(
  onOpenUpdates: () => void = () => undefined,
  loadContract: () => Promise<KeyboardShortcutsContract> = () =>
    Promise.resolve(standInContract()),
  appShortcuts: AppShortcutsModel = new AppShortcutsModel(),
): Promise<HTMLElement> {
  const shell = new ShellState(createEmptyStores(), undefined, undefined, appShortcuts);
  registerAppContext({ stores: shell.stores, shell });
  const mounted = document.createElement("div");
  document.body.appendChild(mounted);
  root = mounted;
  m.mount(mounted, {
    view: () =>
      m(KeyboardShortcutsGroup, {
        agentId: AGENT_ID,
        onOpenUpdates,
        loadContract,
      }),
  });
  await flushPromises();
  return mounted;
}

function frame(mounted: HTMLElement): HTMLIFrameElement {
  return mounted.querySelector(
    "#keyboard-shortcuts-frame",
  ) as HTMLIFrameElement;
}

function pageState(mounted: HTMLElement): string | null {
  return (
    mounted
      .querySelector("#keyboard-shortcuts-group")
      ?.getAttribute("data-page-state") ?? null
  );
}

function postFrom(
  source: Window | null,
  origin: string,
  report: { height: number; isRecording: boolean } = {
    height: 512,
    isRecording: false,
  },
): void {
  window.dispatchEvent(
    new MessageEvent("message", {
      source,
      origin,
      data: { type: PAGE_REPORT, ...report },
    }),
  );
  m.redraw.sync();
}

describe("KeyboardShortcutsGroup", () => {
  it("frames the workspace's keyboard shortcuts page and keeps it hidden while it loads", async () => {
    const mounted = await mountGroup();

    expect(pageState(mounted)).toBe("loading");
    expect(frame(mounted).classList.contains("hidden")).toBe(true);
    const bridgeNext =
      new URL(frame(mounted).src, "http://chrome").searchParams.get("next") ??
      "";
    expect(bridgeNext).toBe(
      `/goto/${AGENT_ID}/?next=${encodeURIComponent("/keyboard-shortcuts")}`,
    );
    const sandbox = frame(mounted).getAttribute("sandbox") ?? "";
    expect(sandbox).toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-top-navigation");
  });

  it("shows the page at the height it reports once the frame says it loaded from a workspace origin", async () => {
    const mounted = await mountGroup();

    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN, {
      height: 512,
      isRecording: false,
    });

    expect(pageState(mounted)).toBe("ready");
    expect(frame(mounted).classList.contains("hidden")).toBe(false);
    expect(frame(mounted).style.height).toBe("512px");
    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN, {
      height: 760,
      isRecording: false,
    });
    expect(frame(mounted).style.height).toBe("760px");
  });

  it("tells the chrome while the page records, and forgets that when the group closes", async () => {
    const mounted = await mountGroup();
    const shell = getAppContext().shell;

    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN, {
      height: 512,
      isRecording: true,
    });
    const isRecordingWhileListening = shell.isWorkspaceShortcutRecording;
    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN, {
      height: 512,
      isRecording: false,
    });
    const isRecordingAfter = shell.isWorkspaceShortcutRecording;
    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN, {
      height: 512,
      isRecording: true,
    });
    m.mount(mounted, null);

    expect(isRecordingWhileListening).toBe(true);
    expect(isRecordingAfter).toBe(false);
    expect(shell.isWorkspaceShortcutRecording).toBe(false);
  });

  it("answers the page's first report with what the chrome can do, so the page reads its shortcuts as a browser's", async () => {
    const mounted = await mountGroup();

    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN);
    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN);

    expect(sentToPage).toEqual([
      { type: CAPABILITIES, payload: { canPopOut: false } },
    ]);
  });

  it("in the desktop app, tells main while the page records, and hands the page a key main took for it", async () => {
    const recording: boolean[] = [];
    const recordChordListeners: ((chord: string) => void)[] = [];
    window.mindsNative = {
      openPopoutWindow: () => undefined,
      setShellShortcuts: () => undefined,
      setShortcutRecording: (isRecording: boolean) =>
        void recording.push(isRecording),
      onRecordChord: (callback: (chord: string) => void) =>
        void recordChordListeners.push(callback),
    } as unknown as NonNullable<Window["mindsNative"]>;
    const mounted = await mountGroup();

    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN, {
      height: 512,
      isRecording: true,
    });
    for (const listener of recordChordListeners) listener("alt+f4");
    m.mount(mounted, null);

    expect(sentToPage).toEqual([
      { type: CAPABILITIES, payload: { canPopOut: true } },
      { type: RECORD_CHORD, payload: { chord: "alt+f4" } },
    ]);
    expect(recording).toEqual([true, false]);
  });

  it("ignores the message from anything but its frame at a workspace origin", async () => {
    const mounted = await mountGroup();

    postFrom(frame(mounted).contentWindow, "https://example.com");
    postFrom(window, WORKSPACE_ORIGIN);

    expect(pageState(mounted)).toBe("loading");
  });

  it("says the workspace needs an update when the page never says it loaded, and opens Updates", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const onOpenUpdates = vi.fn();
    const mounted = await mountGroup(onOpenUpdates);

    vi.advanceTimersByTime(KEYBOARD_SHORTCUTS_READY_TIMEOUT_MS);
    m.redraw.sync();
    const notice = mounted.querySelector(
      "#keyboard-shortcuts-unavailable",
    ) as HTMLElement;
    (notice.querySelector("button") as HTMLButtonElement).click();

    expect(pageState(mounted)).toBe("unavailable");
    expect(frame(mounted).classList.contains("hidden")).toBe(true);
    expect(onOpenUpdates).toHaveBeenCalledTimes(1);
  });

  it("says so at once when the contract cannot load", async () => {
    const mounted = await mountGroup(undefined, () =>
      Promise.reject(new Error("no contract")),
    );
    m.redraw.sync();

    expect(pageState(mounted)).toBe("unavailable");
  });
});

// The account-settings route as the group's saves see it: the account's saved Imbue Studio chords, a PUT merging
// one action at a time (null drops it), and a switch to fail the PUTs.
class FakeSettingsRoute {
  saved: Record<string, string[]> = {};
  isFailingPuts = false;
  readonly puts: unknown[] = [];
  private readonly heldPuts: (() => void)[] = [];
  isHoldingPuts = false;

  readonly fetchImpl = async (url: string, init?: RequestInit): Promise<Response> => {
    expect(url).toBe("/ui/api/settings/local");
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as {
        keybindings: Record<string, Record<string, string[] | null>>;
      };
      this.puts.push(body);
      if (this.isHoldingPuts) await new Promise<void>((resolve) => this.heldPuts.push(resolve));
      if (this.isFailingPuts) return new Response("{}", { status: 500 });
      for (const [id, chords] of Object.entries(body.keybindings["imbue-studio"] ?? {})) {
        if (chords === null) delete this.saved[id];
        else this.saved[id] = chords;
      }
    }
    return new Response(JSON.stringify({ keybindings: { "imbue-studio": this.saved } }), { status: 200 });
  };

  releaseOnePut(): void {
    this.heldPuts.shift()?.();
  }
}

function postSave(mounted: HTMLElement, id: string, chords: string[]): void {
  window.dispatchEvent(
    new MessageEvent("message", {
      source: frame(mounted).contentWindow,
      origin: WORKSPACE_ORIGIN,
      data: { type: SAVE_CHROME_SHORTCUT, id, chords },
    }),
  );
}

function chromeShortcutsSent(): { actions: { id: string; chords: string[] }[]; error?: string }[] {
  return sentToPage
    .filter((sent) => sent.type === CHROME_SHORTCUTS)
    .map((sent) => sent.payload as { actions: { id: string; chords: string[] }[]; error?: string });
}

function chordsOf(payload: { actions: { id: string; chords: string[] }[] }): Record<string, string[]> {
  return Object.fromEntries(payload.actions.map((action) => [action.id, action.chords]));
}

describe("KeyboardShortcutsGroup and Imbue Studio's own shortcuts", () => {
  function inTheDesktopApp(sentToMain: Record<string, readonly string[]>[]): void {
    window.mindsNative = {
      platform: "darwin",
      openPopoutWindow: () => undefined,
      setShellShortcuts: () => undefined,
      setShortcutRecording: () => undefined,
      onRecordChord: () => undefined,
      setAppShortcuts: (_accountId: string, chordsByAction: Record<string, readonly string[]>) =>
        void sentToMain.push(chordsByAction),
    } as unknown as NonNullable<Window["mindsNative"]>;
  }

  async function mountReady(route: FakeSettingsRoute): Promise<HTMLElement> {
    const mounted = await mountGroup(undefined, undefined, new AppShortcutsModel({ fetchImpl: route.fetchImpl }));
    postFrom(frame(mounted).contentWindow, WORKSPACE_ORIGIN);
    await flushPromises();
    return mounted;
  }

  it("sends the page the account's shortcuts after what the chrome can do", async () => {
    inTheDesktopApp([]);
    const route = new FakeSettingsRoute();
    route.saved = { quit: [] };
    await mountReady(route);

    expect(sentToPage[0]?.type).toBe(CAPABILITIES);
    const sent = chromeShortcutsSent();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.actions[0]).toEqual({
      id: "new-window",
      label: "New window",
      defaultChords: ["meta+n"],
      chords: ["meta+n"],
    });
    expect(chordsOf(sent[0]!).quit).toEqual([]);
  });

  it("saves a shortcut to the account, tells main, and sends the shortcuts again", async () => {
    const sentToMain: Record<string, readonly string[]>[] = [];
    inTheDesktopApp(sentToMain);
    const route = new FakeSettingsRoute();
    const mounted = await mountReady(route);

    postSave(mounted, "new-window", ["shift+meta+n"]);
    await flushPromises();
    postSave(mounted, "new-window", ["meta+n"]);
    await flushPromises();

    expect(route.puts).toEqual([
      { keybindings: { "imbue-studio": { "new-window": ["shift+meta+n"] } } },
      { keybindings: { "imbue-studio": { "new-window": null } } },
    ]);
    const sent = chromeShortcutsSent();
    expect(sent.map((payload) => chordsOf(payload)["new-window"])).toEqual([
      ["meta+n"],
      ["shift+meta+n"],
      ["meta+n"],
    ]);
    expect(sentToMain.map((chords) => chords["new-window"])).toEqual([["shift+meta+n"], ["meta+n"]]);
  });

  it("runs the saves one at a time, in the order the page sent them", async () => {
    inTheDesktopApp([]);
    const route = new FakeSettingsRoute();
    const mounted = await mountReady(route);
    route.isHoldingPuts = true;

    postSave(mounted, "quit", []);
    postSave(mounted, "zoom-in", ["meta+plus"]);
    await flushPromises();
    const putsWhileTheFirstIsOut = route.puts.length;
    route.releaseOnePut();
    await flushPromises();
    route.releaseOnePut();
    await flushPromises();

    expect(putsWhileTheFirstIsOut).toBe(1);
    expect(route.puts).toEqual([
      { keybindings: { "imbue-studio": { quit: [] } } },
      { keybindings: { "imbue-studio": { "zoom-in": ["meta+plus"] } } },
    ]);
    const last = chromeShortcutsSent().at(-1)!;
    expect(chordsOf(last).quit).toEqual([]);
    expect(chordsOf(last)["zoom-in"]).toEqual(["meta+plus"]);
  });

  it("says so when a save fails, sending the shortcuts as they were", async () => {
    const sentToMain: Record<string, readonly string[]>[] = [];
    inTheDesktopApp(sentToMain);
    const route = new FakeSettingsRoute();
    const mounted = await mountReady(route);
    route.isFailingPuts = true;

    postSave(mounted, "quit", ["shift+meta+q"]);
    await flushPromises();

    const last = chromeShortcutsSent().at(-1)!;
    expect(last.error).toBe(CHROME_SHORTCUT_SAVE_FAILED_MESSAGE);
    expect(chordsOf(last).quit).toEqual(["meta+q"]);
    expect(sentToMain).toEqual([]);
  });

  it("answers a save of an action it does not have with why, saving nothing", async () => {
    inTheDesktopApp([]);
    const route = new FakeSettingsRoute();
    const mounted = await mountReady(route);

    postSave(mounted, "toggle-devtools", ["meta+i"]);
    await flushPromises();

    expect(route.puts).toEqual([]);
    expect(chromeShortcutsSent()).toHaveLength(2);
    expect(chromeShortcutsSent()[1]?.error).toBe(CHROME_SHORTCUT_UNKNOWN_MESSAGE);
  });

  it("sends no Imbue Studio shortcuts in a browser", async () => {
    const route = new FakeSettingsRoute();
    await mountReady(route);

    expect(chromeShortcutsSent()).toEqual([]);
  });
});
