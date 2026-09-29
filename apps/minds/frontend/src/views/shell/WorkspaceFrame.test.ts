import { afterEach, describe, expect, it, vi } from "vitest";
import type { PermissionResolutionEntry } from "./WorkspaceFrame";
import {
  WORKSPACE_ORIGIN_FAMILY,
  buildEmbedHandlers,
  fetchPermissionResolutionEntries,
  pushResolutionSnapshot,
  requestIdFromMessage,
} from "./WorkspaceFrame";

describe("WORKSPACE_ORIGIN_FAMILY", () => {
  it("accepts the canonical agent-keyed content origins", () => {
    expect(
      WORKSPACE_ORIGIN_FAMILY.test(
        "agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost",
      ),
    ).toBe(true);
    expect(
      WORKSPACE_ORIGIN_FAMILY.test(
        "system_interface-x1y2.agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost",
      ),
    ).toBe(true);
  });

  it("still accepts a legacy host-keyed origin awaiting the redirect heal", () => {
    expect(
      WORKSPACE_ORIGIN_FAMILY.test(
        "host-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost",
      ),
    ).toBe(true);
  });

  it("rejects origins outside the workspace families", () => {
    expect(WORKSPACE_ORIGIN_FAMILY.test("localhost")).toBe(false);
    expect(WORKSPACE_ORIGIN_FAMILY.test("evil-agent-abc.example.com")).toBe(
      false,
    );
    expect(
      WORKSPACE_ORIGIN_FAMILY.test("agent-abc.localhost.example.com"),
    ).toBe(false);
  });
});

// The live pattern from the embed contract module (served by Flask at
// /_static/embed_contract.js, so it cannot be imported into the bundle):
// apps/minds/imbue/minds/desktop_client/static/embed_contract.js.
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

describe("requestIdFromMessage", () => {
  it("keeps the request the workspace named", () => {
    expect(
      requestIdFromMessage({ requestId: "evt-9f2c41" }, REQUEST_ID_PATTERN),
    ).toBe("evt-9f2c41");
  });

  it("names no request when the id is missing or off-shape", () => {
    expect(requestIdFromMessage({}, REQUEST_ID_PATTERN)).toBeNull();
    expect(
      requestIdFromMessage({ requestId: 42 }, REQUEST_ID_PATTERN),
    ).toBeNull();
    expect(
      requestIdFromMessage({ requestId: "" }, REQUEST_ID_PATTERN),
    ).toBeNull();
    // Path / query / whitespace characters must never reach the selection.
    expect(
      requestIdFromMessage({ requestId: "evt-1/../admin" }, REQUEST_ID_PATTERN),
    ).toBeNull();
    expect(
      requestIdFromMessage({ requestId: "evt-1?x=1" }, REQUEST_ID_PATTERN),
    ).toBeNull();
    expect(
      requestIdFromMessage({ requestId: "evt-1 evt-2" }, REQUEST_ID_PATTERN),
    ).toBeNull();
    expect(
      requestIdFromMessage({ requestId: "a".repeat(129) }, REQUEST_ID_PATTERN),
    ).toBeNull();
  });
});

// A stand-in for the Flask-served contract module: only the message-type
// constants and the id pattern matter to the handler map.
function makeContract() {
  return {
    OPEN_REQUEST_MODAL: "minds:open-request-modal",
    OPEN_HELP: "minds:open-help",
    PROVIDER_SIGN_IN: "minds:provider-sign-in",
    PROVIDER_SIGN_IN_ACK: "minds:provider-sign-in-ack",
    PROVIDER_SIGN_IN_END: "minds:provider-sign-in-end",
    BRING_APP_TO_FRONT: "minds:bring-app-to-front",
    OPEN_SHARE_SETTINGS: "minds:open-share-settings",
    CLOSE_ACTIVE_TAB: "minds:close-active-tab",
    PERMISSION_RESOLUTIONS: "minds:permission-resolutions",
    WORKSPACE_READY: "minds:workspace-ready",
    POP_OUT_WINDOW: "minds:pop-out-window",
    WINDOW_DRAG_STARTED: "minds:window-drag-started",
    WINDOW_DRAG_ENDED: "minds:window-drag-ended",
    DETACHED_WINDOWS: "minds:detached-windows",
    EMBEDDER_CAPABILITIES: "minds:embedder-capabilities",
    REATTACH_WINDOW: "minds:reattach-window",
    TEAR_OUT: "minds:tear-out",
    REQUEST_ID_PATTERN,
  } as Parameters<typeof buildEmbedHandlers>[0]["contract"];
}

