import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InertEventSource, jsonResponse } from "../testing";
import type { CreateOperationStatus } from "./create";
import {
  CreateAttemptWatcher,
  MindLivenessTracker,
  READY_WAIT_STATUS_POLL_INTERVAL_MS,
  STATUS_POLL_TIMEOUT_MS,
  STATUS_POLL_INTERVAL_MS,
  hostNameFormatError,
  statusPollIntervalMs,
  submitCreateRequest,
} from "./create";
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

describe("MindLivenessTracker", () => {
  afterEach(() => {
    // restoreAllMocks does NOT undo vi.stubGlobal; only this does.
    vi.unstubAllGlobals();
  });

  it("shows the optimistic transient until the authoritative state reaches the target", async () => {
    const tracker = new MindLivenessTracker(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 200 })),
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
      vi.fn(async () => new Response(null, { status: 500 })),
    );
    await tracker.stop("agent-2");
    expect(tracker.displayedLiveness("agent-2", "RUNNING")).toBe("RUNNING");
  });

  it("keeps the refusal's body for the failure it reports", async () => {
    const tracker = new MindLivenessTracker(() => undefined);
    const body = { error: "Could not start the workspace host: This workspace is undergoing maintenance and will be back shortly." };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(body, 502)),
    );
    const result = await tracker.start("agent-4");
    expect(result).toEqual({ isOk: false, outcome: { status: 502, body } });
  });
});

describe("statusPollIntervalMs", () => {
  it("polls briskly only while the new workspace boots", () => {
    expect(statusPollIntervalMs("WAITING_FOR_READY")).toBe(READY_WAIT_STATUS_POLL_INTERVAL_MS);
    expect(READY_WAIT_STATUS_POLL_INTERVAL_MS).toBeLessThan(STATUS_POLL_INTERVAL_MS);
    for (const status of ["INITIALIZING", "CLONING_REPO", "CREATING_WORKSPACE", "DONE", undefined]) {
      expect(statusPollIntervalMs(status)).toBe(STATUS_POLL_INTERVAL_MS);
    }
  });
});

