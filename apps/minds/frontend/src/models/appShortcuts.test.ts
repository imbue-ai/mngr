import { createRequire } from "node:module";
import { describe, expect, it, vi } from "vitest";
import {
  APP_SHORTCUTS,
  AppShortcutsModel,
  appShortcutChordsByAction,
  appShortcutWriteValue,
  chordLabelOffMac,
  chordsForPlatform,
  resolveAppShortcuts,
} from "./appShortcuts";

const electronAppShortcuts = createRequire(import.meta.url)(
  "../../../electron/app-shortcuts.js",
) as { APP_SHORTCUT_DEFAULTS: Record<string, readonly string[]> };

function chordsById(actions: readonly { id: string; chords: readonly string[] }[]): Record<string, readonly string[]> {
  return Object.fromEntries(actions.map((action) => [action.id, action.chords]));
}

describe("the Imbue Studio shortcut catalog", () => {
  it("declares the defaults main falls back on", () => {
    const defaults = Object.fromEntries(APP_SHORTCUTS.map((action) => [action.id, action.defaultChords]));

    expect(defaults).toEqual(electronAppShortcuts.APP_SHORTCUT_DEFAULTS);
  });
});

describe("resolveAppShortcuts", () => {
  it("takes the account's saved chords, else the defaults", () => {
    const actions = resolveAppShortcuts({
      "imbue-studio": { quit: [], "zoom-in": ["meta+plus"] },
      files: { "select-all": ["meta+q"] },
    });

    expect(chordsById(actions)).toEqual({
      "new-window": ["meta+n"],
      "close-window": ["meta+w"],
      quit: [],
      "zoom-in": ["meta+plus"],
      "zoom-out": ["meta+-"],
      "zoom-reset": ["meta+0"],
    });
    expect(actions.map((action) => action.label)).toEqual([
      "New window",
      "Close window",
      "Quit",
      "Zoom in",
      "Zoom out",
      "Reset zoom",
    ]);
  });

  it("reads every action as its defaults when the account saved none", () => {
    expect(chordsById(resolveAppShortcuts({}))).toEqual(chordsById(resolveAppShortcuts(undefined)));
    expect(chordsById(resolveAppShortcuts({}))["quit"]).toEqual(["meta+q"]);
  });
});

describe("chordsForPlatform", () => {
  it("drops a Mac's Control chords off a Mac only", () => {
    const chords = ["ctrl+meta+n", "meta+n", "alt+meta+n"];

    expect(chordsForPlatform(chords, true)).toEqual(chords);
    expect(chordsForPlatform(chords, false)).toEqual(["meta+n", "alt+meta+n"]);
  });

  it("maps every action to what can arrive on the platform", () => {
    const actions = resolveAppShortcuts({ "imbue-studio": { quit: ["ctrl+q"] } });

    expect(appShortcutChordsByAction(actions, false).quit).toEqual([]);
    expect(appShortcutChordsByAction(actions, true).quit).toEqual(["ctrl+q"]);
    expect(Object.keys(appShortcutChordsByAction(actions, true))).toHaveLength(APP_SHORTCUTS.length);
  });
});

describe("appShortcutWriteValue", () => {
  it("stores nothing for the defaults and the chords otherwise, Off included", () => {
    expect(appShortcutWriteValue("new-window", ["meta+n"])).toBeNull();
    expect(appShortcutWriteValue("new-window", ["shift+meta+n"])).toEqual(["shift+meta+n"]);
    expect(appShortcutWriteValue("new-window", [])).toEqual([]);
    expect(appShortcutWriteValue("new-window", ["meta+n", "f9"])).toEqual(["meta+n", "f9"]);
  });
});

