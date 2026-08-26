import { describe, expect, it, vi } from "vitest";
import { jsonResponse, memoryStorage } from "../testing";
import { HelpModel, setPendingHelpLaunch, takePendingHelpLaunch } from "./help";

describe("HelpModel", () => {
  it("consumes the staged launch exactly once and defaults the mode from it", () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    const first = new HelpModel({ storage: memoryStorage() });
    expect(first.mode).toBe("agent");
    expect(first.launch.workspaceAgentId).toBe("agent-1");
    // The stage is consumed: a fresh model starts unscoped in report mode.
    const second = new HelpModel({ storage: memoryStorage() });
    expect(second.mode).toBe("report");
    expect(takePendingHelpLaunch()).toBeNull();
  });

  it("defaults to report mode when a description was escalated by an agent", () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
      description: "diagnosis",
    });
    const model = new HelpModel({ storage: memoryStorage() });
    expect(model.mode).toBe("report");
    expect(model.description).toBe("diagnosis");
  });

  it("requires a description before submitting", async () => {
    const model = new HelpModel({ storage: memoryStorage() });
    await model.submit();
    expect(model.statusMessage).toBe("Please describe the problem first.");
    expect(model.isStatusError).toBe(true);
  });

  it("submits a report and surfaces the Sentry event id", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: (url, init) => {
        calls.push({ url, body: JSON.parse(String(init?.body)) });
        return Promise.resolve(jsonResponse({ ok: true, event_id: "abc123" }));
      },
    });
    model.description = "it broke";
    model.setRemoteAccessAllowed(true);

    await model.submit();

    expect(calls[0].url).toBe("/help/report");
    expect(calls[0].body).toMatchObject({
      description: "it broke",
      remote_access: true,
    });
    expect(model.phase).toBe("sent");
    expect(model.sentEventId).toBe("abc123");
  });

  it("ignores a second submit while one is already in flight", async () => {
    let resolveResponse: (response: Response) => void = () => undefined;
    let fetchCount = 0;
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () => {
        fetchCount += 1;
        return new Promise<Response>((resolve) => {
          resolveResponse = resolve;
        });
      },
    });
    model.description = "it broke";

    const inFlight = model.submit();
    expect(model.isSubmitBusy).toBe(true);
    await model.submit();
    expect(fetchCount).toBe(1);

    resolveResponse(jsonResponse({ ok: true, event_id: "abc123" }));
    await inFlight;
    expect(model.phase).toBe("sent");
  });

  it("swaps to the agent-error phase when the assist spawn fails", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () =>
        Promise.resolve(jsonResponse({ error: "no assist skill" }, 409)),
    });
    model.description = "help me";

    await model.submit();

    expect(model.phase).toBe("agent_error");
    expect(model.agentErrorMessage).toBe("no assist skill");
    model.backToReportFromError();
    expect(model.phase).toBe("form");
    expect(model.mode).toBe("report");
  });

  it("closes on a successful assist spawn", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    let closed = false;
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () => Promise.resolve(jsonResponse({ ok: true })),
      onClose: () => {
        closed = true;
      },
    });
    model.description = "help me";

    await model.submit();

    expect(closed).toBe(true);
  });

  it("persists the sticky remote-access preference", () => {
    const storage = memoryStorage();
    const model = new HelpModel({ storage });
    model.setRemoteAccessAllowed(true);
    expect(storage.values.get("minds.help.help-remote-access")).toBe("true");
    const next = new HelpModel({ storage });
    expect(next.isRemoteAccessAllowed).toBe(true);
  });

  it("includes workspace logs and the chat transcript unless the user has opted out", () => {
    const model = new HelpModel({ storage: memoryStorage() });
    expect(model.isLogsIncluded).toBe(true);
    expect(model.isTranscriptIncluded).toBe(true);
  });

  it("persists the sticky logs and transcript preferences independently", () => {
    const storage = memoryStorage();
    const model = new HelpModel({ storage });
    model.setLogsIncluded(false);
    expect(storage.values.get("minds.help.help-include-logs")).toBe("false");
    // Opting out of one says nothing about the other, which stays defaulted.
    expect(storage.values.has("minds.help.help-include-transcript")).toBe(
      false,
    );

    const next = new HelpModel({ storage });
    expect(next.isLogsIncluded).toBe(false);
    expect(next.isTranscriptIncluded).toBe(true);

    next.setLogsIncluded(true);
    next.setTranscriptIncluded(false);
    const third = new HelpModel({ storage });
    expect(third.isLogsIncluded).toBe(true);
    expect(third.isTranscriptIncluded).toBe(false);
  });

  it("sends the diagnostics choices with the report", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: (url, init) => {
        calls.push({ url, body: JSON.parse(String(init?.body)) });
        return Promise.resolve(jsonResponse({ ok: true, event_id: "abc123" }));
      },
    });
    model.description = "it broke";
    model.setTranscriptIncluded(false);

    await model.submit();

    const reportCall = calls.find((call) => call.url === "/help/report");
    expect(reportCall).toBeDefined();
    // Pinned exactly: this is the contract POST /help/report parses.
    expect(reportCall?.body).toEqual({
      description: "it broke",
      remote_access: false,
      workspace_agent_id: "agent-1",
      include_logs: true,
      include_transcript: false,
    });
  });
});

describe("HelpModel report-ID copy", () => {
  it("flashes the copied confirmation and clears it after the flash window", async () => {
    vi.useFakeTimers();
    try {
      const model = new HelpModel({
        storage: memoryStorage(),
        clipboardWrite: () => Promise.resolve(),
      });
      model.sentEventId = "abc123";

      await model.copyReportId();

      expect(model.isReportIdCopied).toBe(true);
      vi.runAllTimers();
      expect(model.isReportIdCopied).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows no confirmation when the clipboard write is rejected", async () => {
    // An insecure context or denied permission: the ID stays visible and
    // quotable, so the failure costs only the flash.
    const model = new HelpModel({
      storage: memoryStorage(),
      clipboardWrite: () => Promise.reject(new Error("denied")),
    });
    model.sentEventId = "abc123";

    await model.copyReportId();

    expect(model.isReportIdCopied).toBe(false);
  });
});
