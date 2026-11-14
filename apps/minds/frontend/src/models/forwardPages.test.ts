import { describe, expect, it } from "vitest";
import { FORWARD_PAGE_SPINNER_ONLY_MS, ForwardPageTracker, parseForwardPageMessage } from "./forwardPages";

const FRAME_ORIGIN = "http://agent-0123456789abcdef0123456789abcdef.localhost:8421";
const OTHER_WORKSPACE_ORIGIN = "http://agent-fedcba9876543210fedcba9876543210.localhost:8421";

function pageReport(reason: string): Record<string, unknown> {
  return { type: "mngr-forward:page", reason, status: 503, details: "" };
}

describe("ForwardPageTracker", () => {
  it("holds the page its frame reports, and ignores reports from anywhere else", () => {
    const tracker = new ForwardPageTracker();

    expect(tracker.receive({ data: pageReport("NOT_LISTENING"), origin: FRAME_ORIGIN, isFromFrame: false })).toBe(false);
    expect(tracker.receive({ data: pageReport("NOT_LISTENING"), origin: "https://example.com", isFromFrame: true })).toBe(
      false,
    );
    expect(tracker.report).toBeNull();

    expect(tracker.receive({ data: pageReport("NOT_LISTENING"), origin: FRAME_ORIGIN, isFromFrame: true })).toBe(true);
    expect(tracker.report?.reason).toBe("NOT_LISTENING");
  });

  it("lets the page go when it says it is leaving, though an unloading page's message has no source", () => {
    const tracker = new ForwardPageTracker();
    tracker.receive({ data: pageReport("NOT_LISTENING"), origin: FRAME_ORIGIN, isFromFrame: true });

    const closed = { type: "mngr-forward:page-closed" };
    expect(tracker.receive({ data: closed, origin: OTHER_WORKSPACE_ORIGIN, isFromFrame: false })).toBe(false);
    expect(tracker.report).not.toBeNull();

    expect(tracker.receive({ data: closed, origin: FRAME_ORIGIN, isFromFrame: false })).toBe(true);
    expect(tracker.report).toBeNull();
  });

  it("keeps counting the spinner-only seconds from the first report across a change of reason", () => {
    let nowMs = 1_000;
    const tracker = new ForwardPageTracker(() => nowMs);
    tracker.receive({ data: pageReport("NOT_LISTENING"), origin: FRAME_ORIGIN, isFromFrame: true });
    nowMs += 6_000;
    tracker.receive({ data: pageReport("UNREACHABLE"), origin: FRAME_ORIGIN, isFromFrame: true });

    expect(tracker.report?.reason).toBe("UNREACHABLE");
    expect(tracker.msUntilExplained()).toBe(FORWARD_PAGE_SPINNER_ONLY_MS - 6_000);
    nowMs += 5_000;
    expect(tracker.msUntilExplained()).toBe(0);
  });
});

describe("parseForwardPageMessage", () => {
  it("reads a page going away, and ignores every other message", () => {
    expect(parseForwardPageMessage({ type: "mngr-forward:page-closed" })).toEqual({ kind: "closed" });
    expect(parseForwardPageMessage({ type: "workspace-ready" })).toBeNull();
    expect(parseForwardPageMessage({ type: "mngr-forward:page", reason: "NOT_LISTENING" })).toBeNull();
    expect(parseForwardPageMessage({ type: "mngr-forward:page", reason: "SOME_FUTURE_REASON", status: 503 })).toBeNull();
    expect(parseForwardPageMessage("mngr-forward:page")).toBeNull();
  });
});
