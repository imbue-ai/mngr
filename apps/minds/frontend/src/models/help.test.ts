import { describe, expect, it, vi } from "vitest";
import { jsonResponse, memoryStorage } from "../testing";
import {
  HelpModel,
  setPendingHelpLaunch,
  stageHelpLaunchFromRoute,
  takePendingHelpLaunch,
} from "./help";

/** A fetcher that answers every request with `responseBody` and records each
 * request's url and parsed JSON body in `calls`. */
function recordingFetcher(responseBody: unknown): {
  calls: Array<{ url: string; body: unknown }>;
  fetcher: (url: string, init?: RequestInit) => Promise<Response>;
} {
  const calls: Array<{ url: string; body: unknown }> = [];
  return {
    calls,
    fetcher: (url, init) => {
      calls.push({ url, body: JSON.parse(String(init?.body)) });
      return Promise.resolve(jsonResponse(responseBody));
    },
  };
}

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

  it("lands a Report a bug on the report form and sends what the error held with the report", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
      errorDetails:
        "Couldn't rename this workspace.\n\nHTTP 500\nrename failed",
    });
    const { calls, fetcher } = recordingFetcher({
      ok: true,
      event_id: "abc123",
    });
    const model = new HelpModel({ storage: memoryStorage(), fetcher });
    // The user asked to report, so the agent mode is not the default even
    // where it is offered.
    expect(model.mode).toBe("report");
    model.description = "I tried to rename it";

    await model.submit();

    expect(calls[0].url).toBe("/help/report");
    expect(calls[0].body).toMatchObject({
      description: "I tried to rename it",
      error_details:
        "Couldn't rename this workspace.\n\nHTTP 500\nrename failed",
    });
  });

  it("collects from the workspace an error was about even when the form is not over it", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "",
      errorDetails: "HTTP 502",
      errorWorkspaceAgentId: "agent-1",
    });
    const { calls, fetcher } = recordingFetcher({
      ok: true,
      event_id: "abc123",
    });
    const model = new HelpModel({ storage: memoryStorage(), fetcher });
    model.description = "it broke";

    await model.submit();

    expect(calls[0].body).toMatchObject({ workspace_agent_id: "agent-1" });
  });

  it("hands the attached details to the agent too when the user switches to agent help", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
      errorDetails: "HTTP 500\nrename failed",
    });
    const { calls, fetcher } = recordingFetcher({ ok: true });
    const model = new HelpModel({ storage: memoryStorage(), fetcher });
    model.mode = "agent";
    model.description = "fix the rename";

    await model.submit();

    expect(calls[0].url).toBe("/help/assist");
    expect(calls[0].body).toEqual({
      description: "fix the rename\n\nHTTP 500\nrename failed",
      workspace_agent_id: "agent-1",
    });
  });

  it("requires a description before submitting", async () => {
    const model = new HelpModel({ storage: memoryStorage() });
    await model.submit();
    expect(model.statusMessage).toBe("Please describe the problem first.");
    expect(model.isStatusError).toBe(true);
  });

  it("stays open to an arriving report after an empty Send, but not after a failed one", async () => {
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () =>
        Promise.resolve(jsonResponse({ error: "upstream down" }, 502)),
    });

    await model.submit();
    expect(model.isStatusError).toBe(true);
    expect(model.isAwaitingInput).toBe(true);

    model.description = "it broke";
    await model.submit();
    model.description = "";
    await model.submit();
    expect(model.statusMessage).toBe("Please describe the problem first.");
    expect(model.isAwaitingInput).toBe(false);
  });

  it("submits a report and surfaces the Sentry event id", async () => {
    const { calls, fetcher } = recordingFetcher({
      ok: true,
      event_id: "abc123",
    });
    const model = new HelpModel({ storage: memoryStorage(), fetcher });
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

  it("swaps to the agent-error phase with our sentence and carries the failure into the report", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    const calls: Array<{ url: string; body: unknown }> = [];
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: (url, init) => {
        calls.push({ url, body: JSON.parse(String(init?.body)) });
        if (url === "/help/assist")
          return Promise.resolve(
            jsonResponse(
              {
                error: "Couldn't start an agent in this workspace.",
                reason: "SPAWN_FAILED",
                detail: "Error: Unknown fields in agent_types.opencode",
              },
              502,
            ),
          );
        return Promise.resolve(jsonResponse({ ok: true, event_id: "abc" }));
      },
    });
    model.description = "help me";

    await model.submit();

    expect(model.phase).toBe("agent_error");
    expect(model.agentError?.message).toBe(
      "Couldn't start an agent in this workspace. Try again. If this persists, report a bug.",
    );
    // The workspace's verdict is kept, behind See details.
    expect(model.agentError?.details).toContain(
      "Error: Unknown fields in agent_types.opencode",
    );

    model.backToReportFromError();
    expect(model.phase).toBe("form");
    expect(model.mode).toBe("report");
    await model.submit();
    const report = calls[1].body as { error_details: string };
    expect(calls[1].url).toBe("/help/report");
    expect(report.error_details).toContain("Unknown fields in agent_types");
  });

  it("names a workspace too old for a helper and a request that got no answer apart from the fallback", async () => {
    const answers: Array<() => Promise<Response>> = [
      () =>
        Promise.resolve(
          jsonResponse({ error: "too old", reason: "TOO_OLD" }, 409),
        ),
      () => Promise.reject(new TypeError("Failed to fetch")),
    ];
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () => (answers.shift() as () => Promise<Response>)(),
    });
    model.description = "help me";

    await model.submit();
    expect(model.agentErrorReason).toBe("TOO_OLD");
    expect(model.agentError?.message).toBe(
      "This workspace can't start a helper agent yet. *Update it, then try again.*",
    );

    await model.retryAgentHelp();
    expect(model.agentErrorReason).toBeNull();
    expect(model.agentError?.message).toBe(
      "Imbue Studio didn't respond. Try again in a moment.",
    );
  });

  it("shows only a refusal's own sentence, keeping any other text it answered behind See details", async () => {
    const answers: Array<() => Response> = [
      () =>
        new Response("<title>405 Method Not Allowed</title>", {
          status: 405,
          headers: { "Content-Type": "text/html" },
        }),
      () => jsonResponse({ detail: "workspace_agent_id is malformed" }, 400),
      () =>
        jsonResponse(
          { error: "Describe what the agent should help with, then try again." },
          400,
        ),
    ];
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () => Promise.resolve((answers.shift() as () => Response)()),
    });
    model.description = "help me";
    const fallback =
      "Couldn't start an agent in this workspace. Try again. If this persists, report a bug.";

    await model.submit();
    expect(model.agentError?.message).toBe(fallback);
    expect(model.agentError?.details).toContain("405 Method Not Allowed");

    await model.retryAgentHelp();
    expect(model.agentError?.message).toBe(fallback);
    expect(model.agentError?.details).toContain(
      "workspace_agent_id is malformed",
    );

    await model.retryAgentHelp();
    expect(model.agentError?.message).toBe(
      "Describe what the agent should help with, then try again.",
    );
  });

  it("starts the agent on Try again after a failed start", async () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
    });
    const answers = [
      jsonResponse(
        { error: "x", reason: "START_FAILED", detail: "quota" },
        502,
      ),
      jsonResponse({ ok: true }),
    ];
    const sent: unknown[] = [];
    let closed = false;
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: (_url, init) => {
        sent.push(JSON.parse(String(init?.body)));
        return Promise.resolve(answers.shift() as Response);
      },
      onClose: () => {
        closed = true;
      },
    });
    model.description = "help me";

    await model.submit();
    expect(model.agentError?.message).toBe(
      "Couldn't start this workspace. Try again. If this persists, report a bug.",
    );
    await model.retryAgentHelp();

    expect(closed).toBe(true);
    expect(sent).toEqual([sent[0], sent[0]]);
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
    setPendingHelpLaunch({ workspaceAgentId: "agent-1" });
    const { calls, fetcher } = recordingFetcher({
      ok: true,
      event_id: "abc123",
    });
    const model = new HelpModel({ storage: memoryStorage(), fetcher });
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
      error_details: "",
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

