// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  CURSOR_HIDDEN_CLASS,
  installCursorHidingWhileTyping,
  isTextEditingKeystroke,
  isTextEditingTarget,
} from "./hide-cursor-while-typing";

function keystroke(
  key: string,
  modifiers: Partial<
    Pick<KeyboardEvent, "metaKey" | "ctrlKey" | "altKey" | "shiftKey">
  > = {},
): KeyboardEvent {
  return new KeyboardEvent("keydown", { key, bubbles: true, ...modifiers });
}

function pointerMove(screenX: number, screenY: number): MouseEvent {
  return new MouseEvent("mousemove", { screenX, screenY, bubbles: true });
}

describe("isTextEditingKeystroke", () => {
  it("counts printable characters and the editing keys, shift allowed", () => {
    expect(isTextEditingKeystroke(keystroke("a"))).toBe(true);
    expect(isTextEditingKeystroke(keystroke("A", { shiftKey: true }))).toBe(
      true,
    );
    expect(isTextEditingKeystroke(keystroke(" "))).toBe(true);
    expect(isTextEditingKeystroke(keystroke("Backspace"))).toBe(true);
    expect(isTextEditingKeystroke(keystroke("Delete"))).toBe(true);
    expect(isTextEditingKeystroke(keystroke("Enter"))).toBe(true);
    expect(isTextEditingKeystroke(keystroke("Process"))).toBe(true);
  });

  it("leaves shortcuts, bare modifiers, and navigation keys alone", () => {
    expect(isTextEditingKeystroke(keystroke("c", { metaKey: true }))).toBe(
      false,
    );
    expect(isTextEditingKeystroke(keystroke("v", { ctrlKey: true }))).toBe(
      false,
    );
    expect(isTextEditingKeystroke(keystroke("a", { altKey: true }))).toBe(
      false,
    );
    expect(isTextEditingKeystroke(keystroke("Shift"))).toBe(false);
    expect(isTextEditingKeystroke(keystroke("ArrowLeft"))).toBe(false);
    expect(isTextEditingKeystroke(keystroke("Escape"))).toBe(false);
    expect(isTextEditingKeystroke(keystroke("Tab"))).toBe(false);
  });
});

describe("isTextEditingTarget", () => {
  it("accepts writable text inputs and textareas and refuses toggles, read-only, disabled, and plain elements", () => {
    const input = document.createElement("input");
    const textarea = document.createElement("textarea");
    expect(isTextEditingTarget(input)).toBe(true);
    expect(isTextEditingTarget(textarea)).toBe(true);
    input.type = "password";
    expect(isTextEditingTarget(input)).toBe(true);

    input.type = "checkbox";
    expect(isTextEditingTarget(input)).toBe(false);
    input.type = "text";

    input.readOnly = true;
    expect(isTextEditingTarget(input)).toBe(false);
    textarea.disabled = true;
    expect(isTextEditingTarget(textarea)).toBe(false);

    expect(isTextEditingTarget(document.createElement("button"))).toBe(false);
    expect(isTextEditingTarget(document.body)).toBe(false);
    expect(isTextEditingTarget(null)).toBe(false);
  });
});

describe("installCursorHidingWhileTyping", () => {
  let uninstall: () => void = () => {};
  let input: HTMLInputElement;

  beforeEach(() => {
    input = document.createElement("input");
    document.body.appendChild(input);
    uninstall = installCursorHidingWhileTyping(document);
  });

  afterEach(() => {
    uninstall();
    input.remove();
    document.documentElement.classList.remove(CURSOR_HIDDEN_CLASS);
  });

  const isHidden = (): boolean =>
    document.documentElement.classList.contains(CURSOR_HIDDEN_CLASS);

  it("hides the pointer on a keystroke into a focused field and shows it on real movement", () => {
    input.focus();
    document.dispatchEvent(pointerMove(10, 10));
    input.dispatchEvent(keystroke("h"));
    expect(isHidden()).toBe(true);

    // The phantom mousemove Chromium fires under a stationary pointer when
    // the content beneath it reflows must not bring the pointer back.
    document.dispatchEvent(pointerMove(10, 10));
    expect(isHidden()).toBe(true);

    document.dispatchEvent(pointerMove(11, 10));
    expect(isHidden()).toBe(false);
  });

  it("leaves the pointer alone when focus is not in a field, or the key is a shortcut", () => {
    input.blur();
    document.body.dispatchEvent(keystroke("h"));
    expect(isHidden()).toBe(false);

    input.focus();
    input.dispatchEvent(keystroke("c", { metaKey: true }));
    expect(isHidden()).toBe(false);
  });

  it("shows the pointer again on a press or a scroll", () => {
    input.focus();
    input.dispatchEvent(keystroke("h"));
    expect(isHidden()).toBe(true);
    document.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(isHidden()).toBe(false);

    input.dispatchEvent(keystroke("i"));
    expect(isHidden()).toBe(true);
    document.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
    expect(isHidden()).toBe(false);
  });

  it("stops listening once uninstalled", () => {
    uninstall();
    input.focus();
    input.dispatchEvent(keystroke("h"));
    expect(isHidden()).toBe(false);
  });
});
