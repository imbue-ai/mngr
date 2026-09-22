import { afterEach, describe, expect, it, vi } from "vitest";
import {
  WindowFocusState,
  resolveWindowFocus,
  setRelayedWindowFocus,
} from "./window-focus";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WindowFocusState", () => {
  it("answers from the main-process relay once it has spoken, over the document's own reading", () => {
    // The iframe-focus scenario: keyboard focus sits inside the workspace
    // frame, the reader switches to another app, and the document still
    // reports itself focused (Chromium fired no blur on the top-level
    // window). Main saw the window blur, and its relay is what holds.
    const focus = new WindowFocusState();
    vi.stubGlobal("document", { hasFocus: () => true });
    expect(focus.resolve(undefined)).toBe(true);

    focus.record(false);
    expect(focus.resolve(undefined)).toBe(false);

    focus.record(true);
    expect(focus.resolve(undefined)).toBe(true);
  });

  it("falls back to the document, and reads as focused with no document at all", () => {
    const focus = new WindowFocusState();
    vi.stubGlobal("document", { hasFocus: () => false });
    expect(focus.resolve(undefined)).toBe(false);
    vi.unstubAllGlobals();
    vi.stubGlobal("document", undefined);
    expect(focus.resolve(undefined)).toBe(true);
  });

  it("lets an injected override win over everything", () => {
    const focus = new WindowFocusState();
    focus.record(true);
    expect(focus.resolve(() => false)).toBe(false);
  });
});

// Two instances behind the module's recorder and its resolver would leave
// every reader back on document.hasFocus(), silently restoring the stale
// answer the relay exists to replace.
it("answers every reader of the module from the one relay the window recorded", () => {
  vi.stubGlobal("document", { hasFocus: () => true });
  setRelayedWindowFocus(false);
  expect(resolveWindowFocus(undefined)).toBe(false);
});
