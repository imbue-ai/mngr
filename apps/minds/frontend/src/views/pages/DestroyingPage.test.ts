// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import {
  buttonLabelled,
  buttonLabels,
  InertEventSource,
  jsonResponse,
  mountInBody,
  primaryButtonLabels,
  settle,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { DestroyingPage } from "./DestroyingPage";

const AGENT_ID = "agent-22";

interface DestroyRoutes {
  retry: { status: number; body: unknown };
}

/** A removal that has failed, whose Retry the route answers with `retry`. */
function stubFailedDestroy(routes: DestroyRoutes): string[] {
  const requests: string[] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    requests.push(`${method} ${url}`);
    if (method === "POST")
      return Promise.resolve(
        jsonResponse(routes.retry.body, routes.retry.status),
      );
    if (method === "DELETE") return Promise.resolve(jsonResponse({}, 200));
    return Promise.resolve(jsonResponse({ status: "FAILED" }));
  });
  return requests;
}

async function failedPage(): Promise<HTMLElement> {
  vi.spyOn(m.route, "param").mockReturnValue(AGENT_ID);
  const root = mountInBody(() => m(DestroyingPage));
  await settle();
  m.redraw.sync();
  return root;
}

beforeEach(() => {
  const stores = createEmptyStores();
  registerAppContext({ stores, shell: new ShellState(stores) });
  vi.stubGlobal("EventSource", InertEventSource);
});

afterEach(() => {
  clearAppContextForTests();
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DestroyingPage", () => {
  it("answers a retry refused for a workspace that is gone with Back to workspaces, as the only main button", async () => {
    const requests = stubFailedDestroy({
      retry: {
        status: 404,
        body: { error: "Unknown workspace", code: "WORKSPACE_UNKNOWN" },
      },
    });
    const routes: string[] = [];
    vi.spyOn(m.route, "set").mockImplementation(((path: string) => {
      routes.push(path);
    }) as typeof m.route.set);
    const root = await failedPage();

    buttonLabelled(root, "Retry").click();
    await settle();
    m.redraw.sync();
    const refusal = root.querySelector<HTMLElement>("#destroy-retry-error")!;

    expect(refusal.textContent).toContain(
      "Imbue Studio can't find this workspace any more.",
    );
    expect(primaryButtonLabels(root)).toEqual(["Back to workspaces"]);
    expect(() => buttonLabelled(root, "Retry")).toThrow();

    vi.useFakeTimers();
    buttonLabelled(refusal, "Back to workspaces").click();
    await vi.runAllTimersAsync();

    expect(requests).toContain(
      `DELETE /api/v1/workspaces/operations/destroy/${AGENT_ID}`,
    );
    expect(routes).toEqual(["/account/local/"]);
  });

  it("keeps Retry the main button when a refused retry can be tried again", async () => {
    stubFailedDestroy({ retry: { status: 500, body: null } });
    const root = await failedPage();

    buttonLabelled(root, "Retry").click();
    await settle();
    m.redraw.sync();
    const refusal = root.querySelector<HTMLElement>("#destroy-retry-error")!;

    expect(primaryButtonLabels(root)).toEqual(["Retry"]);
    expect(buttonLabels(refusal)).toEqual([
      "See details",
      "Retry",
      "Dismiss",
      "Report a bug",
    ]);
    expect(
      buttonLabels(root).filter((label) => label === "Report a bug"),
    ).toHaveLength(1);
  });
});
