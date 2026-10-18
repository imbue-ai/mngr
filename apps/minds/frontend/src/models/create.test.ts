import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../testing";
import { MindLivenessTracker, hostNameFormatError, progressForElapsed, submitCreateRequest } from "./create";
import { OnboardingProgress } from "./onboarding";

describe("hostNameFormatError", () => {
  it("accepts empty (auto-named) and plain names", () => {
    expect(hostNameFormatError("")).toBe("");
    expect(hostNameFormatError("my-machine_2")).toBe("");
  });

  it("names the first broken rule in plain language", () => {
    expect(hostNameFormatError("a.b")).toBe("Dots aren't allowed in a name.");
    expect(hostNameFormatError("a b")).toBe("Spaces aren't allowed in a name.");
    expect(hostNameFormatError("a$b")).toBe("Use only letters, numbers, dashes, and underscores.");
    expect(hostNameFormatError("-ab")).toBe("Can't start with a dash or underscore.");
    expect(hostNameFormatError("ab_")).toBe("Can't end with a dash or underscore.");
  });
});

describe("progressForElapsed", () => {
  it("eases to 80 percent over the expected duration then crawls asymptotically", () => {
    expect(progressForElapsed(0, 60)).toBe(0);
    expect(progressForElapsed(30, 60)).toBeCloseTo(40);
    expect(progressForElapsed(60, 60)).toBeCloseTo(80);
    const late = progressForElapsed(600, 60);
    expect(late).toBeGreaterThan(95);
    expect(late).toBeLessThan(100);
  });

  it("falls back to a sane default when the expected duration is zero", () => {
    expect(progressForElapsed(30, 0)).toBeCloseTo(40);
  });
});

describe("MindLivenessTracker", () => {
  afterEach(() => {
    // restoreAllMocks does NOT undo vi.stubGlobal; only this does.
    vi.unstubAllGlobals();
  });

  it("shows the optimistic transient until the authoritative state reaches the target", async () => {
    const tracker = new MindLivenessTracker(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true }) as Response),
    );
    const startPromise = tracker.start("agent-1");
    expect(tracker.displayedLiveness("agent-1", "STOPPED")).toBe("STARTING");
    await startPromise;
    // An interim payload still carrying the pre-action state keeps the transient.
    expect(tracker.displayedLiveness("agent-1", "STOPPED")).toBe("STARTING");
    // The authoritative target clears the pending guard.
    expect(tracker.displayedLiveness("agent-1", "RUNNING")).toBe("RUNNING");
    expect(tracker.displayedLiveness("agent-1", "STOPPED")).toBe("STOPPED");
  });

  it("passes backend-observed transitional states through when no action is pending", () => {
    const tracker = new MindLivenessTracker(() => undefined);
    expect(tracker.displayedLiveness("agent-3", "STOPPING")).toBe("STOPPING");
    expect(tracker.displayedLiveness("agent-3", "STARTING")).toBe("STARTING");
    // An unrecognized reading still reports honestly as unknown.
    expect(tracker.displayedLiveness("agent-3", "weird")).toBe("UNKNOWN");
    expect(tracker.displayedLiveness("agent-3", "")).toBe("UNKNOWN");
  });

  it("drops the transient when the action fails", async () => {
    const tracker = new MindLivenessTracker(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false }) as Response),
    );
    await tracker.stop("agent-2");
    expect(tracker.displayedLiveness("agent-2", "RUNNING")).toBe("RUNNING");
  });
});

describe("submitCreateRequest", () => {
  function progressSeededIncomplete(): OnboardingProgress {
    const progress = new OnboardingProgress();
    progress.seed(false);
    return progress;
  }

  it("posts the body to the create front door and completes onboarding locally on a 202", async () => {
    const progress = progressSeededIncomplete();
    const calls: Array<{ url: string; body: unknown }> = [];
    const result = await submitCreateRequest(
      { git_url: "https://example/repo" },
      (url, init) => {
        calls.push({ url, body: JSON.parse(String(init?.body)) });
        return Promise.resolve(jsonResponse({ operation_id: "create-attempt-1" }, 202));
      },
      progress,
    );
    expect(calls).toEqual([{ url: "/api/v1/workspaces", body: { git_url: "https://example/repo" } }]);
    expect(result).toEqual({ status: 202, data: { operation_id: "create-attempt-1" } });
    expect(progress.isComplete).toBe(true);
  });

  it("leaves onboarding incomplete when the front door refuses", async () => {
    const progress = progressSeededIncomplete();
    const result = await submitCreateRequest(
      {},
      () => Promise.resolve(jsonResponse({ error: "no capacity" }, 409)),
      progress,
    );
    expect(result.status).toBe(409);
    expect(progress.isComplete).toBe(false);
  });

  it("reports status 0 when the server cannot be reached", async () => {
    const progress = progressSeededIncomplete();
    const result = await submitCreateRequest({}, () => Promise.reject(new Error("offline")), progress);
    expect(result).toEqual({ status: 0, data: {} });
    expect(progress.isComplete).toBe(false);
  });
});