describe("chordLabelOffMac", () => {
  it("writes a chord the way Windows and Linux menus do", () => {
    expect(chordLabelOffMac("meta+n")).toBe("Ctrl+N");
    expect(chordLabelOffMac("shift+meta+w")).toBe("Ctrl+Shift+W");
    expect(chordLabelOffMac("alt+shift+meta+arrowup")).toBe("Ctrl+Alt+Shift+Up");
    expect(chordLabelOffMac("meta+=")).toBe("Ctrl+=");
    expect(chordLabelOffMac("meta+plus")).toBe("Ctrl++");
    expect(chordLabelOffMac("f11")).toBe("F11");
  });
});

class FakeSettingsReads {
  readonly reads: string[] = [];
  private readonly answers: ((response: Response) => void)[] = [];

  readonly fetchImpl = (url: string): Promise<Response> => {
    this.reads.push(url);
    return new Promise((resolve) => this.answers.push(resolve));
  };

  answer(index: number, saved: Record<string, string[]>, status = 200): void {
    this.answers[index]?.(new Response(JSON.stringify({ keybindings: { "imbue-studio": saved } }), { status }));
  }
}

async function flushPromises(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await Promise.resolve();
}

describe("AppShortcutsModel", () => {
  function modelWith(reads: FakeSettingsReads, sent: [string, Record<string, readonly string[]>][], isRunByMain = true) {
    return new AppShortcutsModel({
      fetchImpl: reads.fetchImpl,
      isRunByMain: () => isRunByMain,
      isMac: () => false,
      setAppShortcuts: (accountId, chordsByAction) => void sent.push([accountId, chordsByAction]),
    });
  }

  it("reads the window's account once and tells main its chords", async () => {
    const reads = new FakeSettingsReads();
    const sent: [string, Record<string, readonly string[]>][] = [];
    const model = modelWith(reads, sent);

    model.followAccount("user-1");
    model.followAccount("user-1");
    reads.answer(0, { quit: ["ctrl+q"], "new-window": ["shift+meta+n"] });
    await flushPromises();

    expect(reads.reads).toEqual(["/ui/api/settings/user-1"]);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.[0]).toBe("user-1");
    expect(sent[0]?.[1].quit).toEqual([]);
    expect(sent[0]?.[1]["new-window"]).toEqual(["shift+meta+n"]);
  });

  it("follows a change of account, dropping the answer for the account it left", async () => {
    const reads = new FakeSettingsReads();
    const sent: [string, Record<string, readonly string[]>][] = [];
    const model = modelWith(reads, sent);

    model.followAccount("user-1");
    model.followAccount("user-2");
    reads.answer(1, {});
    reads.answer(0, { quit: [] });
    await flushPromises();

    expect(sent.map(([accountId]) => accountId)).toEqual(["user-2"]);
  });

  it("answers the defaults for the menus, not the account it left, until the new account's chords are read", async () => {
    const reads = new FakeSettingsReads();
    const model = modelWith(reads, []);

    model.followAccount("user-1");
    reads.answer(0, { "new-window": ["shift+meta+n"] });
    await flushPromises();
    expect(model.chordsFor("new-window")).toEqual(["shift+meta+n"]);

    model.followAccount("user-2");
    expect(model.chordsFor("new-window")).toEqual(["meta+n"]);
    reads.answer(1, { "new-window": ["alt+meta+n"] });
    await flushPromises();
    expect(model.chordsFor("new-window")).toEqual(["alt+meta+n"]);
  });

  it("leaves main on its defaults when the read fails", async () => {
    const reads = new FakeSettingsReads();
    const sent: [string, Record<string, readonly string[]>][] = [];
    const model = modelWith(reads, sent);

    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    model.followAccount("user-1");
    reads.answer(0, {}, 500);
    await flushPromises();

    expect(sent).toEqual([]);
    expect(warn).toHaveBeenCalledWith("Could not read Imbue Studio's shortcuts", expect.any(Error));
    warn.mockRestore();
  });

  it("reads nothing where main runs no shortcuts", () => {
    const reads = new FakeSettingsReads();
    const model = modelWith(reads, [], false);

    model.followAccount("user-1");

    expect(reads.reads).toEqual([]);
  });
});