const WORKSPACE_AGENT_ID = "agent-ab12";

function makeHandlers(options: { canPopOut?: boolean; isRelaying?: boolean } = {}) {
  const contract = makeContract();
  const navigations: { path: string; params?: Record<string, string> }[] = [];
  const popupOpens: (string | null)[] = [];
  const acks: string[] = [];
  const ackPayloads: (Record<string, unknown> | undefined)[] = [];
  const sent: { type: string; payload?: Record<string, unknown> }[] = [];
  const relayArms: { flowId: string; url: string }[] = [];
  const relayStops: string[] = [];
  const popoutCalls: unknown[] = [];
  let frontCount = 0;
  let readyCount = 0;
  const handlers = buildEmbedHandlers({
    contract,
    navigate: (path, params) => navigations.push({ path, params }),
    sendAck: (type, payload) => {
      acks.push(type);
      ackPayloads.push(payload);
      sent.push({ type, payload });
    },
    bringAppToFront: () => {
      frontCount += 1;
    },
    armProviderRelay: (flowId, url) => {
      relayArms.push({ flowId, url });
      return Promise.resolve(options.isRelaying ?? true);
    },
    stopProviderRelay: (flowId) => {
      relayStops.push(flowId);
    },
    workspaceAgentId: () => WORKSPACE_AGENT_ID,
    openRequestPopup: (requestId) => popupOpens.push(requestId),
    onWorkspaceReady: () => {
      readyCount += 1;
    },
    popout:
      options.canPopOut === true
        ? {
            open: (request) => popoutCalls.push(["open", request]),
            beginDrag: (request) => popoutCalls.push(["drag", request]),
            endDrag: (workspaceId, windowId, isDetached) => popoutCalls.push(["ended", workspaceId, windowId, isDetached]),
            detachedWindows: (windows) => popoutCalls.push(["detached", windows]),
          }
        : null,
  });
  return {
    contract,
    handlers,
    navigations,
    popupOpens,
    acks,
    ackPayloads,
    sent,
    relayArms,
    relayStops,
    popoutCalls,
    frontCount: () => frontCount,
    readyCount: () => readyCount,
  };
}

