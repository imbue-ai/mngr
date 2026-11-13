// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AskAgentModel } from "../../models/assistChat";
import { jsonResponse, mountInBody } from "../../testing";
import {
  AskAgentButton,
  AskAgentFailure,
  askAgentActions,
} from "./AskAgentButton";
import { ErrorNotice } from "./ErrorDisplay";

const WORKSPACE_ID = "agent-ab12";

function mountButton(model: AskAgentModel): HTMLElement {
  return mountInBody(() =>
    m(AskAgentButton, {
      id: "ask-agent",
      model,
      workspaceAgentId: WORKSPACE_ID,
      description: "The sharing list can't be read. Please repair it.",
      label: "Ask the agent to fix it",
    }),
  );
}

/** A model whose every start gets `answer` back from the workspace. */
function modelAnswering(answer: () => Promise<Response>): AskAgentModel {
  return new AskAgentModel({ fetcher: answer, onStarted: () => undefined });
}

/** An error notice carrying the ask as its action, with a failed start drawn
 * as its footer. */
function mountAskNotice(
  model: AskAgentModel,
  isBugReportOffered?: boolean,
): HTMLElement {
  return mountInBody(() =>
    m(ErrorNotice, {
      id: "service-config",
      error: {
        message: "The restored settings couldn't be read.",
        details: "",
      },
      isBugReportOffered,
      actions: askAgentActions({
        id: "ask-agent",
        model,
        workspaceAgentId: WORKSPACE_ID,
        description: "Repair the restored settings.",
        label: "Ask the agent to fix it",
      }),
      footer: m(AskAgentFailure, {
        id: "ask-agent",
        model,
        workspaceAgentId: WORKSPACE_ID,
      }),
    }),
  );
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("AskAgentButton", () => {
  it("starts a chat in the workspace seeded with the description, then hands off", async () => {
    const requests: Array<{ url: string; body: unknown }> = [];
    const started = vi.fn();
    const model = new AskAgentModel({
      fetcher: (url, init) => {
        requests.push({ url, body: JSON.parse(String(init?.body)) });
        return Promise.resolve(jsonResponse({ ok: true }));
      },
      onStarted: started,
    });
    const root = mountButton(model);

    root.querySelector<HTMLButtonElement>("#ask-agent")?.click();
    await vi.waitFor(() => expect(started).toHaveBeenCalledWith(WORKSPACE_ID));

    expect(requests).toEqual([
      {
        url: "/help/assist",
        body: {
          description: "The sharing list can't be read. Please repair it.",
          workspace_agent_id: WORKSPACE_ID,
        },
      },
    ]);
    m.redraw.sync();
    expect(root.querySelector("[role=alert]")).toBeNull();
  });

  it("shows why the start failed under the button, with the workspace's verdict behind See details", async () => {
    const started = vi.fn();
    const model = new AskAgentModel({
      fetcher: () =>
        Promise.resolve(
          jsonResponse(
            {
              error: "Couldn't start an agent in this workspace.",
              detail: "no account is signed in",
            },
            502,
          ),
        ),
      onStarted: started,
    });
    const root = mountButton(model);

    root.querySelector<HTMLButtonElement>("#ask-agent")?.click();
    await vi.waitFor(() => expect(model.error).not.toBeNull());
    m.redraw.sync();

    expect(started).not.toHaveBeenCalled();
    expect(root.querySelector("[role=alert]")).not.toBeNull();
    expect(model.error?.details).toContain("no account is signed in");
    expect(root.querySelector<HTMLButtonElement>("#ask-agent")?.disabled).toBe(
      false,
    );
  });

  it("draws a failed start under the buttons of a notice that carries the ask as its action", async () => {
    const model = modelAnswering(() =>
      Promise.resolve(
        jsonResponse(
          { error: "Couldn't start an agent in this workspace." },
          502,
        ),
      ),
    );
    const root = mountAskNotice(model);

    root.querySelector<HTMLButtonElement>("#ask-agent")?.click();
    await vi.waitFor(() => expect(model.error).not.toBeNull());
    m.redraw.sync();

    const buttonRow = root.querySelector("#ask-agent")?.parentElement;
    const failure = root.querySelector("#ask-agent-error");
    expect(failure?.textContent).toContain("Couldn't start an agent");
    expect(buttonRow?.contains(failure ?? null)).toBe(false);
  });

  it("does not claim the workspace refused, or ask for a report, when Imbue Studio never answered", async () => {
    const model = modelAnswering(() =>
      Promise.reject(new TypeError("Failed to fetch")),
    );
    const root = mountButton(model);

    root.querySelector<HTMLButtonElement>("#ask-agent")?.click();
    await vi.waitFor(() => expect(model.error).not.toBeNull());
    m.redraw.sync();

    expect(model.error?.details).toBe("No response.");
    expect(model.error?.message).not.toContain("this workspace");
    expect(root.querySelector("#ask-agent")).not.toBeNull();
    expect(root.textContent).not.toContain("Report a bug");
  });

  it("treats a proxy's plain-text 404 as a failure to retry or report, not a refusal", async () => {
    const model = modelAnswering(() =>
      Promise.resolve(new Response("Not Found", { status: 404 })),
    );
    const root = mountButton(model);

    root.querySelector<HTMLButtonElement>("#ask-agent")?.click();
    await vi.waitFor(() => expect(model.error).not.toBeNull());
    m.redraw.sync();

    expect(model.error?.message).toBe(
      "Couldn't start an agent in this workspace. Try again. If this persists, report a bug.",
    );
    expect(model.error?.details).toBe("HTTP 404\nNot Found");
    expect(root.querySelector("#ask-agent")).not.toBeNull();
    expect(root.textContent).toContain("Report a bug");
  });

  it("withdraws the ask once the route refused it, leaving the refusal with no report to file", async () => {
    const model = modelAnswering(() =>
      Promise.resolve(
        jsonResponse(
          { error: "This workspace doesn't have the agent-assist skill." },
          409,
        ),
      ),
    );
    const root = mountAskNotice(model, false);

    root.querySelector<HTMLButtonElement>("#ask-agent")?.click();
    await vi.waitFor(() => expect(model.error).not.toBeNull());
    m.redraw.sync();

    expect(model.error?.message).toBe(
      "This workspace doesn't have the agent-assist skill.",
    );
    expect(root.querySelector("#ask-agent")).toBeNull();
    expect(root.querySelector("#ask-agent-error")?.textContent).toContain(
      "doesn't have the agent-assist skill",
    );
    expect(root.textContent).not.toContain("Report a bug");
  });

  it("starts only one chat for a double press", async () => {
    let requestCount = 0;
    const model = modelAnswering(() => {
      requestCount += 1;
      return Promise.resolve(jsonResponse({ ok: true }));
    });

    await Promise.all([
      model.start({ workspaceAgentId: WORKSPACE_ID, description: "help" }),
      model.start({ workspaceAgentId: WORKSPACE_ID, description: "help" }),
    ]);

    expect(requestCount).toBe(1);
  });
});
