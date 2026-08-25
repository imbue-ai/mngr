import { describe, expect, it } from "vitest";
import {
  WORKSPACE_ORIGIN_FAMILY,
  buildEmbedHandlers,
  requestIdFromMessage,
} from "./WorkspaceFrame";

describe("WORKSPACE_ORIGIN_FAMILY", () => {
  it("accepts the canonical agent-keyed content origins", () => {
    expect(
      WORKSPACE_ORIGIN_FAMILY.test("agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost"),
    ).toBe(true);
    expect(
      WORKSPACE_ORIGIN_FAMILY.test(
        "system_interface-x1y2.agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost",
      ),
    ).toBe(true);
  });

  it("still accepts a legacy host-keyed origin awaiting the redirect heal", () => {
    expect(
      WORKSPACE_ORIGIN_FAMILY.test("host-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost"),
    ).toBe(true);
  });

  it("rejects origins outside the workspace families", () => {
    expect(WORKSPACE_ORIGIN_FAMILY.test("localhost")).toBe(false);
    expect(WORKSPACE_ORIGIN_FAMILY.test("evil-agent-abc.example.com")).toBe(false);
    expect(WORKSPACE_ORIGIN_FAMILY.test("agent-abc.localhost.example.com")).toBe(false);
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
    OPEN_AI_KEYS_PAGE: "minds:open-ai-keys-page",
    OPEN_AI_KEYS_ACK: "minds:open-ai-keys-ack",
    BRING_APP_TO_FRONT: "minds:bring-app-to-front",
    CLOSE_ACTIVE_TAB: "minds:close-active-tab",
    REQUEST_ID_PATTERN,
  } as Parameters<typeof buildEmbedHandlers>[0]["contract"];
}

const WORKSPACE_AGENT_ID = "agent-ab12";

function makeHandlers() {
  const contract = makeContract();
  const navigations: { path: string; params?: Record<string, string> }[] = [];
  const popupOpens: (string | null)[] = [];
  const acks: string[] = [];
  let frontCount = 0;
  const handlers = buildEmbedHandlers({
    contract,
    navigate: (path, params) => navigations.push({ path, params }),
    sendAck: (type) => acks.push(type),
    bringAppToFront: () => {
      frontCount += 1;
    },
    workspaceAgentId: () => WORKSPACE_AGENT_ID,
    openRequestPopup: (requestId) => popupOpens.push(requestId),
  });
  return {
    contract,
    handlers,
    navigations,
    popupOpens,
    acks,
    frontCount: () => frontCount,
  };
}

describe("buildEmbedHandlers", () => {
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

  it("acknowledges the AI-keys page only after routing to it", () => {
    // The mint endpoint keys on the workspace id: the coordinate the
    // workspace sent wins (older template code sends its host id, which the
    // page dual-accepts), else the mounted surface's workspace id.
    const { contract, handlers, navigations, acks } = makeHandlers();
    handlers[contract.OPEN_AI_KEYS_PAGE]({});
    expect(navigations).toEqual([
      { path: "/settings/ai-keys", params: { workspace: WORKSPACE_AGENT_ID } },
    ]);
    expect(acks).toEqual([contract.OPEN_AI_KEYS_ACK]);

    handlers[contract.OPEN_AI_KEYS_PAGE]({ hostId: "host-cd34" });
    expect(navigations[1]).toEqual({
      path: "/settings/ai-keys",
      params: { workspace: "host-cd34" },
    });
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
