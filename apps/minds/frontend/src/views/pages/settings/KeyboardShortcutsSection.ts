// Settings > Keyboard shortcuts: the one binding the desktop app lets you
// change, the chord that opens the workspace switcher. The main process holds
// the binding, records a new one, and decides what is allowed
// (electron/keyboard-shortcuts.js), since it is what matches the chord on every
// key press; this section shows what main says and asks it to change.

import m from "mithril";
import type { KeyboardShortcuts } from "../../../electron-bridge";
import { electronBridge } from "../../../electron-bridge";
import { Button } from "../../components/Button";

const RECORDING_PROMPT = "Press a shortcut…";
const OFF_LABEL = "Off";

/** The sentence a failed change shows: main's own words, without Electron's wrapper around a rejected invoke. */
export function shortcutErrorSentence(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/^Error invoking remote method '[^']+': (?:Error: )?/, "");
}

// The preload keeps every listener it is given for the page's life, so the section registers one forwarder, once,
// and points it at whichever section is mounted.
let activeChangeListener: ((shortcuts: KeyboardShortcuts) => void) | null = null;
let isChangeForwarderRegistered = false;

function listenForChanges(listener: ((shortcuts: KeyboardShortcuts) => void) | null): void {
  activeChangeListener = listener;
  if (isChangeForwarderRegistered || listener === null) return;
  isChangeForwarderRegistered = true;
  electronBridge.onKeyboardShortcutsChanged((shortcuts) => activeChangeListener?.(shortcuts));
}

export function KeyboardShortcutsSection(): m.Component {
  let shortcuts: KeyboardShortcuts | null = null;
  let isRecording = false;
  let errorMessage: string | null = null;
  let isRemoved = false;

  function apply(next: KeyboardShortcuts | null): void {
    if (next !== null) shortcuts = next;
    m.redraw();
  }

  async function change(value: Parameters<typeof electronBridge.setWorkspaceSwitcherShortcut>[0]): Promise<void> {
    errorMessage = null;
    try {
      apply(await electronBridge.setWorkspaceSwitcherShortcut(value));
    } catch (error) {
      errorMessage = shortcutErrorSentence(error);
      m.redraw();
    }
  }

  async function record(): Promise<void> {
    errorMessage = null;
    isRecording = true;
    m.redraw();
    const capture = await electronBridge.captureShortcutChord();
    isRecording = false;
    // Closing Settings mid-capture cancels it; nothing is left to draw.
    if (isRemoved) return;
    if (capture.chord === undefined) {
      m.redraw();
      return;
    }
    await change(capture.chord);
  }

  return {
    oninit() {
      void electronBridge.getKeyboardShortcuts().then(apply);
      // Another window may change the binding while this one shows it.
      listenForChanges(apply);
    },
    onremove() {
      isRemoved = true;
      listenForChanges(null);
      if (isRecording) electronBridge.cancelShortcutCapture();
    },
    view() {
      const binding = shortcuts?.workspaceSwitcher ?? null;
      return m("section#keyboard-shortcuts", [
        m("h2", { class: "type-heading-lg text-primary mb-2" }, "Keyboard shortcuts"),
        m(
          "p",
          { class: "type-body text-secondary mb-3" },
          "Applies to every window of the desktop app on this device.",
        ),
        m("div", { class: "flex items-center justify-between gap-3 py-3 border-b border-subtle" }, [
          m("span", [
            m("span", { class: "type-body text-primary font-semibold" }, "Open the workspace switcher"),
            m(
              "span",
              { class: "block type-helper text-tertiary" },
              "Works from anywhere in the app, including inside a workspace.",
            ),
          ]),
          binding === null
            ? null
            : m("span", { class: "flex shrink-0 items-center gap-2" }, [
                m(
                  "kbd#switcher-shortcut-label",
                  {
                    class:
                      "rounded border border-subtle bg-fill-subtle px-2 py-1 font-sans type-body " +
                      (isRecording || binding.label === null ? "text-tertiary" : "text-primary"),
                  },
                  isRecording ? RECORDING_PROMPT : (binding.label ?? OFF_LABEL),
                ),
                isRecording
                  ? m(
                      Button,
                      {
                        id: "switcher-shortcut-cancel",
                        variant: "secondary",
                        onclick: () => electronBridge.cancelShortcutCapture(),
                      },
                      "Cancel",
                    )
                  : [
                      m(
                        Button,
                        { id: "switcher-shortcut-change", variant: "secondary", onclick: () => void record() },
                        "Change",
                      ),
                      binding.isDefault
                        ? null
                        : m(
                            Button,
                            {
                              id: "switcher-shortcut-reset",
                              variant: "secondary",
                              onclick: () => void change("default"),
                            },
                            binding.chord === null ? "Turn on" : "Reset to default",
                          ),
                      binding.chord === null
                        ? null
                        : m(
                            Button,
                            { id: "switcher-shortcut-off", variant: "secondary", onclick: () => void change(null) },
                            "Turn off",
                          ),
                    ],
              ]),
        ]),
        errorMessage === null
          ? null
          : m("p#switcher-shortcut-error", { class: "mt-2 type-helper text-important", role: "alert" }, errorMessage),
        binding === null || binding.isDefault
          ? null
          : m("p", { class: "mt-2 type-helper text-tertiary" }, `The default is ${binding.defaultLabel}.`),
      ]);
    },
  };
}
