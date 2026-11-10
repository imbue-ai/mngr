// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  getAppContext,
  registerAppContext,
} from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import { ShellState } from "../../shell/shell-state";
import {
  KEYBOARD_SHORTCUTS_READY_TIMEOUT_MS,
  KeyboardShortcutsGroup,
  type KeyboardShortcutsContract,
} from "./KeyboardShortcutsGroup";

const AGENT_ID = "agent-" + "c".repeat(8);
const WORKSPACE_ORIGIN = `http://${AGENT_ID}.localhost:8421`;
const PAGE_REPORT = "minds:keyboard-shortcuts-page";
const CAPABILITIES = "minds:embedder-capabilities";
const RECORD_CHORD = "minds:record-chord";

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
): Promise<HTMLElement> {
  const shell = new ShellState(createEmptyStores());
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
