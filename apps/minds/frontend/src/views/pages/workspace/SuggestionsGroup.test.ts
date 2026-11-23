import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, byId, classesOf, jsonResponse, renderDeep, settle, withAttr } from "../../../testing";
import { ShellState } from "../../shell/shell-state";
import { SUGGESTIONS_UNAVAILABLE_LINE, SuggestionsGroup } from "./SuggestionsGroup";

const FIRST = "agent-" + "d".repeat(8);
const SECOND = "agent-" + "e".repeat(8);
const SWITCH_IDS = ["suggestions-next-step-switch", "suggestions-new-chat-switch"];
const ALL_ON = {
  is_next_step_suggestions_enabled: true,
  is_new_chat_suggestions_enabled: true,
};

interface Sent {
  url: string;
  method: string;
  body: unknown;
}

interface Harness {
  /** Draw the group for one workspace against the same component instance, as a route change would. */
  draw: (agentId: string) => AnyVnode;
  leave: () => void;
  sent: Sent[];
}

function harness(respond: (request: Sent) => Promise<Response>): Harness {
  const shell = new ShellState(createEmptyStores());
  registerAppContext({ stores: shell.stores, shell });
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const sent: Sent[] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const request = {
      url,
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null,
    };
    sent.push(request);
    return respond(request);
  });
  const instance = SuggestionsGroup();
  const state = {} as Parameters<NonNullable<typeof instance.view>>[0]["state"];
  let isInitialized = false;
  function vnodeFor(agentId: string): m.Vnode<{ agentId: string }, typeof state> {
    return { ...(m(SuggestionsGroup, { agentId }) as m.Vnode<{ agentId: string }, typeof state>), state };
  }
  let lastAgentId = "";
  return {
    draw(agentId) {
      lastAgentId = agentId;
      const vnode = vnodeFor(agentId);
      if (!isInitialized) {
        isInitialized = true;
        (instance.oninit as (v: typeof vnode) => void).call(state, vnode);
      }
      return renderDeep((instance.view as (v: typeof vnode) => m.Children).call(state, vnode)) as AnyVnode;
    },
    leave() {
      (instance.onremove as unknown as (v: unknown) => void).call(state, vnodeFor(lastAgentId));
    },
    sent,
  };
}

