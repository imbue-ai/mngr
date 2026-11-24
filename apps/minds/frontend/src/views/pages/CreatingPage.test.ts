import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import type { CreateAttemptDetail } from "../../models/create";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import { SETTING_UP_LINE, SETTING_UP_LONG_LINE } from "../../models/firstRun";
import { firstRunCover } from "../../models/firstRunCover";
import { WELCOME_CHAT_WAIT_BOUND_MS, welcomeChatArrival } from "../../models/welcomeChatArrival";
import type { AnyVnode } from "../../testing";
import {
  InertEventSource,
  allText,
  attrsOf,
  byId,
  classesOf,
  classTokensOf,
  collectVnodes,
  jsonResponse,
  renderDeep,
  withAttr,
  withMindsNative,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { CreatingPage } from "./CreatingPage";
import { CreateFailure } from "./create/CreateFailure";

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

/** The answers as drawn, in order, each filled (the leading fix) or quiet. */
function drawnAnswers(view: unknown): [unknown, string][] {
  return withAttr(renderDeep(view), "data-answer").map((node) => {
    const classes = classesOf(node);
    const isFilled = classes.includes("first-run-button") && !classes.includes("first-run-button--secondary");
    return [attrsOf(node)["data-answer"], isFilled ? "filled" : "quiet"];
  });
}

/** The failure notice named `id`. */
function failureNoticeIn(view: unknown, id: string): AnyVnode | undefined {
  return collectVnodes(view).find(
    (node) => node.tag === CreateFailure && attrsOf(node).id === id,
  );
}

/** The ways forward the failure notice offers, by label, in order. */
function failureActions(view: unknown): Map<string, ErrorAction> {
  const actions = (attrsOf(failureNoticeIn(view, "failure-view") ?? {}).actions ?? []) as ErrorAction[];
  return new Map(actions.map((action) => [action.label, action]));
}

/** What the page keeps behind the See details of the failure notice named `id`. */
function failureDetailsIn(view: unknown, id: string): string {
  const error = attrsOf(failureNoticeIn(view, id) ?? {}).error as UserFacingError | undefined;
  return error?.details ?? "";
}

/** The attrs the retry form's embedded create form opens with, once the form is open. */
function retryFormAttrs(view: unknown): Record<string, unknown> | undefined {
  const form = collectVnodes(view).find((node) => attrsOf(node).mode === "embedded");
  return form === undefined ? undefined : attrsOf(form);
}

describe("CreatingPage", () => {
  beforeEach(() => {
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
  });

  afterEach(() => {
    firstRunCover.lower();
    welcomeChatArrival.cancel();
    vi.useRealTimers();
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const REQUEST = {
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
  };

  function liveDetail(createAttemptId: string, isFirstWorkspace = false, accountId = ""): CreateAttemptDetail {
    return {
      kind: "live",
      record: null,
      live: {
        workspace_name: `workspace-${createAttemptId}`,
        provider_label: "",
        is_remote: false,
        is_first_workspace: isFirstWorkspace,
        account_id: accountId,
        request: { ...REQUEST, display_name: `workspace-${createAttemptId}` },
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
        is_first_workspace: false,
        account_id: "",
        request: { ...REQUEST, display_name: `workspace-${createAttemptId}` },
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

  /** The op-log stream as the page opens it: its `onmessage` is where a log line arrives. */
  interface LogSource {
    onmessage: unknown;
  }

  const GLEB = { user_id: "user-1", email: "gleb@example.com", display_name: "Gleb Denisov" };

  /**
   * A live attempt under Gleb's account whose status polls answer `status`, with every welcome-chat post held
   * open until the returned `settleSeed` or `failSeed` is called.
   */
  function mountLiveAttempt(
    createAttemptId: string,
    status: string,
    isFirstWorkspace = true,
  ): {
    page: m.Component;
    render: () => m.Vnode;
    entered: string[];
    focusRequests: [string, string][];
    landings: unknown[];
    seedPosts: (RequestInit | undefined)[];
    logSources: LogSource[];
    settleSeed: () => void;
    failSeed: () => void;
  } {
    vi.useFakeTimers();
    const entered: string[] = [];
    const focusRequests: [string, string][] = [];
    const landings: unknown[] = [];
    const logSources: LogSource[] = [];
    const workspaces = { toAgentScopedId: (id: string) => id };
    const stores = { workspaces, accounts: { accounts: [GLEB] } };
    registerAppContext({
      stores,
      shell: {
        stores,
        enterWorkspace: (id: string) => entered.push(id),
        requestFocusChat: (workspaceId: string, chatId: string) => focusRequests.push([workspaceId, chatId]),
      },
    } as unknown as AppContext);
    vi.stubGlobal(
      "EventSource",
      class extends InertEventSource {
        constructor() {
          super();
          logSources.push(this);
        }
      },
    );
    const seedPosts: (RequestInit | undefined)[] = [];
    const seedResolvers: ((response: Response) => void)[] = [];
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (url.endsWith("/welcome-chat")) {
        seedPosts.push(init);
        return new Promise<Response>((resolve) => {
          seedResolvers.push(resolve);
        });
      }
      if (url.endsWith("/welcome-chat/landing")) {
        landings.push(JSON.parse(init?.body as string));
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (url.startsWith("/ui/api/create/attempts/"))
        return Promise.resolve(jsonResponse(liveDetail(createAttemptId, isFirstWorkspace, GLEB.user_id)));
      return Promise.resolve(
        jsonResponse({
          status,
          redirect_url: status === "DONE" ? "/goto/agent-0123abcd/" : "",
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
      render: () => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnodeFor(createAttemptId)),
      entered,
      focusRequests,
      landings,
      seedPosts,
      logSources,
      settleSeed: () => {
        for (const resolve of seedResolvers)
          resolve(jsonResponse({ chat_id: "agent-seeded" }));
      },
      failSeed: () => {
        for (const resolve of seedResolvers)
          resolve(new Response(JSON.stringify({ error: "no chat app" }), { status: 502 }));
      },
    };
  }

  /** Run a mounted, finished attempt to its entry: seeded, the workspace mounted under the cover, the page gone. */
  async function runToEntry(mounted: ReturnType<typeof mountLiveAttempt>): Promise<void> {
    await vi.waitFor(() => expect(mounted.seedPosts).toHaveLength(2));
    mounted.settleSeed();
    await vi.advanceTimersByTimeAsync(1);
    expect(mounted.entered).toEqual(["agent-0123abcd"]);
    (mounted.page.onremove as () => void).call(mounted.page);
  }

  it("welcomes the owner of a first workspace with its progress alone, and asks for the seed with no body", async () => {
    const { page, render, seedPosts } = mountLiveAttempt("create-attempt-w", "CREATING_WORKSPACE");
    await vi.waitFor(() => expect(seedPosts).toHaveLength(1));

    const building = renderDeep(render());
    expect(attrsOf(building as AnyVnode)["data-first-run"]).toBe("true");
    expect(attrsOf(building as AnyVnode)["data-agent-id"]).toBe("create-attempt-w");
    expect(classTokensOf(building as AnyVnode)).toContain("light");
    expect(allText(byId(building, "creating-welcome"))).toBe("Welcome, Gleb!");
    expect(allText(byId(building, "creating-setting-up"))).toBe(SETTING_UP_LINE);
    expect(byId(building, "creating-welcome-bar")).toBeDefined();
    // Nothing to pick and no Continue while it builds: the one control is the log's toggle.
    const buttons = collectVnodes(building).filter((node) => node.tag === "button");
    expect(buttons.map((button) => allText(button))).toEqual(["Show details"]);
    expect(collectVnodes(building).filter((node) => node.tag === "input")).toEqual([]);
    // The seed's body is the desktop client's, so the ask carries none.
    expect(seedPosts[0]?.method).toBe("POST");
    expect(seedPosts[0]?.body).toBeUndefined();
    (page.onremove as () => void).call(page);
  });

  it("names a later workspace in its heading, over the bar alone", async () => {
    const { page, render, seedPosts } = mountLiveAttempt("create-attempt-l", "CREATING_WORKSPACE", false);
    await vi.waitFor(() => expect(seedPosts).toHaveLength(1));

    const building = renderDeep(render());
    expect(allText(byId(building, "creating-welcome"))).toBe("Setting up workspace-create-attempt-l");
    expect(byId(building, "creating-setting-up")).toBeUndefined();
    expect(byId(building, "creating-welcome-bar")).toBeDefined();
    (page.onremove as () => void).call(page);
  });

  it("draws the same page when the same attempt is mounted again, as after a reload", async () => {
    const first = mountLiveAttempt("create-attempt-r", "CREATING_WORKSPACE");
    await vi.waitFor(() => expect(first.seedPosts).toHaveLength(1));
    const firstText = shownText(first.render());
    (first.page.onremove as () => void).call(first.page);

    const second = mountLiveAttempt("create-attempt-r", "CREATING_WORKSPACE");
    await vi.waitFor(() => expect(second.seedPosts).toHaveLength(1));
    expect(shownText(second.render())).toBe(firstText);
    expect(firstText).toContain("Welcome, Gleb!");
    (second.page.onremove as () => void).call(second.page);
  });

  it("enters under the cover only once the workspace has its seed, and offers Continue when its chat is on screen", async () => {
    const { page, render, entered, focusRequests, landings, seedPosts, settleSeed } = mountLiveAttempt(
      "create-attempt-d",
      "DONE",
    );
    // Asked once the page knew the attempt was live, and again once it was done.
    await vi.waitFor(() => expect(seedPosts).toHaveLength(2));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(byId(renderDeep(render()), "creating-continue")).toBeUndefined();
    expect(entered).toEqual([]);
    expect(firstRunCover.state).toBeNull();

    // The seed is what tells the workspace to play its arrival, so the workspace is entered only after it.
    settleSeed();
    await vi.advanceTimersByTimeAsync(1);
    expect(entered).toEqual(["agent-0123abcd"]);
    expect(focusRequests).toEqual([["agent-0123abcd", "agent-seeded"]]);
    expect(firstRunCover.state).toEqual({
      createAttemptId: "create-attempt-d",
      heading: "Welcome, Gleb!",
      isReady: false,
    });
    (page.onremove as () => void).call(page);

    // Only the seeded chat's own report makes the cover ready.
    welcomeChatArrival.noteChatOnScreen("agent-99999999");
    await vi.advanceTimersByTimeAsync(0);
    expect(firstRunCover.state?.isReady).toBe(false);
    welcomeChatArrival.noteChatOnScreen("agent-seeded");
    await vi.advanceTimersByTimeAsync(0);
    expect(firstRunCover.state?.isReady).toBe(true);
    expect(landings).toEqual([{ landing: "ON_SCREEN", waited_ms: expect.any(Number) }]);
  });

  it("offers Continue at once when the seed definitively fails, and records why", async () => {
    const { page, entered, focusRequests, landings, seedPosts, failSeed } = mountLiveAttempt(
      "create-attempt-s",
      "DONE",
    );
    await vi.waitFor(() => expect(seedPosts).toHaveLength(2));

    failSeed();
    await vi.advanceTimersByTimeAsync(1);
    expect(entered).toEqual(["agent-0123abcd"]);
    expect(firstRunCover.state?.isReady).toBe(true);
    expect(focusRequests).toEqual([]);
    expect(landings).toEqual([{ landing: "SEED_FAILED", waited_ms: expect.any(Number) }]);
    (page.onremove as () => void).call(page);
  });

  it("offers Continue once the provider chooser is on screen", async () => {
    const mounted = mountLiveAttempt("create-attempt-p", "DONE");
    await runToEntry(mounted);
    expect(firstRunCover.state?.isReady).toBe(false);

    welcomeChatArrival.noteProviderChooserOnScreen();
    await vi.advanceTimersByTimeAsync(0);
    expect(firstRunCover.state?.isReady).toBe(true);
    expect(mounted.landings).toEqual([{ landing: "PROVIDER_CHOOSER_ON_SCREEN", waited_ms: expect.any(Number) }]);
  });

  it("offers Continue at the bound when the chat never shows, so the user is not stranded", async () => {
    const mounted = mountLiveAttempt("create-attempt-t", "DONE");
    await runToEntry(mounted);

    await vi.advanceTimersByTimeAsync(WELCOME_CHAT_WAIT_BOUND_MS - 1000);
    expect(firstRunCover.state?.isReady).toBe(false);
    await vi.advanceTimersByTimeAsync(2000);
    expect(firstRunCover.state?.isReady).toBe(true);
    expect(mounted.landings).toEqual([{ landing: "TIMED_OUT", waited_ms: expect.any(Number) }]);
  });

  it("asks for the seed while the workspace is still being built, so leaving the page cannot lose it", async () => {
    const { page, entered, seedPosts } = mountLiveAttempt("create-attempt-f", "CREATING_WORKSPACE");

    await vi.waitFor(() => expect(seedPosts).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(30_000);
    (page.onremove as () => void).call(page);
    expect(seedPosts).toHaveLength(1);
    expect(entered).toEqual([]);
    expect(firstRunCover.state).toBeNull();
  });

  it("keeps the log's end in view as lines arrive, but not once the reader has scrolled up", async () => {
    const { page, render, logSources } = mountLiveAttempt("create-attempt-n", "CREATING_WORKSPACE");
    await vi.waitFor(() => expect(logSources).toHaveLength(1));
    const feedLine = (line: string): void =>
      (logSources[0].onmessage as (event: { data: string }) => void)({ data: JSON.stringify({ log: line }) });
    const showDetails = collectVnodes(renderDeep(render())).find(
      (node) => node.tag === "button" && allText(node) === "Show details",
    ) as AnyVnode;
    (attrsOf(showDetails).onclick as () => void)();

    // The <pre> only reads and moves its own scroll position, so a bare object stands in for it.
    const element = { scrollTop: 0, scrollHeight: 0, clientHeight: 100 };
    const logHooks = (): {
      oncreate: (vnode: { dom: unknown }) => void;
      onupdate: (vnode: { dom: unknown }) => void;
      onscroll: (event: { target: unknown }) => void;
    } => attrsOf(byId(renderDeep(render()), "logs") as AnyVnode) as ReturnType<typeof logHooks>;

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
    const details = failureDetailsIn(view, "failure-view");
    expect(details).toContain("mngr create failed (exit code 1)");
    expect(details).toContain("[Imbue Studio] Creating...");
    // The way out stands among the quiet links at the row's left, the fix as the button at its right.
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
    expect(allText(renderDeep(failureNoticeIn(view, "failure-view")))).toContain("Report a bug");
    expect(failureDetailsIn(view, "failure-view")).toContain("couldn't find remote ref v9");
    expect([...failureActions(view).keys()]).toEqual(["Retry", "Dismiss"]);
  });

  it("offers a waitlisted account its own computer, and a Retry on Imbue Cloud for once it accepts its invite", async () => {
    const { render } = await mountRecord(
      "create-attempt-w",
      recordWith("create-attempt-w", { error: "This account is on the waitlist.", error_kind: "WAITLISTED" }),
    );
    const view = render();
    expect(shownText(view)).toContain("still on the waitlist");
    const offered = failureActions(view);
    expect([...offered.keys()]).toEqual(["Run on this computer", "Retry", "Dismiss"]);
    offered.get("Run on this computer")?.onclick();
    expect(retryFormAttrs(render())).toMatchObject({
      retryId: "create-attempt-w",
      initialPreset: "local",
      isImbueCloudRefused: true,
    });
    offered.get("Retry")?.onclick();
    const retryForm = retryFormAttrs(render());
    expect(retryForm?.retryId).toBe("create-attempt-w");
    expect(retryForm?.initialPreset).toBeUndefined();
    expect(retryForm?.isImbueCloudRefused).toBe(false);
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
    expect(routed).toHaveBeenCalledWith(
      "/settings",
      { tab: "studio", section: "updates", account: "user-cd34" },
      undefined,
    );
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
    // A held Retry is never the filled button: with nothing pressable in
    // the row, nothing in it is filled.
    expect(drawnAnswers(render())).toEqual([
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
    expect(failureDetailsIn(render(), "discard-failed")).toBe("mngr destroy: host busy");
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
    // Discard leads the row and is the filled button; the held Retry follows
    // it, quieter and unpressable.
    expect(drawnAnswers(view)).toEqual([
      ["discard", "filled"],
      ["retry", "quiet"],
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
    const details = failureDetailsIn(render(), "failure-view");
    expect(details).toContain("Host name already exists: alpha");
    expect(details).toContain("[Imbue Studio] Creating workspace...");
    (page.onremove as () => void).call(page);
  });

  it("opens the create form from a first workspace's failed create, for Retry and for Run on this computer", async () => {
    vi.stubGlobal("EventSource", InertEventSource);
    vi.stubGlobal("fetch", (url: string) =>
      Promise.resolve(
        url.startsWith("/ui/api/create/attempts/")
          ? jsonResponse(liveDetail("create-attempt-u", true))
          : jsonResponse({ status: "FAILED", error: "This account is on the waitlist.", error_kind: "WAITLISTED" }),
      ),
    );
    const { page, render } = mountPage("create-attempt-u");
    await vi.waitFor(() => expect(shownText(render())).toContain("still on the waitlist"));
    expect(allText(shownById(render(), "creating-welcome"))).toBe("Welcome!");

    failureActions(render()).get("Run on this computer")?.onclick();
    expect(retryFormAttrs(render())).toMatchObject({
      retryId: "create-attempt-u",
      initialPreset: "local",
      isImbueCloudRefused: true,
    });
    failureActions(render()).get("Retry")?.onclick();
    expect(retryFormAttrs(render())).toMatchObject({ retryId: "create-attempt-u", isImbueCloudRefused: false });
    expect(retryFormAttrs(render())?.initialPreset).toBeUndefined();
    (page.onremove as () => void).call(page);
  });

  /** Mount a live attempt whose build says it has minutes left, and wait for the page to show that estimate. */
  async function mountLongBuild(createAttemptId: string, isFirstWorkspace: boolean): Promise<MountedPage> {
    const progress = {
      percent: 10,
      percent_cap: 50,
      percent_per_second: 0,
      remaining_text: "About 4 minutes left",
      is_over_a_minute_left: true,
      current_label: "",
      steps: [],
    };
    vi.stubGlobal("EventSource", InertEventSource);
    vi.stubGlobal("fetch", (url: string) => {
      if (url.endsWith("/welcome-chat")) return Promise.resolve(jsonResponse({ chat_id: "" }, 202));
      if (url.startsWith("/ui/api/create/attempts/"))
        return Promise.resolve(jsonResponse(liveDetail(createAttemptId, isFirstWorkspace)));
      return Promise.resolve(jsonResponse({ status: "CREATING_WORKSPACE", progress }));
    });
    const mounted = mountPage(createAttemptId);
    await vi.waitFor(() =>
      expect(allText(shownById(mounted.render(), "creating-remaining"))).toBe("About 4 minutes left"),
    );
    return mounted;
  }

  it("says plainly that a first workspace is being set up once the build has minutes left", async () => {
    const { page, render } = await mountLongBuild("create-attempt-m", true);
    expect(allText(shownById(render(), "creating-setting-up"))).toBe(SETTING_UP_LONG_LINE);
    (page.onremove as () => void).call(page);
  });

  it("stands a later workspace's estimate under the bar with no line over it", async () => {
    const { page, render } = await mountLongBuild("create-attempt-n", false);
    expect(shownById(render(), "creating-setting-up")).toBeUndefined();
    (page.onremove as () => void).call(page);
  });

  it("welcomes the owner of a first workspace whose attempt is read back from its record", async () => {
    const stores = { accounts: { accounts: [GLEB] } };
    registerAppContext({ stores, shell: { stores } } as unknown as AppContext);
    const detail = recordWith("create-attempt-h", { is_first_workspace: true, account_id: GLEB.user_id });
    vi.stubGlobal("fetch", () => Promise.resolve(jsonResponse(detail)));
    const { render } = mountPage("create-attempt-h");
    await vi.waitFor(() => expect(allText(shownById(render(), "creating-welcome"))).toBe("Welcome, Gleb!"));
    expect(allText(shownById(render(), "error-message"))).toContain("Couldn't create workspace-create-attempt-h.");
  });

  it("deletes a dismissed attempt and leaves for the home page", async () => {
    const requested: string[] = [];
    const { render } = await mountRecord("create-attempt-x", failedRecord("create-attempt-x"), (url, init) => {
      requested.push(`${init?.method ?? "GET"} ${url}`);
      return new Response(null, { status: 204 });
    });
    const shell = registerShell();
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    failureActions(render()).get("Dismiss")?.onclick();

    await vi.waitFor(() => expect(routed).toHaveBeenCalledWith(shell.hubRoute(), undefined, undefined));
    expect(requested).toEqual(["DELETE /api/v1/workspaces/create-attempts/create-attempt-x"]);
  });

  it("routes a submitted retry to the new attempt's creation page and closes the form", async () => {
    const { render } = await mountRecord("create-attempt-y", failedRecord("create-attempt-y"));
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    failureActions(render()).get("Retry")?.onclick();

    (retryFormAttrs(render())?.onSubmitted as (operationId: string) => void)("create-attempt-z");

    expect(routed).toHaveBeenCalledWith("/creating/create-attempt-z");
    expect(retryFormAttrs(render())).toBeUndefined();
  });

  it("neither enters the workspace nor raises the cover when the page is left before the seed answers", async () => {
    const { page, entered, seedPosts, settleSeed } = mountLiveAttempt("create-attempt-g", "DONE");
    await vi.waitFor(() => expect(seedPosts).toHaveLength(2));
    (page.onremove as () => void).call(page);

    settleSeed();
    await vi.advanceTimersByTimeAsync(1);

    expect(entered).toEqual([]);
    expect(firstRunCover.state).toBeNull();
  });
});
