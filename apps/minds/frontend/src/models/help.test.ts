import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { HelpModel, setPendingHelpLaunch, takePendingHelpLaunch } from "./help";

function memoryStorage(): Pick<Storage, "getItem" | "setItem"> & { values: Map<string, string> } {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
}

describe("HelpModel", () => {
  it("consumes the staged launch exactly once and defaults the mode from it", () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1", isAssistAvailable: true });
    const first = new HelpModel({ storage: memoryStorage() });
    expect(first.mode).toBe("agent");
    expect(first.launch.workspaceAgentId).toBe("agent-1");
    // The stage is consumed: a fresh model starts unscoped in report mode.
    const second = new HelpModel({ storage: memoryStorage() });
    expect(second.mode).toBe("report");
    expect(takePendingHelpLaunch()).toBeNull();
  });

  it("defaults to report mode when a description was escalated by an agent", () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1", isAssistAvailable: true, description: "diagnosis" });
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
    expect(calls[0].body).toMatchObject({ description: "it broke", remote_access: true });
    expect(model.phase).toBe("sent");
    expect(model.sentEventId).toBe("abc123");
  });

  it("swaps to the agent-error phase when the assist spawn fails", async () => {
    setPendingHelpLaunch({ workspaceAgentId: "agent-1", isAssistAvailable: true });
    const model = new HelpModel({
      storage: memoryStorage(),
      fetcher: () => Promise.resolve(jsonResponse({ error: "no assist skill" }, 409)),
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
    setPendingHelpLaunch({ workspaceAgentId: "agent-1", isAssistAvailable: true });
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
});