describe("CreateAttemptWatcher", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** A started watcher whose status reads go to `fetchStub`. */
  function startWatcher(fetchStub: (url: string, init: RequestInit) => Promise<Response>): {
    watcher: CreateAttemptWatcher;
    doneRedirects: string[];
    failures: string[];
  } {
    vi.useFakeTimers();
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.stubGlobal("EventSource", InertEventSource);
    vi.stubGlobal("fetch", fetchStub);
    const doneRedirects: string[] = [];
    const failures: string[] = [];
    const watcher = new CreateAttemptWatcher("create-attempt-0123abcd", {
      onDone: (redirectUrl) => doneRedirects.push(redirectUrl),
      onFailed: (error) => failures.push(error),
      onStageText: () => undefined,
      onLogLines: () => undefined,
      onProgress: () => undefined,
    });
    watcher.start();
    return { watcher, doneRedirects, failures };
  }

  /** A started watcher whose status polls answer `answer.current` at the time each poll is made
   * (once it settles, when it is a promise). */
  function startWatching(answer: { current: CreateOperationStatus | Promise<CreateOperationStatus> }): {
    watcher: CreateAttemptWatcher;
    statusPollCount: () => number;
    doneRedirects: string[];
    failures: string[];
  } {
    let polls = 0;
    const started = startWatcher(() => {
      polls += 1;
      return Promise.resolve(answer.current).then(jsonResponse);
    });
    return { ...started, statusPollCount: () => polls };
  }

  it("drops a status read that never answers, so the next poll can still see DONE", async () => {
    let polls = 0;
    const { watcher, doneRedirects } = startWatcher((_url, init) => {
      polls += 1;
      if (polls === 1) {
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        });
      }
      return Promise.resolve(jsonResponse({ status: "DONE", redirect_url: "/goto/agent-0123abcd/" }));
    });

    await vi.advanceTimersByTimeAsync(STATUS_POLL_TIMEOUT_MS - 1);
    expect(polls).toBe(1);
    await vi.advanceTimersByTimeAsync(STATUS_POLL_INTERVAL_MS + 1);
    expect(polls).toBe(2);
    expect(doneRedirects).toEqual(["/goto/agent-0123abcd/"]);
    watcher.stop();
  });

  it("polls every two seconds while the workspace is built, every quarter second while it boots, and stops at DONE", async () => {
    const answer = { current: { status: "CREATING_WORKSPACE" } as CreateOperationStatus };
    const { watcher, statusPollCount, doneRedirects } = startWatching(answer);
    await vi.advanceTimersByTimeAsync(0);
    expect(statusPollCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(STATUS_POLL_INTERVAL_MS - 1);
    expect(statusPollCount()).toBe(1);

    // The poll that sees the boot start switches to the brisk cadence.
    answer.current = { status: "WAITING_FOR_READY" };
    await vi.advanceTimersByTimeAsync(1);
    expect(statusPollCount()).toBe(2);
    await vi.advanceTimersByTimeAsync(READY_WAIT_STATUS_POLL_INTERVAL_MS * 4);
    expect(statusPollCount()).toBe(6);

    // DONE is picked up within one brisk poll of being published, once, and polling ends.
    answer.current = { status: "DONE", redirect_url: "/goto/agent-0123abcd/" };
    await vi.advanceTimersByTimeAsync(READY_WAIT_STATUS_POLL_INTERVAL_MS);
    expect(doneRedirects).toEqual(["/goto/agent-0123abcd/"]);
    const pollsAtDone = statusPollCount();
    await vi.advanceTimersByTimeAsync(STATUS_POLL_INTERVAL_MS * 5);
    expect(statusPollCount()).toBe(pollsAtDone);
    expect(doneRedirects).toEqual(["/goto/agent-0123abcd/"]);
    watcher.stop();
  });

  it("stops polling at a failure", async () => {
    const answer = { current: { status: "WAITING_FOR_READY" } as CreateOperationStatus };
    const { watcher, statusPollCount, failures } = startWatching(answer);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(READY_WAIT_STATUS_POLL_INTERVAL_MS);
    expect(statusPollCount()).toBe(2);

    answer.current = { status: "FAILED", error: "the workspace never answered" };
    await vi.advanceTimersByTimeAsync(READY_WAIT_STATUS_POLL_INTERVAL_MS);
    expect(failures).toEqual(["the workspace never answered"]);
    const pollsAtFailure = statusPollCount();
    await vi.advanceTimersByTimeAsync(STATUS_POLL_INTERVAL_MS * 5);
    expect(statusPollCount()).toBe(pollsAtFailure);
    watcher.stop();
  });

  it("starts no poll while the last one has not answered", async () => {
    let answerSlowPoll: (status: CreateOperationStatus) => void = () => undefined;
    const answer: { current: CreateOperationStatus | Promise<CreateOperationStatus> } = {
      current: new Promise((resolve) => {
        answerSlowPoll = resolve;
      }),
    };
    const { watcher, statusPollCount } = startWatching(answer);
    await vi.advanceTimersByTimeAsync(STATUS_POLL_INTERVAL_MS * 3);
    expect(statusPollCount()).toBe(1);

    answer.current = { status: "WAITING_FOR_READY" };
    answerSlowPoll({ status: "WAITING_FOR_READY" });
    await vi.advanceTimersByTimeAsync(READY_WAIT_STATUS_POLL_INTERVAL_MS);
    expect(statusPollCount()).toBe(2);
    watcher.stop();
  });

  it("polls no more once stopped", async () => {
    const { watcher, statusPollCount } = startWatching({ current: { status: "WAITING_FOR_READY" } });
    await vi.advanceTimersByTimeAsync(0);
    watcher.stop();
    await vi.advanceTimersByTimeAsync(STATUS_POLL_INTERVAL_MS * 5);
    expect(statusPollCount()).toBe(1);
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
