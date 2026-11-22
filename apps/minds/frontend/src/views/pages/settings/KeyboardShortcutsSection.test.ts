// Settings > Keyboard shortcuts: the section shows what the main process says
// the binding is, and asks it for every change.

import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KeyboardShortcuts, ShortcutCapture, ShortcutChord } from "../../../electron-bridge";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, byId, required, settle, withMindsNative } from "../../../testing";
import { shortcutErrorSentence } from "./KeyboardShortcutsSection";

const DEFAULT_CHORD: ShortcutChord = { code: "KeyK", key: "k", meta: false, control: true, alt: false, shift: true };
const OTHER_CHORD: ShortcutChord = { code: "KeyJ", key: "j", meta: false, control: true, alt: true, shift: false };

const ON_DEFAULT: KeyboardShortcuts = {
  workspaceSwitcher: { chord: DEFAULT_CHORD, label: "Ctrl+Shift+K", defaultLabel: "Ctrl+Shift+K", isDefault: true },
};
const REBOUND: KeyboardShortcuts = {
  workspaceSwitcher: { chord: OTHER_CHORD, label: "Ctrl+Alt+J", defaultLabel: "Ctrl+Shift+K", isDefault: false },
};
const TURNED_OFF: KeyboardShortcuts = {
  workspaceSwitcher: { chord: null, label: null, defaultLabel: "Ctrl+Shift+K", isDefault: false },
};

type SetValue = ShortcutChord | null | "default";

interface Native {
  surface: Record<string, unknown>;
  /** Every value the section asked main to store, in order. */
  stored: SetValue[];
  /** How many captures the section abandoned. */
  cancelCount: () => number;
  /** Settle the capture in flight, as a key press (or Escape) in the window would. */
  finishCapture: (capture: ShortcutCapture) => void;
  /** Tell the page the binding changed, as another window's change does: every listener it registered hears it. */
  announce: (shortcuts: KeyboardShortcuts) => void;
  /** How many change listeners the page has registered, each of which the preload keeps for the page's life. */
  changeListenerCount: () => number;
}

/** A main process holding `initial`, whose set answers `onSet` (the stored binding by default). */
function native(
  initial: KeyboardShortcuts,
  onSet: (value: SetValue) => Promise<KeyboardShortcuts> = () => Promise.resolve(initial),
): Native {
  const stored: SetValue[] = [];
  let cancelCount = 0;
  let finishCapture: (capture: ShortcutCapture) => void = () => {
    throw new Error("no capture is in flight");
  };
  const changeListeners: ((shortcuts: KeyboardShortcuts) => void)[] = [];
  return {
    stored,
    cancelCount: () => cancelCount,
    finishCapture: (capture) => finishCapture(capture),
    announce: (shortcuts) => changeListeners.forEach((listener) => listener(shortcuts)),
    changeListenerCount: () => changeListeners.length,
    surface: {
      getKeyboardShortcuts: () => Promise.resolve(initial),
      setWorkspaceSwitcherShortcut: (value: SetValue) => {
        stored.push(value);
        return onSet(value);
      },
      captureShortcutChord: () =>
        new Promise<ShortcutCapture>((resolve) => {
          finishCapture = resolve;
        }),
      cancelShortcutCapture: () => {
        cancelCount += 1;
        return Promise.resolve();
      },
      onKeyboardShortcutsChanged: (callback: (shortcuts: KeyboardShortcuts) => void) => {
        changeListeners.push(callback);
      },
    },
  };
}

interface Section {
  draw: () => AnyVnode;
  remove: () => void;
}

// The module registers its change forwarder once for the page's life, so each test loads a page's worth of it.
let sectionModule: { KeyboardShortcutsSection: () => m.Component };

/** The section once it has read the binding. */
async function openSection(): Promise<Section> {
  const instance = sectionModule.KeyboardShortcutsSection() as unknown as {
    oninit: () => void;
    onremove: () => void;
    view: () => AnyVnode;
  };
  instance.oninit();
  await settle();
  return { draw: () => instance.view(), remove: () => instance.onremove() };
}

function label(root: AnyVnode): string {
  return allText(required(byId(root, "switcher-shortcut-label")));
}

function press(root: AnyVnode, id: string): void {
  (attrsOf(required(byId(root, id))).onclick as () => void)();
}

function buttonText(root: AnyVnode, id: string): string | null {
  const button = byId(root, id);
  return button === undefined ? null : allText(button);
}