describe("buildEmbedHandlers", () => {
  it("reports a workspace's readiness announcement, which is what releases a held chat ask", () => {
    const { contract, handlers, readyCount, navigations } = makeHandlers();
    handlers[contract.WORKSPACE_READY]({});
    expect(readyCount()).toBe(1);
    expect(navigations).toEqual([]);
  });

  it("answers a readiness announcement with what this chrome can do", () => {
    // The workspace's pull-out gesture turns on only where a desktop window
    // can be made; a plain browser says so and the gesture stays off.
    const browser = makeHandlers();
    browser.handlers[browser.contract.WORKSPACE_READY]({});
    expect(browser.acks).toEqual([browser.contract.EMBEDDER_CAPABILITIES]);
    expect(browser.ackPayloads).toEqual([{ canPopOut: false }]);
    const desktop = makeHandlers({ canPopOut: true });
    desktop.handlers[desktop.contract.WORKSPACE_READY]({});
    expect(desktop.ackPayloads).toEqual([{ canPopOut: true }]);
  });

  it("hands the pull-out asks to main with the mounted workspace's id, and ignores them where nothing can pop out", () => {
    const desktop = makeHandlers({ canPopOut: true });
    const { contract, handlers, popoutCalls } = desktop;
    const size = { windowId: "win-0123", title: "Notes", width: 640, height: 480 };
    handlers[contract.POP_OUT_WINDOW]({ ...size, extra: "dropped" });
    handlers[contract.WINDOW_DRAG_STARTED]({ ...size, grabX: 12, grabY: 8, extra: "dropped" });
    handlers[contract.WINDOW_DRAG_ENDED]({ windowId: "win-0123", isDetached: true });
    handlers[contract.WINDOW_DRAG_ENDED]({ windowId: "win-0123", isDetached: false });
    const request = { workspaceId: WORKSPACE_AGENT_ID, ...size };
    expect(popoutCalls).toEqual([
      ["open", request],
      ["drag", { ...request, grabX: 12, grabY: 8 }],
      ["ended", WORKSPACE_AGENT_ID, "win-0123", true],
      ["ended", WORKSPACE_AGENT_ID, "win-0123", false],
    ]);
    const browser = makeHandlers();
    expect(browser.handlers[browser.contract.POP_OUT_WINDOW]).toBeUndefined();
    expect(browser.handlers[browser.contract.WINDOW_DRAG_STARTED]).toBeUndefined();
    expect(browser.handlers[browser.contract.WINDOW_DRAG_ENDED]).toBeUndefined();
    expect(browser.handlers[browser.contract.DETACHED_WINDOWS]).toBeUndefined();
  });

  it("reads the detached set as window ids with their titles, dropping off-shape entries", () => {
    const { contract, handlers, popoutCalls } = makeHandlers({ canPopOut: true });
    handlers[contract.DETACHED_WINDOWS]({
      windows: [{ windowId: "win-1", title: "A" }, { windowId: "win-2" }, "junk", { title: "no id" }],
    });
    expect(popoutCalls).toEqual([
      [
        "detached",
        [
          { windowId: "win-1", title: "A" },
          { windowId: "win-2", title: "" },
        ],
      ],
    ]);
  });

  it("opens the review popup on the request the workspace asked to review", () => {
    // The chat card's "Review & respond" must land on THAT request, not on
    // whatever else happens to be pending. Opening the popup is the shell's
    // own navigation (it floats over this workspace, kept mounted), so nothing
    // here routes the base layer away.
    const { contract, handlers, popupOpens, navigations } = makeHandlers();
    handlers[contract.OPEN_REQUEST_MODAL]({ requestId: "evt-9f2c41" });
    expect(popupOpens).toEqual(["evt-9f2c41"]);
    expect(navigations).toEqual([]);
  });

  it("opens the popup on nothing in particular when the id is off-shape", () => {
    const { contract, handlers, popupOpens } = makeHandlers();
    handlers[contract.OPEN_REQUEST_MODAL]({ requestId: "evt-1/../admin" });
    expect(popupOpens).toEqual([null]);
  });

  it("arms the relay for a sign-in and acks whether it is relaying, only once the relay answered", async () => {
    const { contract, handlers, relayArms, sent, navigations } =
      makeHandlers();
    const url = "https://claude.ai/oauth/authorize?state=s-1";
    handlers[contract.PROVIDER_SIGN_IN]({ url, flowId: "flow-7c" });
    expect(relayArms).toEqual([{ flowId: "flow-7c", url }]);
    await Promise.resolve();
    expect(sent).toEqual([
      { type: contract.PROVIDER_SIGN_IN_ACK, payload: { relay: true } },
    ]);
    expect(navigations).toEqual([]);
  });

  it("acks a sign-in the desktop cannot relay, so the workspace falls back at once", async () => {
    const { contract, handlers, sent } = makeHandlers({ isRelaying: false });
    handlers[contract.PROVIDER_SIGN_IN]({
      url: "https://claude.ai/oauth/authorize",
      flowId: "flow-8d",
    });
    await Promise.resolve();
    expect(sent).toEqual([
      { type: contract.PROVIDER_SIGN_IN_ACK, payload: { relay: false } },
    ]);
  });

  it("stops the relay for a sign-in the workspace says has ended", () => {
    const { contract, handlers, relayStops, sent } = makeHandlers();
    handlers[contract.PROVIDER_SIGN_IN_END]({ flowId: "flow-9e" });
    expect(relayStops).toEqual(["flow-9e"]);
    expect(sent).toEqual([]);
  });

  it("ignores the retired AI-keys page ask", () => {
    const { handlers, navigations, acks } = makeHandlers();
    expect(handlers["minds:open-ai-keys-page"]).toBeUndefined();
    expect(navigations).toEqual([]);
    expect(acks).toEqual([]);
  });

  it("floats the Share tab over this workspace, focused on the asking app", () => {
    // No ack: with no Imbue Studio chrome present the Share click is simply a no-op.
    const { contract, handlers, navigations, acks } = makeHandlers();
    handlers[contract.OPEN_SHARE_SETTINGS]({ serviceName: "web" });
    expect(navigations).toEqual([
      {
        path: `/workspace/${WORKSPACE_AGENT_ID}/options`,
        params: { tab: "share", target: "web" },
      },
    ]);
    expect(acks).toEqual([]);
  });

  it("lands the Share tab untargeted when the name is absent", () => {
    // Unreachable through the real endpoint (the validator requires
    // serviceName); pins the handler's own tolerance.
    const { contract, handlers, navigations } = makeHandlers();
    handlers[contract.OPEN_SHARE_SETTINGS]({});
    expect(navigations).toEqual([
      {
        path: `/workspace/${WORKSPACE_AGENT_ID}/options`,
        params: { tab: "share" },
      },
    ]);
  });

  it("floats help over this workspace, without opening the popup", () => {
    const { contract, handlers, navigations, popupOpens, frontCount } =
      makeHandlers();
    handlers[contract.OPEN_HELP]({});
    handlers[contract.BRING_APP_TO_FRONT]({});
    expect(navigations).toEqual([
      { path: "/help", params: { workspace: WORKSPACE_AGENT_ID } },
    ]);
    expect(popupOpens).toEqual([]);
    expect(frontCount()).toBe(1);
  });
});