describe("stageHelpLaunchFromRoute", () => {
  it("keeps the agent's staged diagnosis when the route also names the machine", () => {
    // A route param must not blank the fields it does not carry: the
    // agent-report launch rides both carriers at once (see
    // stageHelpLaunchFromRoute).
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      description: "the diagnosis",
      isAgentReport: true,
      workspaceName: "octopus",
    });

    stageHelpLaunchFromRoute({ workspace: "agent-1" });

    const launch = takePendingHelpLaunch();
    expect(launch).toEqual({
      workspaceAgentId: "agent-1",
      isAssistAvailable: false,
      description: "the diagnosis",
      isAgentReport: true,
      workspaceName: "octopus",
      errorDetails: "",
      errorWorkspaceAgentId: "",
    });
  });

  it("lets the route's params win over the staged launch, field by field", () => {
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      description: "staged",
      workspaceName: "octopus",
    });

    stageHelpLaunchFromRoute({
      workspace: "agent-1",
      assist: "1",
      description: "from the link",
    });

    expect(takePendingHelpLaunch()).toEqual({
      workspaceAgentId: "agent-1",
      isAssistAvailable: true,
      description: "from the link",
      isAgentReport: false,
      workspaceName: "octopus",
      errorDetails: "",
      errorWorkspaceAgentId: "",
    });
  });

  it("drops a launch staged for a machine other than the one the route names", () => {
    // A launch left staged by an ask nothing consumed is not this route's:
    // merged, the form would be addressed to the machine the route names --
    // and so would the logs and transcript the report collects -- while
    // holding what the other machine said.
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      description: "the diagnosis",
      isAgentReport: true,
      workspaceName: "octopus",
    });

    stageHelpLaunchFromRoute({ workspace: "agent-2" });

    expect(takePendingHelpLaunch()).toEqual({
      workspaceAgentId: "agent-2",
      isAssistAvailable: false,
      description: "",
      isAgentReport: false,
      workspaceName: "",
      errorDetails: "",
      errorWorkspaceAgentId: "",
    });
  });

  it("keeps a machine's report off a Help opened over no machine", () => {
    // The titlebar bug button on Home routes to plain /help; a report staged
    // for a machine and never shown must not surface there as if it were this
    // open's own.
    setPendingHelpLaunch({
      workspaceAgentId: "agent-1",
      description: "the diagnosis",
      isAgentReport: true,
    });

    stageHelpLaunchFromRoute({});

    expect(takePendingHelpLaunch()).toBeNull();

    setPendingHelpLaunch({ description: "no machine", isAgentReport: true });

    stageHelpLaunchFromRoute({});

    expect(takePendingHelpLaunch()).toMatchObject({
      workspaceAgentId: "",
      description: "no machine",
    });
  });

  it("stages nothing when neither the route nor the opener carried a launch", () => {
    stageHelpLaunchFromRoute({});

    expect(takePendingHelpLaunch()).toBeNull();
  });
});
