import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import type { CreateAttemptDetail } from "../../models/create";
import { readyTurnMarkdown } from "../../models/creationTranscript";
import { MANIFESTO_QUESTION } from "../../models/startFlow";
import type { WelcomeChatBody } from "../../models/welcomeChat";
import { allText, attrsOf, collectVnodes, jsonResponse } from "../../testing";
import { CreatingPage, failureGuidance, failureTurn } from "./CreatingPage";

describe("failureGuidance", () => {
  it("explains the two recognized authentication failures and nothing else", () => {
    expect(allText(failureGuidance("GITHUB_AUTH_REQUIRED"))).toContain("Install the GitHub app");
    expect(allText(failureGuidance("GIT_AUTH_REQUIRED"))).toContain("rejected anonymous access");
    expect(failureGuidance("")).toBeNull();
    expect(failureGuidance("SOMETHING_ELSE")).toBeNull();
  });
});

describe("failureTurn", () => {
  it("keeps the ids the e2e workspace runner polls, around the failure text", () => {
    const turn = failureTurn("alpha", "clone blew up");
    const nodes = collectVnodes(turn);
    const view = nodes.find((node) => attrsOf(node).id === "failure-view");
    const message = nodes.find((node) => attrsOf(node).id === "error-message");
    expect(view).toBeDefined();
    expect(message).toBeDefined();
    expect(allText(message)).toBe("Could not create alpha: clone blew up");
  });

  it("lands the failure text whole rather than streamed", () => {
    // An error can run to pages, and streamed a character at a time it reads
    // as the app hanging while the cause is still spelling itself out.
    const turn = failureTurn("alpha", "clone blew up");
    const streamed = collectVnodes(turn).filter((node) => String(attrsOf(node).class ?? "").includes("start-char"));
    expect(streamed).toHaveLength(0);
    const message = collectVnodes(turn).find((node) => attrsOf(node).id === "error-message");
    expect(allText(message)).toBe("Could not create alpha: clone blew up");
  });
});

