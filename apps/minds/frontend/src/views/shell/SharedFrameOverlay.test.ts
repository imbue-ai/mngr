// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import { buttonLabelled, mountInBody } from "../../testing";
import { SHARED_OPEN_STALL_MS, SharedFrameOverlay, isSharedOpenStalled, sharedStallDelayMs } from "./SharedFrameOverlay";
import type { SharedFrameProgress } from "./shell-state";

const STARTED_AT_MS = 1_700_000_000_000;

function progress(overrides: Partial<SharedFrameProgress>): SharedFrameProgress {
  return {
    shareKey: "agent-" + "9".repeat(32),
    phase: "connecting",
    verdict: null,
    refusalMessage: null,
    startedAtMs: STARTED_AT_MS,
    isSignInRetried: false,
    ...overrides,
  };
}

function mountOverlay(current: SharedFrameProgress, nowMs: number): { root: HTMLElement; retries: () => number } {
  let retries = 0;
  const root = mountInBody(() =>
    m(SharedFrameOverlay, {
      progress: current,
      workspaceName: "Josh's workspace",
      nowMs,
      onRetry: () => {
        retries += 1;
      },
    }),
  );
  return { root, retries: () => retries };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("SharedFrameOverlay", () => {
  it("says it is connecting, with a spinner and no Retry, while a fresh open runs", () => {
    const { root } = mountOverlay(progress({}), STARTED_AT_MS + 1_000);
    const overlay = root.querySelector("#shared-frame-overlay");
    expect(overlay?.getAttribute("data-shared-open-phase")).toBe("connecting");
    expect(overlay?.textContent).toContain("Connecting to Josh's workspace…");
    expect(root.querySelector("#shared-frame-retry")).toBeNull();
  });

  it("admits a slow open and offers Retry once it has run long", () => {
    const now = STARTED_AT_MS + SHARED_OPEN_STALL_MS;
    expect(isSharedOpenStalled(progress({}), now - 1)).toBe(false);
    expect(isSharedOpenStalled(progress({}), now)).toBe(true);
    const { root, retries } = mountOverlay(progress({}), now);
    const overlay = root.querySelector("#shared-frame-overlay");
    expect(overlay?.getAttribute("data-shared-open-phase")).toBe("stalled");
    expect(overlay?.textContent).toContain("Still connecting");
    buttonLabelled(root, "Retry")?.click();
    expect(retries()).toBe(1);
  });

  it("times the stall redraw to the moment the open stalls, and asks for none once stalled or failed", () => {
    const connecting = progress({});
    expect(sharedStallDelayMs(connecting, STARTED_AT_MS + 1_000)).toBe(SHARED_OPEN_STALL_MS - 1_000);
    expect(sharedStallDelayMs(connecting, STARTED_AT_MS + SHARED_OPEN_STALL_MS - 1)).toBe(1);
    // A stalled open wants no further redraw: one would arm another timer at once, and redraw without end.
    expect(sharedStallDelayMs(connecting, STARTED_AT_MS + SHARED_OPEN_STALL_MS)).toBeNull();
    expect(sharedStallDelayMs(connecting, STARTED_AT_MS + SHARED_OPEN_STALL_MS + 5_000)).toBeNull();
    expect(sharedStallDelayMs(progress({ phase: "failed", verdict: "refused" }), STARTED_AT_MS + 1_000)).toBeNull();
  });

  it("names the failure in the row's words and offers Retry when the open failed", () => {
    const failed = progress({ phase: "failed", verdict: "sign_in_failed" });
    expect(isSharedOpenStalled(failed, STARTED_AT_MS + SHARED_OPEN_STALL_MS)).toBe(false);
    const { root, retries } = mountOverlay(failed, STARTED_AT_MS + 2_000);
    const overlay = root.querySelector("#shared-frame-overlay");
    expect(overlay?.getAttribute("data-shared-open-phase")).toBe("failed");
    expect(overlay?.textContent).toContain("Couldn't open Josh's workspace");
    expect(overlay?.textContent).toContain("Could not sign in to open it");
    buttonLabelled(root, "Retry")?.click();
    expect(retries()).toBe(1);
  });

  it("says the desktop's refusal in its own words when the open never reached the frame", () => {
    const refused = progress({ phase: "failed", verdict: null, refusalMessage: "No longer shared with you" });
    const { root } = mountOverlay(refused, STARTED_AT_MS + 2_000);
    const overlay = root.querySelector("#shared-frame-overlay");
    expect(overlay?.getAttribute("data-shared-open-phase")).toBe("failed");
    expect(overlay?.textContent).toContain("No longer shared with you");
    expect(overlay?.textContent).not.toContain("did not let this account in");
    expect(buttonLabelled(root, "Retry")).not.toBeNull();
  });
});
