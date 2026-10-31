import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import type { CreateAttemptDetail } from "../../models/create";
import {
  SETUP_LINE,
  SHOWCASE_APP,
  SHOWCASE_INTRO_LINE,
  readyTurnMarkdown,
  showcaseMarkdown,
} from "../../models/creationTranscript";
import {
  FLOW_OPTIONS_GAP_MS,
  FLOW_THINK_MS,
  MANIFESTO_OPENER,
  MANIFESTO_QUESTION,
  REPORTING_ASK,
  startFlow,
  streamDurationMs,
} from "../../models/startFlow";
import { manifestoOpenerMarkdown } from "../../models/welcomeChat";
import type { WelcomeChatBody } from "../../models/welcomeChat";
import type { AnyVnode } from "../../testing";
import { allText, byId, classTokensOf, collectText, attrsOf, collectVnodes, jsonResponse, renderDeep } from "../../testing";
import { Button } from "../components/Button";
import { CreatingPage, failureGuidance, failureTurn } from "./CreatingPage";

describe("failureGuidance", () => {
  it("explains the recognized clone failures and nothing else", () => {
    expect(allText(failureGuidance("GITHUB_AUTH_REQUIRED"))).toContain(
      "Install the GitHub app",
    );
    expect(allText(failureGuidance("GIT_AUTH_REQUIRED"))).toContain(
      "rejected anonymous access",
    );
    expect(allText(failureGuidance("GIT_REF_NOT_FOUND"))).toContain(
      "no branch or tag with that name",
    );
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
    const streamed = collectVnodes(turn).filter((node) =>
      String(attrsOf(node).class ?? "").includes("start-char"),
    );
    expect(streamed).toHaveLength(0);
    const message = collectVnodes(turn).find(
      (node) => attrsOf(node).id === "error-message",
    );
    expect(allText(message)).toBe("Could not create alpha: clone blew up");
  });
});

