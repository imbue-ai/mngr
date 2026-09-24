// @vitest-environment jsdom
//
// Mounted under the real router rather than rendered by hand: what is under
// test is the page's behavior when Mithril keeps its instance across a /help
// param change, which only the real m.route does.
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import {
  setPendingHelpLaunch,
  stagedHelpLaunchWorkspace,
  takePendingHelpLaunch,
} from "../../models/help";
import { jsonResponse, settle, workspacesMessage } from "../../testing";
import { ShellState } from "../shell/shell-state";
import { HelpPage } from "./HelpPage";

const ALPHA = "agent-aa11";
const BETA = "agent-cc33";

const ROUTES: m.RouteDefs = {
  "/help": HelpPage,
  "/workspace/:workspaceId": { view: () => m("div#machine-surface") },
};

let root: HTMLElement | null = null;

afterEach(async () => {
  takePendingHelpLaunch();
  // Let any route.set still in flight resolve before the router is torn down,
  // so it cannot remount into the next test's root.
  await settle();
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A shell that knows machines alpha and beta, so an agent's ask is staged
 * with its machine's name the way the app stages it. */
function shellWithTwoMachines(): ShellState {
  const shell = new ShellState(createEmptyStores());
  const [alpha] = workspacesMessage().workspaces;
  shell.stores.workspaces.applyWorkspacesMessage(
    workspacesMessage({
      workspaces: [
        alpha,
        { ...alpha, id: BETA, name: "beta", host_id: "host-dd44" },
      ],
      restorable_workspace_ids: [ALPHA, BETA],
    }),
  );
  return shell;
}

/** Open Help with alpha's agent report on it, as its first arrival does. */
function openAlphaReport(description = "alpha's diagnosis"): void {
  setPendingHelpLaunch({
    workspaceAgentId: ALPHA,
    description,
    isAgentReport: true,
    workspaceName: "alpha",
  });
  window.history.replaceState(null, "", `#!/help?workspace=${ALPHA}`);
  root = document.createElement("div");
  document.body.appendChild(root);
  m.route(root, "/help", ROUTES);
}

/** Let a route.set land, then draw what it landed on. */
async function landRoute(): Promise<void> {
  await settle();
  m.redraw.sync();
}

/** An agent's report arriving while Help is up: staged, the redraw index.ts
 * asks for while the route still names the previous machine, then the route
 * landing on the reporting machine. */
async function arriveReport(
  shell: ShellState,
  workspaceAgentId: string,
  description: string,
): Promise<void> {
  shell.openHelpForAgentAsk(workspaceAgentId, description);
  m.redraw.sync();
  await landRoute();
}

function formDescription(): string | null {
  const textarea = document.getElementById("help-description");
  return textarea === null ? null : (textarea as HTMLTextAreaElement).value;
}

function pageText(): string {
  return root?.textContent ?? "";
}

function clickButton(label: string): void {
  const button = Array.from(document.querySelectorAll("button")).find(
    (candidate) => candidate.textContent === label,
  );
  if (button === undefined) throw new Error(`no ${label} button on screen`);
  button.click();
}

function stubReportPost(
  respond: () => Promise<Response>,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) => respond());
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function postedReport(
  fetchMock: ReturnType<typeof vi.fn>,
  index: number,
): Record<string, unknown> {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  expect(url).toBe("/help/report");
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

async function sendAlphaReportThrough(): Promise<void> {
  stubReportPost(async () => jsonResponse({ event_id: "evt-alpha" }));
  clickButton("Send report");
  await settle();
  m.redraw.sync();
  expect(pageText()).toContain("Thanks!");
  expect(pageText()).toContain("evt-alpha");
}

describe("HelpPage when another report arrives while it is up", () => {
  it("rebuilds the form for a report from another machine once the route names it", async () => {
    const shell = shellWithTwoMachines();
    openAlphaReport();
    expect(formDescription()).toBe("alpha's diagnosis");

    shell.openHelpForAgentAsk(BETA, "beta's diagnosis");
    // The redraw index.ts asks for right after the ask, while the route still
    // names alpha: beta's report must neither show under alpha nor be spent.
    m.redraw.sync();
    expect(formDescription()).toBe("alpha's diagnosis");
    expect(stagedHelpLaunchWorkspace()).toBe(BETA);

    await landRoute();

    expect(formDescription()).toBe("beta's diagnosis");
    expect(pageText()).toContain("An agent in machine beta wants to submit");
    expect(takePendingHelpLaunch()).toBeNull();
    const fetchMock = stubReportPost(async () =>
      jsonResponse({ event_id: "evt-beta" }),
    );
    clickButton("Send report");
    await settle();
    expect(postedReport(fetchMock, 0)).toMatchObject({
      workspace_agent_id: BETA,
      description: "beta's diagnosis",
    });
  });

  it("shows a second report from the same machine", async () => {
    const shell = shellWithTwoMachines();
    openAlphaReport("first diagnosis");

    await arriveReport(shell, ALPHA, "second diagnosis");

    expect(formDescription()).toBe("second diagnosis");
    expect(takePendingHelpLaunch()).toBeNull();
  });

  it("keeps the form of a report being sent, which ends on its own Thanks", async () => {
    const shell = shellWithTwoMachines();
    openAlphaReport();
    let answerPost: (response: Response) => void = () => undefined;
    const fetchMock = stubReportPost(
      () =>
        new Promise<Response>((resolve) => {
          answerPost = resolve;
        }),
    );
    clickButton("Send report");
    m.redraw.sync();

    await arriveReport(shell, BETA, "beta's diagnosis");

    expect(formDescription()).toBe("alpha's diagnosis");
    expect(pageText()).toContain("Sending...");
    expect(postedReport(fetchMock, 0)).toMatchObject({
      workspace_agent_id: ALPHA,
      description: "alpha's diagnosis",
    });
    answerPost(jsonResponse({ event_id: "evt-alpha" }));
    await landRoute();
    expect(pageText()).toContain("evt-alpha");
    expect(formDescription()).toBeNull();
  });

  it("keeps a form the user has written in, and shows the arrival after its Thanks", async () => {
    const shell = shellWithTwoMachines();
    openAlphaReport();
    const textarea = document.getElementById(
      "help-description",
    ) as HTMLTextAreaElement;
    textarea.value = "my own words";
    textarea.dispatchEvent(new Event("input"));

    await arriveReport(shell, BETA, "beta's diagnosis");

    expect(formDescription()).toBe("my own words");
    await sendAlphaReportThrough();
    clickButton("Done");
    await landRoute();
    expect(formDescription()).toBe("beta's diagnosis");
    expect(takePendingHelpLaunch()).toBeNull();
  });

  it("keeps a report whose send failed, and shows the arrival after a retry's Thanks", async () => {
    const shell = shellWithTwoMachines();
    openAlphaReport();
    stubReportPost(async () => jsonResponse({ error: "upstream down" }, 502));
    clickButton("Send report");
    await settle();
    m.redraw.sync();
    expect(pageText()).toContain("upstream down");

    await arriveReport(shell, BETA, "beta's diagnosis");

    expect(formDescription()).toBe("alpha's diagnosis");
    await sendAlphaReportThrough();
    clickButton("Done");
    await landRoute();
    expect(formDescription()).toBe("beta's diagnosis");
    expect(takePendingHelpLaunch()).toBeNull();
  });

  it("holds a report that arrives on the Thanks screen until Done, then shows it", async () => {
    const shell = shellWithTwoMachines();
    openAlphaReport();
    await sendAlphaReportThrough();

    await arriveReport(shell, BETA, "beta's diagnosis");
    expect(pageText()).toContain("evt-alpha");
    expect(formDescription()).toBeNull();

    clickButton("Done");
    await landRoute();

    expect(formDescription()).toBe("beta's diagnosis");
    expect(pageText()).toContain("An agent in machine beta wants to submit");
    expect(m.route.param("workspace")).toBe(BETA);
    expect(takePendingHelpLaunch()).toBeNull();
  });

  it("reopens for a report held on the Thanks screen when Help is navigated away from", async () => {
    // Every other way off the Thanks screen -- the close X, the backdrop,
    // Escape, a titlebar switch, the sidebar, Back -- is a navigation away
    // from /help, which is what this stands in for.
    const shell = shellWithTwoMachines();
    openAlphaReport();
    await sendAlphaReportThrough();
    await arriveReport(shell, BETA, "beta's diagnosis");

    m.route.set(`/workspace/${ALPHA}`);
    await landRoute();
    await landRoute();

    expect(m.route.get()).toBe(`/help?workspace=${BETA}`);
    expect(formDescription()).toBe("beta's diagnosis");
    expect(takePendingHelpLaunch()).toBeNull();
  });
});