describe("fetchPermissionResolutionEntries", () => {
  afterEach(() => {
    // restoreAllMocks does NOT undo vi.stubGlobal; only this does.
    vi.unstubAllGlobals();
  });

  it("maps the wire shape onto entries, drops off-shape rows, and reports failures as null", async () => {
    // Null (not an empty array) on failure: an empty answer would wrongly
    // tell the workspace "asked and none are resolved".
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        requested.push(url);
        return {
          ok: true,
          json: async () => ({
            resolutions: [
              { request_id: "evt-1", resolution: "granted" },
              { request_id: "evt-2", resolution: "shredded" },
              { resolution: "denied" },
              { request_id: "evt-3", resolution: "denied" },
            ],
          }),
        } as unknown as Response;
      }),
    );
    expect(await fetchPermissionResolutionEntries("agent-ab12")).toEqual([
      { requestId: "evt-1", resolution: "granted" },
      { requestId: "evt-3", resolution: "denied" },
    ]);
    expect(requested).toEqual([
      "/ui/api/inbox/resolutions?workspace=agent-ab12",
    ]);

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500 }) as unknown as Response),
    );
    expect(await fetchPermissionResolutionEntries("agent-ab12")).toBeNull();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          ({ ok: true, json: async () => ({}) }) as unknown as Response,
      ),
    );
    expect(await fetchPermissionResolutionEntries("agent-ab12")).toBeNull();
  });
});

describe("pushResolutionSnapshot", () => {
  it("pushes the workspace's verdicts into the frame, and nothing on a failed or empty lookup", async () => {
    // The snapshot is what keeps a rebuilt page from offering Approve/Deny
    // for an already-decided request; a failed lookup must stay silent (the
    // cards then follow the transcript's own resolution notices), and an
    // empty snapshot sends nothing -- there is nothing to flip.
    const entries: PermissionResolutionEntry[] = [
      { requestId: "evt-1", resolution: "granted" },
    ];
    const sends: PermissionResolutionEntry[][] = [];
    await pushResolutionSnapshot(
      async (ws) => (ws === "agent-ab12" ? entries : null),
      (e) => sends.push(e),
      "agent-ab12",
    );
    await pushResolutionSnapshot(
      async () => null,
      (e) => sends.push(e),
      "agent-ab12",
    );
    await pushResolutionSnapshot(
      async () => [],
      (e) => sends.push(e),
      "agent-ab12",
    );
    expect(sends).toEqual([entries]);
  });
});
