import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import type { CreateAttemptDetail } from "../../models/create";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import {
  READY_LINE,
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
import { wash } from "../../models/wash";
import { manifestoOpenerMarkdown } from "../../models/welcomeChat";
import { WELCOME_CHAT_WAIT_BOUND_MS, welcomeChatArrival } from "../../models/welcomeChatArrival";
import type { WelcomeChatBody } from "../../models/welcomeChat";
import type { AnyVnode } from "../../testing";
import {
  InertEventSource,
  allText,
  attrsOf,
  byId,
  classTokensOf,
  classesOf,
  collectText,
  collectVnodes,
  jsonResponse,
  renderDeep,
  withAttr,
  withMindsNative,
} from "../../testing";
import { Button } from "../components/Button";
import { BTN_VARIANTS } from "../components/constants";
import { ChatError } from "./start/ChatError";
import type { ChatErrorAttrs } from "./start/ChatError";
import { ShellState } from "../shell/shell-state";
import { CreatingPage, failureTurn } from "./CreatingPage";

describe("failureTurn", () => {
  /** The turn as one live component, so its See details stays open across a
   * redraw the way it does on the page the e2e runner drives. */
  function drawFailureTurn(error: UserFacingError): () => unknown {
    const turn = failureTurn(error) as m.Vnode<ChatErrorAttrs>;
    const component = (turn.tag as () => m.Component<ChatErrorAttrs>)();
    return () => renderDeep(component.view(turn));
  }

  /** The node carrying `id`; throws when the turn does not name one. */
  function nodeById(tree: unknown, id: string): AnyVnode {
    const node = collectVnodes(tree).find((candidate) => attrsOf(candidate).id === id);
    if (node === undefined) throw new Error(`no #${id} in the failure turn`);
    return node;
  }

  it("keeps the ids the e2e workspace runner polls, around our sentence, landed whole", () => {
    const turn = renderDeep(
      failureTurn({
        message: "Couldn't create alpha. Try again. If this persists, report a bug.",
        details: "",
      }),
    );
    const nodes = collectVnodes(turn);
    expect(nodes.find((node) => attrsOf(node).id === "failure-view")).toBeDefined();
    const message = nodes.find((node) => attrsOf(node).id === "error-message");
    expect(allText(message)).toBe("Couldn't create alpha. Try again. If this persists, report a bug.");
    // An error is read, not followed: streamed a character at a time it reads as the app hanging.
    expect(nodes.filter((node) => String(attrsOf(node).class ?? "").includes("start-char"))).toHaveLength(0);
  });

  it("names the See details toggle and the panel it opens from the failure view the runner polls", () => {
    const draw = drawFailureTurn({
      message: "Couldn't create alpha. Try again. If this persists, report a bug.",
      details: "mngr create failed (exit code 1):\nunknown or invalid runtime name: runsc",
    });
    // The runner clicks `#failure-view-details` and reads the panel it
    // opens; without that panel's id it has the sentence alone, which names
    // no cause.
    (attrsOf(nodeById(draw(), "failure-view-details")).onclick as () => void)();
    expect(allText(nodeById(draw(), "failure-view-details-panel"))).toContain(
      "unknown or invalid runtime name: runsc",
    );
  });
});

/** What the page puts on screen, with its components rendered through. */
function shownText(view: unknown): string {
  return allText(renderDeep(view));
}

/** The element carrying `id` in what the page actually draws. */
function shownById(view: unknown, id: string): unknown {
  return collectVnodes(renderDeep(view)).find(
    (node) => attrsOf(node).id === id,
  );
}

/** The answer buttons a render offers, by their data-answer id. */
function answers(view: unknown): Map<string, () => void> {
  return new Map(
    withAttr(renderDeep(view), "data-answer").map((node) => [
      String(attrsOf(node)["data-answer"]),
      attrsOf(node).onclick as () => void,
    ]),
  );
}

/** The chat error keyed `key`. */
function failureNoticeIn(view: unknown, key: string): AnyVnode | undefined {
  return collectVnodes(view).find(
    (node) => node.tag === ChatError && attrsOf(node).key === key,
  );
}

/** The ways forward the failure's chat error offers, by label, in order. */
function failureActions(view: unknown): Map<string, ErrorAction> {
  const actions = (attrsOf(failureNoticeIn(view, "creation-failure") ?? {}).actions ?? []) as ErrorAction[];
  return new Map(actions.map((action) => [action.label, action]));
}

/** What the page keeps behind the See details of the chat error keyed `key`. */
function failureDetailsIn(view: unknown, key: string): string {
  const error = attrsOf(failureNoticeIn(view, key) ?? {}).error as UserFacingError | undefined;
  return error?.details ?? "";
}

/** The attrs the retry form's embedded create form opens with, once the form is open. */
function retryFormAttrs(view: unknown): Record<string, unknown> | undefined {
  const form = collectVnodes(view).find((node) => attrsOf(node).mode === "embedded");
  return form === undefined ? undefined : attrsOf(form);
}

describe("CreatingPage across a route change", () => {
  afterEach(() => {
    startFlow.reset();
    wash.cancel();
    welcomeChatArrival.cancel();
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
   * otherwise), with every welcome-chat post held open until the returned `settleSeed` or `failSeed` is called. Reduced
   * motion, so the wash covers at once instead of growing from the loading box, which measures the DOM.
   */
  function mountDoneAttempt(
    createAttemptId: string,
    status = "DONE",
    isDriven = false,
  ): {
    page: m.Component;
    entered: string[];
    focusRequests: [string, string][];
    landings: unknown[];
    seedBodies: WelcomeChatBody[];
    logSources: LogSource[];
    settleSeed: () => void;
    failSeed: () => void;
    setStatus: (next: string) => void;
  } {
    vi.useFakeTimers();
    let currentStatus = status;
    const entered: string[] = [];
    const focusRequests: [string, string][] = [];
    const landings: unknown[] = [];
    const logSources: LogSource[] = [];
    const workspaces = { accentEntry: () => undefined, toAgentScopedId: (id: string) => id };
    registerAppContext({
      stores: { workspaces },
      shell: {
        stores: { workspaces },
        enterWorkspace: (id: string) => entered.push(id),
        requestFocusChat: (workspaceId: string, chatId: string) => focusRequests.push([workspaceId, chatId]),
      },
    } as unknown as AppContext);
    vi.stubGlobal("window", {
      matchMedia: () => ({ matches: true }),
      innerWidth: 800,
      innerHeight: 600,
    });
    vi.stubGlobal(
      "EventSource",
      class extends InertEventSource {
        constructor() {
          super();
          logSources.push(this);
        }
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
      if (url.endsWith("/welcome-chat/landing")) {
        landings.push(JSON.parse(init?.body as string));
        return Promise.resolve(new Response(null, { status: 204 }));
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
      focusRequests,
      landings,
      seedBodies,
      logSources,
      settleSeed: () => {
        for (const resolve of seedResolvers)
          resolve(jsonResponse({ chat_id: "agent-seeded" }));
      },
      failSeed: () => {
        for (const resolve of seedResolvers)
          resolve(new Response(JSON.stringify({ error: "no chat app" }), { status: 502 }));
      },
      setStatus: (next: string) => {
        currentStatus = next;
      },
    };
  }

  /** Run a mounted, finished attempt to its entry: the workspace mounted under the cover and the page gone. */
  async function runToEntry(mounted: ReturnType<typeof mountDoneAttempt>): Promise<void> {
    await vi.waitFor(() => expect(mounted.seedBodies).toHaveLength(2));
    await vi.advanceTimersByTimeAsync(FLOW_THINK_MS + streamDurationMs(READY_LINE) + 1);
    expect(mounted.entered).toEqual(["agent-0123abcd"]);
    (mounted.page.onremove as () => void).call(mounted.page);
  }

  it("hands the conversation to the finished attempt's workspace and keeps the cover up until the chat is on screen", async () => {
    const { page, entered, focusRequests, landings, seedBodies, settleSeed } =
      mountDoneAttempt("create-attempt-d");
    const render = (vnode: m.Vnode): m.Vnode =>
      (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);

    // Handed over once the page knows the settings, and again once the attempt is done, which starts the seed.
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

    // The ready line streams first.
    expect(entered).toEqual([]);
    await vi.advanceTimersByTimeAsync(FLOW_THINK_MS + streamDurationMs(READY_LINE) + 1);
    // Then the workspace mounts behind the cover with the seed still in flight, and the page goes away.
    expect(entered).toEqual(["agent-0123abcd"]);
    (page.onremove as () => void).call(page);
    expect(wash.active).not.toBeNull();

    // The seed's answer asks the workspace for the chat, and is not yet the chat on screen.
    settleSeed();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(focusRequests).toEqual([["agent-0123abcd", "agent-seeded"]]);
    expect(wash.active?.isLifting).toBe(false);

    // Only the seeded chat's own report lifts it.
    welcomeChatArrival.noteChatOnScreen("agent-99999999");
    await vi.advanceTimersByTimeAsync(0);
    expect(wash.active?.isLifting).toBe(false);
    welcomeChatArrival.noteChatOnScreen("agent-seeded");
    await vi.advanceTimersByTimeAsync(0);
    expect(wash.active).toBeNull();
    expect(landings).toEqual([{ landing: "ON_SCREEN", waited_ms: expect.any(Number) }]);
  });

  it("lifts the cover at once when the seed definitively fails, and records why", async () => {
    const mounted = mountDoneAttempt("create-attempt-s");
    const { focusRequests, landings, failSeed } = mounted;
    await runToEntry(mounted);

    failSeed();
    await vi.advanceTimersByTimeAsync(0);
    expect(wash.active).toBeNull();
    expect(focusRequests).toEqual([]);
    expect(landings).toEqual([{ landing: "SEED_FAILED", waited_ms: expect.any(Number) }]);
  });

  it("lifts the cover at the bound when the chat never shows, so the user is not stranded", async () => {
    const mounted = mountDoneAttempt("create-attempt-t");
    const { landings, settleSeed } = mounted;
    await runToEntry(mounted);
    settleSeed();

    await vi.advanceTimersByTimeAsync(WELCOME_CHAT_WAIT_BOUND_MS - FLOW_THINK_MS - streamDurationMs(READY_LINE) - 1000);
    expect(wash.active?.isLifting).toBe(false);
    await vi.advanceTimersByTimeAsync(2000);
    expect(wash.active).toBeNull();
    expect(landings).toEqual([{ landing: "TIMED_OUT", waited_ms: expect.any(Number) }]);
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

    // The attempt finishes: the ready line lands under the box, and the workspace is entered with
    // the seed still in flight.
    setStatus("DONE");
    await vi.advanceTimersByTimeAsync(2000);
    expect(seedBodies).toHaveLength(2);
    const done = columnKeys(render());
    expect(done.indexOf("creation-box")).toBeLessThan(done.indexOf("creation-ready"));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(entered).toEqual(["agent-0123abcd"]);
    settleSeed();
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

  it("leaves the cover up when another creating page opens during the wait for the chat", async () => {
    const first = mountDoneAttempt("create-attempt-w");
    await runToEntry(first);
    first.settleSeed();
    await vi.advanceTimersByTimeAsync(0);

    const second = mountDoneAttempt("create-attempt-x", "CREATING");
    await vi.advanceTimersByTimeAsync(0);
    expect(wash.active?.isLifting).toBe(false);
    (second.page.onremove as () => void).call(second.page);
    expect(wash.active?.isLifting).toBe(false);

    welcomeChatArrival.noteChatOnScreen("agent-seeded");
    await vi.advanceTimersByTimeAsync(0);
    expect(wash.active).toBeNull();
  });

  it("does not enter the workspace when the page was left during the ready turn", async () => {
    const { page, entered, landings, seedBodies } = mountDoneAttempt("create-attempt-e");

    await vi.waitFor(() => expect(seedBodies).toHaveLength(2));
    (page.onremove as () => void).call(page);
    await vi.advanceTimersByTimeAsync(WELCOME_CHAT_WAIT_BOUND_MS + 1000);
    expect(entered).toEqual([]);
    expect(wash.active).toBeNull();
    expect(landings).toEqual([]);
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
  type AttemptRecord = NonNullable<CreateAttemptDetail["record"]>;

  function recordWith(createAttemptId: string, record: Partial<AttemptRecord>): CreateAttemptDetail {
    const failed = failedRecord(createAttemptId);
    return { ...failed, record: { ...(failed.record as AttemptRecord), ...record } };
  }

  interface MountedPage {
    page: m.Component;
    render: () => m.Vnode;
  }

  /** Mount the page on `createAttemptId` against whatever `fetch` the test stubbed. */
  function mountPage(createAttemptId: string): MountedPage {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor(createAttemptId));
    const render = (): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnodeFor(createAttemptId));
    (page.oninit as (v: m.VnodeDOM) => void).call(page, vnodeFor(createAttemptId));
    return { page, render };
  }

  /** Register a real shell, whose hub route is where a finished discard leaves for. */
  function registerShell(): ShellState {
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    return shell;
  }

  /** Mount the page on a record-backed attempt, with `fetch` answering every other URL through `route`. */
  async function mountRecord(
    createAttemptId: string,
    detail: CreateAttemptDetail,
    route: (url: string, init?: RequestInit) => Response | Promise<Response> = () => jsonResponse({}),
  ): Promise<MountedPage> {
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) =>
      Promise.resolve(url.startsWith("/ui/api/create/attempts/") ? jsonResponse(detail) : route(url, init)),
    );
    const mounted = mountPage(createAttemptId);
    // The name is in the settings the user turn folds away, so this waits on
    // the markup rather than on what a reader would see.
    await vi.waitFor(() => expect(allText(mounted.render())).toContain(`workspace-${createAttemptId}`));
    return mounted;
  }

  it("tells a failed create in our words and keeps the raw error and its log behind See details", async () => {
    const { render } = await mountRecord(
      "create-attempt-f",
      recordWith("create-attempt-f", {
        error: "mngr create failed (exit code 1):\nTraceback: boom",
        log_tail: ["[Imbue Studio] Creating...", "Traceback: boom"],
      }),
    );
    const view = render();
    const message = shownById(view, "error-message");
    expect(allText(message)).toBe(
      "Couldn't create workspace-create-attempt-f. Try again. If this persists, report a bug.",
    );
    expect(shownText(view)).not.toContain("exit code");
    const details = failureDetailsIn(view, "creation-failure");
    expect(details).toContain("mngr create failed (exit code 1)");
    expect(details).toContain("[Imbue Studio] Creating...");
    // The ways forward are the user's answer row: the way out among its quiet
    // links at the left, the fix as the answer at its right.
    expect([...answers(view).keys()]).toEqual([
      "failure-view-dismissal-0",
      "failure-view-fix-0",
    ]);
    expect([...failureActions(view).keys()]).toEqual(["Retry", "Dismiss"]);
    failureActions(view).get("Retry")?.onclick();
    const retryForm = retryFormAttrs(render());
    expect(retryForm?.retryId).toBe("create-attempt-f");
    expect(retryForm?.initialPreset).toBeUndefined();
    expect(retryForm?.isImbueCloudRefused).toBe(false);
  });

  it("tells a branch the repository lacks by name, with Retry and the clone's output behind See details", async () => {
    const failed = failedRecord("create-attempt-r");
    const record = failed.record as AttemptRecord;
    const { render } = await mountRecord("create-attempt-r", {
      ...failed,
      record: {
        ...record,
        error: "git fetch failed:\nfatal: couldn't find remote ref v9",
        error_kind: "GIT_REF_NOT_FOUND",
        request: { ...(record.request as NonNullable<AttemptRecord["request"]>), branch: "v9" },
      },
    });
    const view = render();
    const message = shownById(view, "error-message");
    expect(allText(message)).toContain("https://example.com/repo has no branch or tag called v9.");
    expect(allText(renderDeep(failureNoticeIn(view, "creation-failure")))).toContain("Report a bug");
    expect(failureDetailsIn(view, "creation-failure")).toContain("couldn't find remote ref v9");
    expect([...failureActions(view).keys()]).toEqual(["Retry", "Dismiss"]);
  });

  it("offers a waitlisted account its own computer instead of a Retry that fails the same way", async () => {
    const { render } = await mountRecord(
      "create-attempt-w",
      recordWith("create-attempt-w", { error: "This account is on the waitlist.", error_kind: "WAITLISTED" }),
    );
    const view = render();
    expect(shownText(view)).toContain("still on the waitlist");
    const offered = failureActions(view);
    expect([...offered.keys()]).toEqual(["Run on this computer", "Dismiss"]);
    offered.get("Run on this computer")?.onclick();
    expect(retryFormAttrs(render())).toMatchObject({
      retryId: "create-attempt-w",
      initialPreset: "local",
      isImbueCloudRefused: true,
    });
  });

  it("leads a plan limit with a run on this computer that keeps Imbue Cloud backups and sharing, Retry behind it", async () => {
    const { render } = await mountRecord(
      "create-attempt-q",
      recordWith("create-attempt-q", {
        error: "Quota exceeded",
        error_kind: "QUOTA_EXCEEDED",
        quota_refusal: { entitlement: "max_remote_workspaces", limit: 3 },
      }),
    );
    const offered = failureActions(render());
    expect([...offered.keys()]).toEqual(["Run on this computer", "Retry", "Dismiss"]);
    offered.get("Run on this computer")?.onclick();
    expect(retryFormAttrs(render())).toMatchObject({ initialPreset: "local", isImbueCloudRefused: false });
  });

  it("offers a version too old for Imbue Cloud an update in the desktop app, and a run on this computer", async () => {
    const shell = new ShellState(createEmptyStores());
    shell.accountId = "user-cd34";
    registerAppContext({ stores: shell.stores, shell });
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await withMindsNative({}, async () => {
      const { render } = await mountRecord(
        "create-attempt-o",
        recordWith("create-attempt-o", { error: "Client too old", error_kind: "CLIENT_TOO_OLD" }),
      );
      const offered = failureActions(render());
      expect([...offered.keys()]).toEqual(["Update Imbue Studio", "Run on this computer", "Dismiss"]);
      offered.get("Update Imbue Studio")?.onclick();
    });
    expect(routed).toHaveBeenCalledWith("/settings", { section: "updates", account: "user-cd34" });
  });

  it("holds Retry on a failed attempt that can't be retried, saying why instead of asking for a retry", async () => {
    const reason = "The connection this attempt used was removed, so it can't be retried.";
    const { render } = await mountRecord(
      "create-attempt-h",
      recordWith("create-attempt-h", { error_kind: "NAME_TAKEN", retry_unavailable_reason: reason }),
    );
    const message = shownById(render(), "error-message");
    expect(allText(message)).toBe(`Couldn't create workspace-create-attempt-h. ${reason}`);
    expect([...failureActions(render()).keys()]).toEqual(["Retry", "Dismiss"]);
    const retry = failureActions(render()).get("Retry");
    expect(retry).toMatchObject({ isDisabled: true, tooltip: reason });
    retry?.onclick();
    expect(retryFormAttrs(render())).toBeUndefined();
    // A held Retry is never the emphasized answer: with nothing pressable in
    // the row, nothing in it is filled.
    expect(
      withAttr(renderDeep(render()), "data-answer").map((node) => [
        attrsOf(node)["data-answer"],
        classesOf(node).includes(BTN_VARIANTS.success) ? "success" : "quiet",
      ]),
    ).toEqual([
      ["failure-view-dismissal-0", "quiet"],
      ["failure-view-fix-0", "quiet"],
    ]);
  });

  it("words an unreachable provider from the fix the provider named", async () => {
    const { render } = await mountRecord(
      "create-attempt-d",
      recordWith("create-attempt-d", {
        error: "Provider 'docker' is not available: Cannot connect to Docker daemon",
        error_kind: "PROVIDER_UNAVAILABLE",
        provider_remediation: "start Docker",
      }),
    );
    const message = shownById(render(), "error-message");
    // The fix is set apart, so it reads as its own run of text.
    expect(allText(message)).toBe(
      "Couldn't create workspace-create-attempt-d. To fix it,  start Docker, then select Retry .",
    );
  });

  it("stays on the page through a discard and leaves only once the discard has finished", async () => {
    vi.useFakeTimers();
    const statuses = [
      { status: "RUNNING", is_done: false },
      { status: "DONE", is_done: true },
    ];
    const requested: string[] = [];
    const { render } = await mountRecord(
      "create-attempt-i",
      recordWith("create-attempt-i", { state: "interrupted" }),
      (url, init) => {
        requested.push(`${init?.method ?? "GET"} ${url}`);
        if (url.endsWith("/discard")) return jsonResponse({ operation_id: "create-attempt-i" }, 202);
        return jsonResponse(statuses.shift());
      },
    );
    const shell = registerShell();
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    answers(render()).get("discard")?.();
    expect(shownText(render())).toContain("Removing what was left of workspace-create-attempt-i…");
    await vi.advanceTimersByTimeAsync(0);
    expect(routed).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);

    expect(routed).toHaveBeenCalledWith(shell.hubRoute(), undefined, undefined);
    expect(requested).toEqual([
      "POST /api/v1/workspaces/create-attempts/create-attempt-i/discard",
      "GET /api/v1/workspaces/operations/create-attempt-discard/create-attempt-i",
      "GET /api/v1/workspaces/operations/create-attempt-discard/create-attempt-i",
    ]);
  });

  it("stops following a discard once the page is gone, so it never routes away from where the user went", async () => {
    vi.useFakeTimers();
    let releaseStatus: (response: Response) => void = () => undefined;
    const requested: string[] = [];
    const { page, render } = await mountRecord(
      "create-attempt-k",
      recordWith("create-attempt-k", { state: "interrupted" }),
      (url, init) => {
        requested.push(`${init?.method ?? "GET"} ${url}`);
        if (url.endsWith("/discard")) return jsonResponse({ operation_id: "create-attempt-k" }, 202);
        return new Promise<Response>((resolve) => {
          releaseStatus = resolve;
        });
      },
    );
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    answers(render()).get("discard")?.();
    await vi.advanceTimersByTimeAsync(0);
    (page.onremove as () => void).call(page);
    releaseStatus(jsonResponse({ status: "RUNNING", is_done: false }));
    await vi.advanceTimersByTimeAsync(5000);

    expect(routed).not.toHaveBeenCalled();
    expect(requested).toEqual([
      "POST /api/v1/workspaces/create-attempts/create-attempt-k/discard",
      "GET /api/v1/workspaces/operations/create-attempt-discard/create-attempt-k",
    ]);
  });

  it("says a discard failed, with its output behind See details, instead of leaving as if it worked", async () => {
    const { render } = await mountRecord(
      "create-attempt-j",
      recordWith("create-attempt-j", { state: "interrupted" }),
      (url) =>
        url.endsWith("/discard")
          ? jsonResponse({ operation_id: "create-attempt-j" }, 202)
          : jsonResponse({ status: "FAILED", is_done: false, log: "mngr destroy: host busy" }),
    );
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    answers(render()).get("discard")?.();
    await vi.waitFor(() =>
      expect(shownText(render())).toContain(
        "Imbue Studio couldn't remove what was left of workspace-create-attempt-j. " +
          "Try again. If this persists, report a bug.",
      ),
    );
    expect(failureDetailsIn(render(), "creation-discard-failure")).toBe("mngr destroy: host busy");
    expect(routed).not.toHaveBeenCalled();
    // Discard stays on offer as the way to try again.
    expect([...answers(render()).keys()]).toEqual(["retry", "discard"]);
  });

  it("emphasizes Discard on an interrupted attempt that can't be retried, and says why instead of offering a retry", async () => {
    const reason = "The connection this attempt used was removed, so it can't be retried.";
    const { render } = await mountRecord(
      "create-attempt-l",
      recordWith("create-attempt-l", { state: "interrupted", retry_unavailable_reason: reason }),
    );
    const view = render();
    expect(shownText(view)).toContain(`${reason} You can discard the partial workspace.`);
    expect(shownText(view)).not.toContain("You can retry");
    // Discard leads the row and carries the emphasis; the held Retry follows
    // it, quieter and unpressable.
    const drawn = withAttr(renderDeep(view), "data-answer").map((node) => [
      attrsOf(node)["data-answer"],
      classesOf(node).includes(BTN_VARIANTS.success) ? "success" : "secondary",
    ]);
    expect(drawn).toEqual([
      ["discard", "success"],
      ["retry", "secondary"],
    ]);
  });

  it("leaves for the workspaces list when the attempt was already discarded elsewhere, rather than failing the discard", async () => {
    const { render } = await mountRecord(
      "create-attempt-g",
      recordWith("create-attempt-g", { state: "interrupted" }),
      () => jsonResponse({ error: "Unknown create attempt create-attempt-g" }, 404),
    );
    const shell = registerShell();
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    answers(render()).get("discard")?.();

    await vi.waitFor(() => expect(routed).toHaveBeenCalledWith(shell.hubRoute(), undefined, undefined));
    expect(shownText(render())).not.toContain("couldn't remove");
  });

  it("keeps the create log of a failure it watched live, behind See details with the raw error", async () => {
    const sources: Array<{ onmessage: ((event: { data: string }) => void) | null }> = [];
    vi.stubGlobal(
      "EventSource",
      class {
        onmessage: ((event: { data: string }) => void) | null = null;
        onerror: unknown = null;
        constructor() {
          sources.push(this);
        }
        close(): void {}
      },
    );
    vi.stubGlobal("fetch", (url: string) =>
      Promise.resolve(
        url.startsWith("/ui/api/create/attempts/")
          ? jsonResponse(liveDetail("create-attempt-l"))
          : jsonResponse({
              status: "FAILED",
              error: "mngr create failed (exit code 1):\nError: Host name already exists: alpha",
              error_kind: "NAME_TAKEN",
            }),
      ),
    );
    const { page, render } = mountPage("create-attempt-l");
    await vi.waitFor(() => expect(sources).toHaveLength(1));
    sources[0].onmessage?.({ data: JSON.stringify({ log: "[Imbue Studio] Creating workspace..." }) });

    await vi.waitFor(() => expect(shownText(render())).toContain("There's already a workspace called"));
    const details = failureDetailsIn(render(), "creation-failure");
    expect(details).toContain("Host name already exists: alpha");
    expect(details).toContain("[Imbue Studio] Creating workspace...");
    (page.onremove as () => void).call(page);
  });
});