describe("CreatingPage across a route change", () => {
  afterEach(() => {
    startFlow.reset();
    vi.useRealTimers();
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function liveDetail(createAttemptId: string, isDriven = false): CreateAttemptDetail {
    return {
      kind: "live",
      record: null,
      live: {
        workspace_name: `workspace-${createAttemptId}`,
        provider_label: "",
        is_remote: false,
        request: {
          is_onboarding_driven_by_agent: isDriven,
          display_name: `workspace-${createAttemptId}`,
          launch_mode: "LIMA",
          cloud_account: "",
          cloud_account_alias: "",
          cloud_account_backend_label: "",
          backup_provider: "CONFIGURE_LATER",
          region: "",
          instance_type: "",
          cpu_count: null,
          memory_gb: null,
          disk_gb: null,
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
          is_onboarding_driven_by_agent: false,
          display_name: `workspace-${createAttemptId}`,
          launch_mode: "LIMA",
          cloud_account: "",
          cloud_account_alias: "",
          cloud_account_backend_label: "",
          backup_provider: "CONFIGURE_LATER",
          region: "",
          instance_type: "",
          cpu_count: null,
          memory_gb: null,
          disk_gb: null,
          repository: "https://example.com/repo.git",
          branch: "",
        },
        retry_unavailable_reason: "",
      },
    };
  }

  // The router hands the page its route params as attrs; the page's own type
  // declares none, so the vnode is built the way mithril would at runtime.
  function vnodeFor(createAttemptId: string): m.VnodeDOM {
    return m(CreatingPage, {
      agentId: createAttemptId,
    } as m.Attributes) as m.VnodeDOM;
  }

  /** The keys of the page's column, in order: the turns as they stand. */
  function columnKeys(rendered: m.Vnode): string[] {
    return (rendered.children as m.Vnode[]).map((child) => String(attrsOf(child).key));
  }

  /** The turn of the page's column keyed `key`. */
  function columnTurn(rendered: m.Vnode, key: string): AnyVnode {
    const turn = (rendered.children as m.Vnode[]).find((child) => attrsOf(child).key === key);
    expect(turn, `the ${key} turn`).toBeDefined();
    return turn as AnyVnode;
  }

  it("opens with the manifesto exchange even for an attempt the start flow did not submit", async () => {
    vi.stubGlobal("fetch", (url: string) =>
      Promise.resolve(
        jsonResponse(failedRecord(url.slice(url.lastIndexOf("/") + 1))),
      ),
    );
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor("create-attempt-c"));
    const render = (vnode: m.Vnode): m.Vnode =>
      (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);
    (page.oninit as (v: m.VnodeDOM) => void).call(
      page,
      vnodeFor("create-attempt-c"),
    );

    await vi.waitFor(() =>
      expect(allText(render(vnodeFor("create-attempt-c")))).toContain(
        "workspace-create-attempt-c",
      ),
    );

    const text = allText(render(vnodeFor("create-attempt-c")));
    expect(text.indexOf(MANIFESTO_OPENER)).toBeLessThan(
      text.indexOf(MANIFESTO_QUESTION),
    );
    expect(text.indexOf(MANIFESTO_QUESTION)).toBeLessThan(
      text.indexOf("Honest Software:"),
    );
    expect(text.indexOf("Honest Software:")).toBeLessThan(
      text.indexOf("Create a workspace with these settings:"),
    );
  });

  /** The op-log stream as the page opens it: its `onmessage` is where a log line arrives. */
  interface LogSource {
    onmessage: unknown;
  }

  /**
   * A live attempt whose status polls answer `status` (DONE unless given, until `setStatus` says
   * otherwise), with every welcome-chat post held open until the returned `settleSeed` is called. Reduced motion, so
   * the page enters directly instead of starting the wash, which measures the DOM.
   */
  function mountDoneAttempt(
    createAttemptId: string,
    status = "DONE",
    isDriven = false,
  ): {
    page: m.Component;
    entered: string[];
    seedBodies: WelcomeChatBody[];
    logSources: LogSource[];
    settleSeed: () => void;
    setStatus: (next: string) => void;
  } {
    vi.useFakeTimers();
    let currentStatus = status;
    const entered: string[] = [];
    const logSources: LogSource[] = [];
    registerAppContext({
      stores: { workspaces: { accentEntry: () => undefined } },
      shell: { enterWorkspace: (id: string) => entered.push(id) },
    } as unknown as AppContext);
    vi.stubGlobal("window", {
      matchMedia: () => ({ matches: true }),
      innerWidth: 800,
      innerHeight: 600,
    });
    vi.stubGlobal(
      "EventSource",
      class {
        onmessage: unknown = null;
        onerror: unknown = null;
        constructor() {
          logSources.push(this);
        }
        close(): void {}
      },
    );
    const seedBodies: WelcomeChatBody[] = [];
    const seedResolvers: ((response: Response) => void)[] = [];
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (url.endsWith("/welcome-chat")) {
        seedBodies.push(JSON.parse(init?.body as string) as WelcomeChatBody);
        return new Promise<Response>((resolve) => {
          seedResolvers.push(resolve);
        });
      }
      if (url.startsWith("/ui/api/create/attempts/"))
        return Promise.resolve(jsonResponse(liveDetail(createAttemptId, isDriven)));
      return Promise.resolve(
        jsonResponse({
          status: currentStatus,
          redirect_url: currentStatus === "DONE" ? "/goto/agent-0123abcd/" : "",
        }),
      );
    });
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor(createAttemptId));
    (page.oninit as (v: m.VnodeDOM) => void).call(
      page,
      vnodeFor(createAttemptId),
    );
    return {
      page,
      entered,
      seedBodies,
      logSources,
      settleSeed: () => {
        for (const resolve of seedResolvers)
          resolve(jsonResponse({ chat_id: "agent-seeded" }));
      },
      setStatus: (next: string) => {
        currentStatus = next;
      },
    };
  }

  it("hands the conversation to the finished attempt's workspace and enters it once the hand-off settles", async () => {
    const { page, entered, seedBodies, settleSeed } =
      mountDoneAttempt("create-attempt-d");
    const render = (vnode: m.Vnode): m.Vnode =>
      (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);

    // Handed over once the page knows the settings, and again once the attempt is done, which waits on the seed.
    await vi.waitFor(() => expect(seedBodies).toHaveLength(2));
    expect(seedBodies[1]).toEqual(seedBodies[0]);
    const turns = seedBodies[0].turns;
    expect(turns[0]).toEqual({
      role: "assistant",
      text: manifestoOpenerMarkdown(),
    });
    expect(turns[1]).toEqual({ role: "user", text: MANIFESTO_QUESTION });
    expect(turns[3].text).toContain(REPORTING_ASK);
    expect(turns[4].text).toContain("Name — workspace-create-attempt-d");
    expect(turns[turns.length - 1].text).toBe(readyTurnMarkdown());
    const root = render(vnodeFor("create-attempt-d"));
    const nodes = collectVnodes(root);
    // The settings turn, the setup line, the loading box at once (no recording), and the ready
    // line, which stands further from the box than one turn from the next.
    const keys = columnKeys(root);
    expect(keys.indexOf("creation-summary")).toBeLessThan(keys.indexOf("creation-setup"));
    expect(keys.indexOf("creation-setup")).toBeLessThan(keys.indexOf("creation-box"));
    expect(keys.indexOf("creation-box")).toBeLessThan(keys.indexOf("creation-ready"));
    expect(keys.indexOf("creation-ready")).toBeLessThan(keys.indexOf("scroll-anchor"));
    expect(keys).not.toContain("showcase-todo-list");
    const ready = nodes.find((node) => attrsOf(node).key === "creation-ready") as AnyVnode;
    expect(classTokensOf(ready)).toContain("mt-20");

    // The ready hold passes, but the workspace is not entered while the hand-off is still in flight.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(entered).toEqual([]);
    settleSeed();
    await vi.advanceTimersByTimeAsync(0);
    expect(entered).toEqual(["agent-0123abcd"]);
    (page.onremove as () => void).call(page);
  });

  it("folds the settings behind a toggle in the user's turn, and lists them once it is opened", async () => {
    const { page, seedBodies } = mountDoneAttempt("create-attempt-j", "CREATING_WORKSPACE");
    const render = (vnode: m.Vnode): m.Vnode =>
      (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);
    await vi.waitFor(() => expect(seedBodies).toHaveLength(1));

    const closed = render(vnodeFor("create-attempt-j"));
    expect(allText(closed)).toContain("Create a workspace with these settings:");
    const detailsOf = (rendered: m.Vnode): AnyVnode =>
      collectVnodes(rendered).find((node) => attrsOf(node).id === "creation-summary-details") as AnyVnode;
    expect(attrsOf(detailsOf(closed)).isOpen).toBe(false);
    expect(allText(detailsOf(closed))).toContain("Name — workspace-create-attempt-j");
    (attrsOf(detailsOf(closed)).onToggle as () => void)();
    expect(attrsOf(detailsOf(render(vnodeFor("create-attempt-j")))).isOpen).toBe(true);
    (page.onremove as () => void).call(page);
  });

  it("keeps the log's end in view as lines arrive, but not once the reader has scrolled up", async () => {
    const { page, logSources } = mountDoneAttempt("create-attempt-n", "CREATING_WORKSPACE");
    const render = (): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnodeFor("create-attempt-n"));
    await vi.waitFor(() => expect(columnKeys(render())).toContain("creation-box"));
    expect(logSources).toHaveLength(1);
    const feedLine = (line: string): void =>
      (logSources[0].onmessage as (event: { data: string }) => void)({ data: JSON.stringify({ log: line }) });
    const showDetails = collectVnodes(render()).find(
      (node) => node.tag === Button && allText(node) === "Show details",
    ) as AnyVnode;
    (attrsOf(showDetails).onclick as () => void)();

    // The <pre> only reads and moves its own scroll position, so a bare object stands in for it.
    const element = { scrollTop: 0, scrollHeight: 0, clientHeight: 100 };
    const logHooks = (): {
      oncreate: (vnode: { dom: unknown }) => void;
      onupdate: (vnode: { dom: unknown }) => void;
      onscroll: (event: { target: unknown }) => void;
    } => attrsOf(byId(render(), "logs") as AnyVnode) as ReturnType<typeof logHooks>;

    // Opened empty, there is nothing to pin to.
    logHooks().oncreate({ dom: element });
    expect(element.scrollTop).toBe(0);
    // Lines arrive: the end comes into view.
    feedLine("cloning");
    feedLine("building");
    element.scrollHeight = 400;
    logHooks().onupdate({ dom: element });
    expect(element.scrollTop).toBe(400);
    // A redraw with no new line (the progress timer's) moves nothing.
    element.scrollTop = 350;
    logHooks().onupdate({ dom: element });
    expect(element.scrollTop).toBe(350);
    // The reader scrolls up to read something: new lines no longer pull the end into view.
    element.scrollTop = 100;
    logHooks().onscroll({ target: element });
    feedLine("installing");
    element.scrollHeight = 600;
    logHooks().onupdate({ dom: element });
    expect(element.scrollTop).toBe(100);
    // Back at the end, the log follows the lines again.
    element.scrollTop = 500;
    logHooks().onscroll({ target: element });
    feedLine("starting");
    element.scrollHeight = 800;
    logHooks().onupdate({ dom: element });
    expect(element.scrollTop).toBe(800);
    (page.onremove as () => void).call(page);
  });

  it("shows an onboarding attempt's recording under its intro line, with no caption, before the box, and hands it over at once", async () => {
    // Submitted by the start flow, as an onboarding create is, so its turns arrive on the agent's clock.
    startFlow.submittedCreateAttemptId = "create-attempt-k";
    const { page, entered, seedBodies, settleSeed, setStatus } = mountDoneAttempt("create-attempt-k", "CREATING_WORKSPACE", true);
    const render = (): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnodeFor("create-attempt-k"));
    await vi.waitFor(() => expect(seedBodies).toHaveLength(1));

    // The intro and the recording sit under the setup line, the box under them.
    const building = render();
    const keys = columnKeys(building);
    expect(keys.indexOf("creation-setup")).toBeLessThan(keys.indexOf("creation-showcase-intro"));
    expect(keys.indexOf("creation-showcase-intro")).toBeLessThan(keys.indexOf("showcase-todo-list"));
    expect(keys.indexOf("showcase-todo-list")).toBeLessThan(keys.indexOf("creation-box"));
    expect(keys).not.toContain("creation-ready");
    expect(collectText(columnTurn(building, "creation-showcase-intro")).join("")).toContain(SHOWCASE_INTRO_LINE);
    const showcase = columnTurn(building, "showcase-todo-list");
    expect(collectText(showcase).join("").trim()).toBe("");
    const image = collectVnodes(showcase).find((node) => node.tag === "img") as AnyVnode;
    expect(attrsOf(image).src).toBe(SHOWCASE_APP.gifUrl);
    expect(attrsOf(image).alt).toBe(SHOWCASE_APP.label);
    // The recording arrives once its intro has landed, and the box a beat after the recording.
    const introAt = FLOW_THINK_MS + streamDurationMs(SETUP_LINE) + FLOW_OPTIONS_GAP_MS;
    const recordingAt = introAt + streamDurationMs(SHOWCASE_INTRO_LINE) + FLOW_OPTIONS_GAP_MS;
    expect(String(attrsOf(showcase).style)).toContain(`--start-chat-delay: ${recordingAt}ms`);
    expect(String(attrsOf(columnTurn(building, "creation-box")).style)).toContain(
      `--start-chat-delay: ${recordingAt + FLOW_THINK_MS}ms`,
    );

    // The conversation went over with the recording in it, ahead of the welcome.
    const tail = seedBodies[0].turns.slice(-3).map((turn) => turn.text);
    expect(tail).toEqual([SETUP_LINE, showcaseMarkdown(SHOWCASE_APP), readyTurnMarkdown()]);

    // The attempt finishes: the ready line lands under the box, and the entry waits for the seed.
    setStatus("DONE");
    await vi.advanceTimersByTimeAsync(2000);
    expect(seedBodies).toHaveLength(2);
    const done = columnKeys(render());
    expect(done.indexOf("creation-box")).toBeLessThan(done.indexOf("creation-ready"));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(entered).toEqual([]);
    settleSeed();
    await vi.advanceTimersByTimeAsync(0);
    expect(entered).toEqual(["agent-0123abcd"]);
    (page.onremove as () => void).call(page);
  });

  it("opens the recording enlarged on a click and closes it again", async () => {
    const { page } = mountDoneAttempt("create-attempt-q", "DONE", true);
    const render = (): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnodeFor("create-attempt-q"));
    await vi.waitFor(() => expect(columnKeys(render())).toContain("showcase-todo-list"));

    // The recording is a control; nothing is enlarged until it is pressed.
    const settled = renderDeep(render());
    expect(collectVnodes(settled).find((node) => attrsOf(node).role === "dialog")).toBeUndefined();
    const opener = collectVnodes(settled).find(
      (node) => attrsOf(node)["aria-label"] === "View the recording larger",
    ) as AnyVnode;
    expect(opener.tag).toBe("button");
    (attrsOf(opener).onclick as () => void)();

    // Enlarged: the same recording in a dialog, over the page.
    const enlarged = renderDeep(render());
    const dialog = collectVnodes(enlarged).find((node) => attrsOf(node).role === "dialog") as AnyVnode;
    expect(dialog).toBeDefined();
    expect(attrsOf(dialog)["aria-label"]).toBe(SHOWCASE_APP.label);
    const enlargedImage = collectVnodes(dialog).find(
      (node) => node.tag === "img" && attrsOf(node).src === SHOWCASE_APP.gifUrl,
    );
    expect(enlargedImage).toBeDefined();

    // Its close control takes it down again.
    const close = collectVnodes(enlarged).find(
      (node) => attrsOf(node)["aria-label"] === "Close image viewer",
    ) as AnyVnode;
    (attrsOf(close).onclick as () => void)();
    expect(collectVnodes(renderDeep(render())).find((node) => attrsOf(node).role === "dialog")).toBeUndefined();
    (page.onremove as () => void).call(page);
  });

  it("does not enter the workspace when the page was left while the hand-off was in flight", async () => {
    const { page, entered, seedBodies, settleSeed } =
      mountDoneAttempt("create-attempt-e");

    await vi.waitFor(() => expect(seedBodies).toHaveLength(2));
    await vi.advanceTimersByTimeAsync(30_000);
    (page.onremove as () => void).call(page);
    settleSeed();
    await vi.advanceTimersByTimeAsync(0);
    expect(entered).toEqual([]);
  });

  it("hands the conversation over while the workspace is still being built, so leaving the page cannot lose it", async () => {
    const { page, entered, seedBodies } = mountDoneAttempt(
      "create-attempt-f",
      "CREATING_WORKSPACE",
    );

    await vi.waitFor(() => expect(seedBodies).toHaveLength(1));
    const turns = seedBodies[0].turns;
    expect(turns[0]).toEqual({
      role: "assistant",
      text: manifestoOpenerMarkdown(),
    });
    expect(turns[turns.length - 1].text).toBe(readyTurnMarkdown());
    await vi.advanceTimersByTimeAsync(30_000);
    (page.onremove as () => void).call(page);
    expect(seedBodies).toHaveLength(1);
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
    const render = (vnode: m.Vnode): m.Vnode =>
      (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);

    (page.oninit as (v: m.VnodeDOM) => void).call(
      page,
      vnodeFor("create-attempt-a"),
    );
    await vi.waitFor(() =>
      expect(allText(render(vnodeFor("create-attempt-a")))).toContain(
        "workspace-create-attempt-a",
      ),
    );

    const later = vnodeFor("create-attempt-b");
    (page.onbeforeupdate as (v: m.VnodeDOM, old: m.VnodeDOM) => void).call(
      page,
      later,
      later,
    );
    // The same render already shows the new attempt loading, not a's failure.
    const loading = render(later);
    expect(attrsOf(loading)["data-agent-id"]).toBe("create-attempt-b");
    expect(allText(loading)).not.toContain("workspace-create-attempt-a");
    await vi.waitFor(() =>
      expect(allText(render(later))).toContain("workspace-create-attempt-b"),
    );
    expect(fetched).toEqual([
      "/ui/api/create/attempts/create-attempt-a",
      "/ui/api/create/attempts/create-attempt-b",
    ]);
  });
});