describe("CreatingPage across a route change", () => {
  afterEach(() => {
    vi.useRealTimers();
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function liveDetail(createAttemptId: string): CreateAttemptDetail {
    return {
      kind: "live",
      record: null,
      live: {
        workspace_name: `workspace-${createAttemptId}`,
        provider_label: "",
        is_remote: false,
        expected_duration_seconds: 60,
        request: {
          display_name: `workspace-${createAttemptId}`,
          launch_mode: "LIMA",
          cloud_account: "",
          backup_provider: "CONFIGURE_LATER",
          region: "",
          instance_type: "",
          repository: "https://example.com/repo.git",
          branch: "",
        },
      },
    };
  }

  function failedRecord(createAttemptId: string): CreateAttemptDetail {
    return {
      kind: "record",
      live: null,
      record: {
        state: "failed",
        workspace_name: `workspace-${createAttemptId}`,
        error: "boom",
        error_kind: "",
        log_tail: [],
        provider_label: "",
        request: {
          display_name: `workspace-${createAttemptId}`,
          launch_mode: "LIMA",
          cloud_account: "",
          backup_provider: "CONFIGURE_LATER",
          region: "",
          instance_type: "",
          repository: "https://example.com/repo.git",
          branch: "",
        },
      },
    };
  }

  // The router hands the page its route params as attrs; the page's own type
  // declares none, so the vnode is built the way mithril would at runtime.
  function vnodeFor(createAttemptId: string): m.VnodeDOM {
    return m(CreatingPage, { agentId: createAttemptId } as m.Attributes) as m.VnodeDOM;
  }

  it("opens with the manifesto exchange even for an attempt the start flow did not submit", async () => {
    vi.stubGlobal("fetch", (url: string) =>
      Promise.resolve(jsonResponse(failedRecord(url.slice(url.lastIndexOf("/") + 1)))),
    );
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor("create-attempt-c"));
    const render = (vnode: m.Vnode): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);
    (page.oninit as (v: m.VnodeDOM) => void).call(page, vnodeFor("create-attempt-c"));

    await vi.waitFor(() => expect(allText(render(vnodeFor("create-attempt-c")))).toContain("workspace-create-attempt-c"));

    const text = allText(render(vnodeFor("create-attempt-c")));
    expect(text.indexOf("Wait.. what is honest software?")).toBeLessThan(text.indexOf("Honest Software:"));
    expect(text.indexOf("Honest Software:")).toBeLessThan(text.indexOf("Create a workspace with these settings:"));
  });

  /**
   * A live attempt whose first status poll answers DONE, with the welcome-chat post held open
   * until the returned `settleSeed` is called. Reduced motion, so the page enters directly
   * instead of starting the wash, which measures the DOM.
   */
  function mountDoneAttempt(createAttemptId: string): {
    page: m.Component;
    entered: string[];
    seedBodies: WelcomeChatBody[];
    settleSeed: () => void;
  } {
    vi.useFakeTimers();
    const entered: string[] = [];
    registerAppContext({
      stores: { workspaces: { accentEntry: () => undefined } },
      shell: { enterWorkspace: (id: string) => entered.push(id) },
    } as unknown as AppContext);
    vi.stubGlobal("window", { matchMedia: () => ({ matches: true }), innerWidth: 800, innerHeight: 600 });
    vi.stubGlobal(
      "EventSource",
      class {
        onmessage: unknown = null;
        onerror: unknown = null;
        close(): void {}
      },
    );
    const seedBodies: WelcomeChatBody[] = [];
    let resolveSeed: (response: Response) => void = () => undefined;
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (url.endsWith("/welcome-chat")) {
        seedBodies.push(JSON.parse(init?.body as string) as WelcomeChatBody);
        return new Promise<Response>((resolve) => {
          resolveSeed = resolve;
        });
      }
      if (url.startsWith("/ui/api/create/attempts/")) return Promise.resolve(jsonResponse(liveDetail(createAttemptId)));
      return Promise.resolve(jsonResponse({ status: "DONE", redirect_url: "/goto/agent-0123abcd/" }));
    });
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor(createAttemptId));
    (page.oninit as (v: m.VnodeDOM) => void).call(page, vnodeFor(createAttemptId));
    return { page, entered, seedBodies, settleSeed: () => resolveSeed(jsonResponse({ chat_id: "agent-seeded" })) };
  }

  it("hands the conversation to the finished attempt's workspace and enters it once the hand-off settles", async () => {
    const { page, entered, seedBodies, settleSeed } = mountDoneAttempt("create-attempt-d");
    const render = (vnode: m.Vnode): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);

    await vi.waitFor(() => expect(seedBodies).toHaveLength(1));
    const turns = seedBodies[0].turns;
    expect(turns[0]).toEqual({ role: "user", text: MANIFESTO_QUESTION });
    expect(turns[2].text).toContain("Name — workspace-create-attempt-d");
    expect(turns[turns.length - 1].text).toBe(readyTurnMarkdown());
    const nodes = collectVnodes(render(vnodeFor("create-attempt-d")));
    expect(nodes.some((node) => attrsOf(node).key === "creation-ready")).toBe(true);
    expect(allText(nodes.find((node) => attrsOf(node).id === "start-options"))).toContain("Start with an app");

    // The ready hold passes, but the workspace is not entered while the hand-off is still in flight.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(entered).toEqual([]);
    settleSeed();
    await vi.advanceTimersByTimeAsync(0);
    expect(entered).toEqual(["agent-0123abcd"]);
    (page.onremove as () => void).call(page);
  });

  it("does not enter the workspace when the page was left while the hand-off was in flight", async () => {
    const { page, entered, seedBodies, settleSeed } = mountDoneAttempt("create-attempt-e");

    await vi.waitFor(() => expect(seedBodies).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(30_000);
    (page.onremove as () => void).call(page);
    settleSeed();
    await vi.advanceTimersByTimeAsync(0);
    expect(entered).toEqual([]);
  });

  it("picks up the attempt the route now names instead of keeping the first one", async () => {
    // The router reuses the page instance when the retry routes
    // /creating/<a> -> /creating/<b>, so oninit does not run again.
    const fetched: string[] = [];
    vi.stubGlobal("fetch", (url: string) => {
      fetched.push(url);
      const createAttemptId = url.slice(url.lastIndexOf("/") + 1);
      return Promise.resolve(jsonResponse(failedRecord(createAttemptId)));
    });
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor("create-attempt-a"));
    const render = (vnode: m.Vnode): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);

    (page.oninit as (v: m.VnodeDOM) => void).call(page, vnodeFor("create-attempt-a"));
    await vi.waitFor(() => expect(allText(render(vnodeFor("create-attempt-a")))).toContain("workspace-create-attempt-a"));

    const later = vnodeFor("create-attempt-b");
    (page.onbeforeupdate as (v: m.VnodeDOM, old: m.VnodeDOM) => void).call(page, later, later);
    // The same render already shows the new attempt loading, not a's failure.
    const loading = render(later);
    expect(attrsOf(loading)["data-agent-id"]).toBe("create-attempt-b");
    expect(allText(loading)).not.toContain("workspace-create-attempt-a");
    await vi.waitFor(() => expect(allText(render(later))).toContain("workspace-create-attempt-b"));
    expect(fetched).toEqual(["/ui/api/create/attempts/create-attempt-a", "/ui/api/create/attempts/create-attempt-b"]);
  });
});