function switchOf(root: AnyVnode, id: string): AnyVnode {
  const found = byId(root, id);
  if (found === undefined) throw new Error(`no #${id} in the Suggestions group`);
  return found;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

describe("SuggestionsGroup", () => {
  it("names the two switches, each with what it is, and says they are the whole workspace's", async () => {
    const { draw } = harness(() => Promise.resolve(jsonResponse(ALL_ON)));
    draw(FIRST);
    await settle();
    const root = draw(FIRST);

    const rows = withAttr(root, "data-suggestion-switch");
    expect(rows.map((row) => attrsOf(row)["data-suggestion-switch"])).toEqual(["next-step", "new-chat"]);
    expect(rows.map((row) => allText(row))).toEqual([
      "Next step suggestions Suggested replies, offered after the agent finishes a reply.",
      "New chat suggestions Things to try in Studio, offered on a new chat.",
    ]);
    expect(allText(root)).toContain("These apply to the whole workspace, for everyone in it.");
    for (const [index, id] of SWITCH_IDS.entries()) {
      const drawn = switchOf(root, id);
      expect(attrsOf(drawn).role).toBe("switch");
      expect(attrsOf(drawn)["aria-label"]).toBe(["Next step suggestions", "New chat suggestions"][index]);
      // Each switch points at its own quiet line.
      expect(byId(root, String(attrsOf(drawn)["aria-describedby"]))).toBeDefined();
    }
  });

  it("stands a waiting placeholder where each switch will be until the workspace answers, then shows its values", async () => {
    const { draw, sent } = harness(() =>
      Promise.resolve(jsonResponse({ ...ALL_ON, is_new_chat_suggestions_enabled: false })),
    );

    const loading = draw(FIRST);
    expect(attrsOf(loading)["data-status"]).toBe("loading");
    for (const id of SWITCH_IDS) {
      const placeholder = switchOf(loading, id);
      // Not a switch yet: one drawn off before the answer would read as the answer.
      expect(attrsOf(placeholder).role).toBe("status");
      expect(attrsOf(placeholder)["aria-busy"]).toBe("true");
      expect(classesOf(placeholder)).toContain("is-checking");
    }
    expect(byId(loading, "suggestions-unavailable")).toBeUndefined();
    expect(sent).toEqual([{ url: `/ui/api/workspaces/${FIRST}/suggestion-settings`, method: "GET", body: null }]);

    await settle();
    const ready = draw(FIRST);
    expect(attrsOf(ready)["data-status"]).toBe("ready");
    expect(SWITCH_IDS.map((id) => attrsOf(switchOf(ready, id))["aria-checked"])).toEqual(["true", "false"]);
    expect(SWITCH_IDS.map((id) => attrsOf(switchOf(ready, id)).disabled)).toEqual([false, false]);
  });

  it.each(["UNREACHABLE", "UNSUPPORTED"])(
    "says when the settings are available, with nothing to flip, for a workspace that gave none (%s)",
    async (code) => {
      const { draw } = harness(() => Promise.resolve(jsonResponse({ error: "No.", code }, 502)));
      draw(FIRST);
      await settle();
      const root = draw(FIRST);

      expect(attrsOf(root)["data-status"]).toBe("unavailable");
      expect(allText(byId(root, "suggestions-unavailable"))).toBe(SUGGESTIONS_UNAVAILABLE_LINE);
      for (const id of SWITCH_IDS) {
        const placeholder = switchOf(root, id);
        expect(attrsOf(placeholder).role).toBe("status");
        expect(attrsOf(placeholder).onclick).toBeUndefined();
        expect(classesOf(placeholder)).toContain("is-unknown");
      }
      expect(byId(root, "suggestions-write-error")).toBeUndefined();
    },
  );

  it("writes a flip at once, holding that switch alone while the write is out", async () => {
    let settleWrite: (response: Response) => void = () => undefined;
    const { draw, sent } = harness((request) =>
      request.method === "GET"
        ? Promise.resolve(jsonResponse(ALL_ON))
        : new Promise<Response>((resolve) => {
            settleWrite = resolve;
          }),
    );
    draw(FIRST);
    await settle();

    (attrsOf(switchOf(draw(FIRST), "suggestions-new-chat-switch")).onclick as () => void)();

    expect(sent.at(-1)).toEqual({
      url: `/ui/api/workspaces/${FIRST}/suggestion-settings`,
      method: "PUT",
      body: { is_new_chat_suggestions_enabled: false },
    });
    const writing = draw(FIRST);
    const flipped = switchOf(writing, "suggestions-new-chat-switch");
    expect(attrsOf(flipped)["aria-checked"]).toBe("false");
    expect(attrsOf(flipped)["aria-busy"]).toBe("true");
    expect(attrsOf(flipped).disabled).toBe(true);
    expect(attrsOf(switchOf(writing, "suggestions-next-step-switch")).disabled).toBe(false);

    settleWrite(jsonResponse({ ...ALL_ON, is_new_chat_suggestions_enabled: false }));
    await settle();
    const written = switchOf(draw(FIRST), "suggestions-new-chat-switch");
    expect(attrsOf(written)["aria-checked"]).toBe("false");
    expect(attrsOf(written)["aria-busy"]).toBeUndefined();
    expect(attrsOf(written).disabled).toBe(false);
  });

  it("puts a switch back where it was and shows the reason when its write fails", async () => {
    const { draw } = harness((request) =>
      Promise.resolve(
        request.method === "GET"
          ? jsonResponse(ALL_ON)
          : jsonResponse(
              { error: "Couldn't reach this workspace's suggestion settings. Make sure it's running, then try again.", code: "UNREACHABLE" },
              502,
            ),
      ),
    );
    draw(FIRST);
    await settle();

    (attrsOf(switchOf(draw(FIRST), "suggestions-new-chat-switch")).onclick as () => void)();
    expect(attrsOf(switchOf(draw(FIRST), "suggestions-new-chat-switch"))["aria-checked"]).toBe("false");
    await settle();

    const root = draw(FIRST);
    expect(attrsOf(switchOf(root, "suggestions-new-chat-switch"))["aria-checked"]).toBe("true");
    expect(allText(byId(root, "suggestions-write-error"))).toContain(
      "Couldn't reach this workspace's suggestion settings. Make sure it's running, then try again.",
    );
    // The values are still the workspace's: the group stays usable for another try.
    expect(attrsOf(root)["data-status"]).toBe("ready");
  });

  it("reads the other workspace's switches when the route moves to it, and drops the first one's late answer", async () => {
    const answers = new Map<string, (response: Response) => void>();
    const { draw, sent } = harness(
      (request) =>
        new Promise<Response>((resolve) => {
          answers.set(request.url, resolve);
        }),
    );
    draw(FIRST);
    draw(SECOND);
    expect(sent.map((request) => request.url)).toEqual([
      `/ui/api/workspaces/${FIRST}/suggestion-settings`,
      `/ui/api/workspaces/${SECOND}/suggestion-settings`,
    ]);

    answers.get(`/ui/api/workspaces/${FIRST}/suggestion-settings`)?.(jsonResponse(ALL_ON));
    await settle();
    expect(attrsOf(draw(SECOND))["data-status"]).toBe("loading");

    answers.get(`/ui/api/workspaces/${SECOND}/suggestion-settings`)?.(
      jsonResponse({ ...ALL_ON, is_next_step_suggestions_enabled: false }),
    );
    await settle();
    const root = draw(SECOND);
    expect(attrsOf(switchOf(root, "suggestions-next-step-switch"))["aria-checked"]).toBe("false");
  });
});