beforeEach(async () => {
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.resetModules();
  sectionModule = await import("./KeyboardShortcutsSection");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("shortcutErrorSentence", () => {
  it("drops Electron's wrapper around a rejected invoke, keeping main's own sentence", () => {
    expect(
      shortcutErrorSentence(
        new Error(
          "Error invoking remote method 'set-workspace-switcher-shortcut': Error: Ctrl+C is reserved for copying.",
        ),
      ),
    ).toBe("Ctrl+C is reserved for copying.");
    // A rejection that was not an Error carries no inner "Error: ".
    expect(
      shortcutErrorSentence(new Error("Error invoking remote method 'set-workspace-switcher-shortcut': Not allowed.")),
    ).toBe("Not allowed.");
  });

  it("leaves a sentence with no wrapper as it is", () => {
    expect(shortcutErrorSentence(new Error("A shortcut needs a modifier key."))).toBe(
      "A shortcut needs a modifier key.",
    );
    expect(shortcutErrorSentence("plain words")).toBe("plain words");
  });
});

describe("the Keyboard shortcuts section", () => {
  it("names the one shortcut and shows no control until main has said what it is bound to", async () => {
    const neverAnswers = {
      getKeyboardShortcuts: () => new Promise<KeyboardShortcuts>(() => undefined),
      onKeyboardShortcutsChanged: () => undefined,
    };
    await withMindsNative(neverAnswers, async () => {
      const root = (await openSection()).draw();

      expect(allText(root)).toContain("Open the workspace switcher");
      expect(byId(root, "switcher-shortcut-label")).toBeUndefined();
      expect(byId(root, "switcher-shortcut-change")).toBeUndefined();
    });
  });

  it("shows the default binding's label with Change and Turn off, and nothing to reset", async () => {
    await withMindsNative(native(ON_DEFAULT).surface, async () => {
      const root = (await openSection()).draw();

      expect(label(root)).toBe("Ctrl+Shift+K");
      expect(buttonText(root, "switcher-shortcut-change")).toBe("Change");
      expect(buttonText(root, "switcher-shortcut-off")).toBe("Turn off");
      expect(byId(root, "switcher-shortcut-reset")).toBeUndefined();
      expect(allText(root)).not.toContain("The default is");
    });
  });

  it("offers Reset to default, and says what the default is, while the binding is another chord", async () => {
    const main = native(REBOUND, () => Promise.resolve(ON_DEFAULT));
    await withMindsNative(main.surface, async () => {
      const section = await openSection();
      const root = section.draw();

      expect(label(root)).toBe("Ctrl+Alt+J");
      expect(buttonText(root, "switcher-shortcut-reset")).toBe("Reset to default");
      expect(allText(root)).toContain("The default is Ctrl+Shift+K.");

      press(root, "switcher-shortcut-reset");
      await settle();

      expect(main.stored).toEqual(["default"]);
      expect(label(section.draw())).toBe("Ctrl+Shift+K");
      expect(byId(section.draw(), "switcher-shortcut-reset")).toBeUndefined();
    });
  });

  it("turns the shortcut off, then reads Off and offers to turn it back on", async () => {
    const main = native(ON_DEFAULT, (value) => Promise.resolve(value === null ? TURNED_OFF : ON_DEFAULT));
    await withMindsNative(main.surface, async () => {
      const section = await openSection();

      press(section.draw(), "switcher-shortcut-off");
      await settle();

      expect(main.stored).toEqual([null]);
      const off = section.draw();
      expect(label(off)).toBe("Off");
      expect(byId(off, "switcher-shortcut-off")).toBeUndefined();
      expect(buttonText(off, "switcher-shortcut-reset")).toBe("Turn on");
      expect(buttonText(off, "switcher-shortcut-change")).toBe("Change");

      press(off, "switcher-shortcut-reset");
      await settle();

      expect(main.stored).toEqual([null, "default"]);
      expect(label(section.draw())).toBe("Ctrl+Shift+K");
      expect(buttonText(section.draw(), "switcher-shortcut-off")).toBe("Turn off");
    });
  });

  it("records a chord on Change and stores what was pressed at once", async () => {
    const main = native(ON_DEFAULT, () => Promise.resolve(REBOUND));
    await withMindsNative(main.surface, async () => {
      const section = await openSection();

      press(section.draw(), "switcher-shortcut-change");
      const recording = section.draw();
      expect(label(recording)).toBe("Press a shortcut…");
      expect(buttonText(recording, "switcher-shortcut-cancel")).toBe("Cancel");
      // Nothing else can be pressed while the next key press belongs to the capture.
      expect(byId(recording, "switcher-shortcut-change")).toBeUndefined();
      expect(byId(recording, "switcher-shortcut-off")).toBeUndefined();
      expect(main.stored).toEqual([]);

      main.finishCapture({ chord: OTHER_CHORD });
      await settle();

      expect(main.stored).toEqual([OTHER_CHORD]);
      const rebound = section.draw();
      expect(label(rebound)).toBe("Ctrl+Alt+J");
      expect(byId(rebound, "switcher-shortcut-cancel")).toBeUndefined();
      expect(buttonText(rebound, "switcher-shortcut-reset")).toBe("Reset to default");
    });
  });

  it("changes nothing when the capture is cancelled", async () => {
    const main = native(ON_DEFAULT);
    await withMindsNative(main.surface, async () => {
      const section = await openSection();

      press(section.draw(), "switcher-shortcut-change");
      press(section.draw(), "switcher-shortcut-cancel");
      expect(main.cancelCount()).toBe(1);
      // Main answers the abandoned capture as it answers Escape.
      main.finishCapture({ cancelled: true });
      await settle();

      expect(main.stored).toEqual([]);
      const root = section.draw();
      expect(label(root)).toBe("Ctrl+Shift+K");
      expect(buttonText(root, "switcher-shortcut-change")).toBe("Change");
      expect(byId(root, "switcher-shortcut-error")).toBeUndefined();
    });
  });

  it("says why main refused a chord, in main's words, and keeps the binding it had", async () => {
    const main = native(ON_DEFAULT, () =>
      Promise.reject(
        new Error(
          "Error invoking remote method 'set-workspace-switcher-shortcut': Error: Ctrl+C is reserved for copying.",
        ),
      ),
    );
    await withMindsNative(main.surface, async () => {
      const section = await openSection();

      press(section.draw(), "switcher-shortcut-change");
      main.finishCapture({ chord: OTHER_CHORD });
      await settle();

      const refused = section.draw();
      const error = required(byId(refused, "switcher-shortcut-error"));
      expect(allText(error)).toBe("Ctrl+C is reserved for copying.");
      expect(attrsOf(error).role).toBe("alert");
      expect(label(refused)).toBe("Ctrl+Shift+K");

      // The next attempt starts clean.
      press(refused, "switcher-shortcut-change");
      expect(byId(section.draw(), "switcher-shortcut-error")).toBeUndefined();
    });
  });

  it("follows a change made in another window", async () => {
    const main = native(ON_DEFAULT);
    await withMindsNative(main.surface, async () => {
      const section = await openSection();

      main.announce(REBOUND);

      expect(label(section.draw())).toBe("Ctrl+Alt+J");
    });
  });

  it("registers one change listener for the page, however often the section is opened", async () => {
    // The preload never lets a listener go, so one per open would pile up for as long as the window lives.
    const main = native(ON_DEFAULT);
    await withMindsNative(main.surface, async () => {
      const first = await openSection();
      first.remove();
      const second = await openSection();
      second.remove();
      const third = await openSection();

      expect(main.changeListenerCount()).toBe(1);
      main.announce(REBOUND);
      expect(label(third.draw())).toBe("Ctrl+Alt+J");
    });
  });

  it("passes a change on only to the section on screen", async () => {
    const main = native(ON_DEFAULT);
    await withMindsNative(main.surface, async () => {
      const closed = await openSection();
      closed.remove();

      // Nothing is on screen to hear it.
      main.announce(REBOUND);
      expect(label(closed.draw())).toBe("Ctrl+Shift+K");

      const open = await openSection();
      main.announce(TURNED_OFF);

      expect(label(open.draw())).toBe("Off");
      expect(label(closed.draw())).toBe("Ctrl+Shift+K");
    });
  });

  it("ends a capture still running when the section is closed, and stores nothing from it", async () => {
    const main = native(ON_DEFAULT);
    await withMindsNative(main.surface, async () => {
      const section = await openSection();
      press(section.draw(), "switcher-shortcut-change");

      section.remove();
      expect(main.cancelCount()).toBe(1);
      main.finishCapture({ chord: OTHER_CHORD });
      await settle();

      expect(main.stored).toEqual([]);
    });
  });
});
