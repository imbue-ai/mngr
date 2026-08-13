import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { InboxModel } from "../../models/inbox";
import type { PredefinedPermissionDetail } from "../../models/inbox";
import { jsonResponse, settle } from "../../testing";
import { ShellState } from "../shell/shell-state";
import { PredefinedPermissionDetailView } from "./inbox/PredefinedPermissionDetail";
import { InboxPage, requestDetailView } from "./InboxPage";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, collectVnodes } from "../../testing";

// Latchkey requests are filed by the workspace's system-services sibling, so a
// request's own agent_id is NEVER the workspace id. Fixtures keep them distinct
// so nothing here can pass by comparing an id space that does not occur live.
const WORKSPACE_ID = "agent-ab12";
const FILING_SIBLING_ID = "agent-sibling-ab12";

const CARD_A = {
  id: "evt-a",
  kind_label: "permission",
  ws_name: "alpha",
  display_name: "Slack",
  accent: "#123456",
  workspace_agent_id: WORKSPACE_ID,
};
const CARD_B = { ...CARD_A, id: "evt-b", ws_name: "beta", display_name: "Gmail", accent: "#654321" };

const PREDEFINED_DETAIL: PredefinedPermissionDetail = {
  kind: "predefined",
  request_id: "evt-a",
  agent_id: FILING_SIBLING_ID,
  ws_name: "alpha",
  rationale: "read the channel",
  scope: "slack-api",
  display_name: "Slack",
  service_name: "slack",
  permission_groups: [
    {
      heading: "Full access",
      is_extras: false,
      rows: [{ permission: "slack-read-all", label: "Read everything", description: "", is_wildcard: false }],
    },
  ],
  checked_permissions: ["slack-read-all"],
  account_choices: [
    { value: "", label: "Default account", hint: "", is_credential_setup_needed: false, is_account_name_needed: false },
  ],
  selected_account_value: "",
  new_account_value: ":new-account",
  wildcard_permission: "any",
  will_open_browser: false,
  manual_credentials: null,
};

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

interface Harness {
  shell: ShellState;
  render: () => AnyVnode;
  detailUrls: string[];
  sent: [string, string][];
  closeCount: () => number;
}

/** Mount the popup page over stubbed inbox endpoints and a real ShellState, and
 * hand back a renderer that calls view() directly (no DOM), the idiom the other
 * view suites use. `selected` is the ?selected the route carries. */
async function mountPopup(
  cards: (typeof CARD_A)[],
  selected: string | null,
  options: { displayed?: string | null } = {},
): Promise<Harness> {
  const detailUrls: string[] = [];
  vi.stubGlobal("fetch", (url: string) => {
    if (url.endsWith("/detail")) {
      detailUrls.push(url);
      return Promise.resolve(
        jsonResponse({ detail: { ...PREDEFINED_DETAIL, request_id: url.split("/")[4] } }),
      );
    }
    if (url.endsWith("/grant")) return Promise.resolve(jsonResponse({ outcome: "GRANTED" }));
    if (url.endsWith("/deny")) return Promise.resolve(jsonResponse({ ok: true }));
    return Promise.resolve(jsonResponse({ cards }));
  });
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const routeSelection = selected;
  vi.spyOn(m.route, "param").mockImplementation(
    ((name: string) => (name === "selected" ? (routeSelection ?? undefined) : undefined)) as typeof m.route.param,
  );

  const stores = createEmptyStores();
  const shell = new ShellState(stores);
  shell.displayedWorkspaceAnyId = options.displayed === undefined ? WORKSPACE_ID : options.displayed;
  const sent: [string, string][] = [];
  shell.registerPermissionResolvedSender((requestId, verdict) => sent.push([requestId, verdict]));
  let closeCount = 0;
  vi.spyOn(shell, "closeAppOverlay").mockImplementation(() => {
    closeCount += 1;
    return true;
  });
  registerAppContext({ stores, shell });

  const instance = (InboxPage as () => m.Component)();
  const vnode = m(instance, {} as m.Attributes) as m.Vnode;
  (instance.oninit as unknown as (v: m.Vnode) => void).call(instance, vnode);
  await settle();
  await settle();

  const render = (): AnyVnode =>
    (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(instance, vnode) as unknown as AnyVnode;
  return {
    shell,
    render,
    detailUrls,
    sent,
    closeCount: () => closeCount,
  };
}

describe("the request popup's body", () => {
  it("heads the dialog with the machine the request came from", async () => {
    const { render } = await mountPopup([CARD_A], "evt-a");
    const root = render();
    const text = allText(root);
    expect(text).toContain("Permission request");
    expect(text).toContain("for");
    expect(text).toContain("alpha");
    const dot = collectVnodes(root).find((vnode) => String(attrsOf(vnode).style ?? "").includes("#123456"));
    expect(dot, "the eyebrow carries the machine's accent dot").toBeDefined();
  });

  it("drops the 'for <machine>' half while no card matches the selection", async () => {
    const { render } = await mountPopup([], null);
    expect(allText(render())).toContain("Permission request");
    expect(allText(render())).not.toContain("alpha");
  });

  it("shows one request's grant dialog, and no list of the others", async () => {
    // Popup-only review: the second pending request is not on screen anywhere,
    // so there is nothing to pick from -- only the one being reviewed.
    const { render } = await mountPopup([CARD_A, CARD_B], "evt-a");
    const root = render();
    const dialogs = collectVnodes(root).filter((vnode) => vnode.tag === PredefinedPermissionDetailView);
    expect(dialogs).toHaveLength(1);
    expect(allText(root)).not.toContain("beta");
    expect(allText(root)).not.toContain("Gmail");
  });

  it("dispatches each detail kind to its own dialog", () => {
    const model = new InboxModel();
    const unavailable = requestDetailView(model, {
      kind: "unavailable",
      message: "It has already been processed.",
    });
    expect(allText(unavailable)).toContain("This permission request is no longer available");
    expect(allText(unavailable)).toContain("It has already been processed.");
    const unsupported = requestDetailView(model, { kind: "unsupported", message: "no handler" });
    expect(allText(unsupported)).toContain("no handler");
  });

  it("says the queue is empty rather than showing a blank card", async () => {
    const { render } = await mountPopup([], null);
    expect(allText(render())).toContain("You're all caught up — no pending requests.");
  });

  // The popup opens only when the user asks for it -- the in-chat card's
  // "Review & respond" button or a Waiting-on-you row -- so it offers nothing
  // that would make it open by itself: no "open this automatically" control,
  // and no preference behind one. Re-adding either fails here.
  it("offers no way to make the popup open by itself", async () => {
    const { render } = await mountPopup([CARD_A], "evt-a");
    const root = render();
    expect(allText(root).toLowerCase()).not.toContain("automatic");
    const dialog = collectVnodes(root).find((vnode) => vnode.tag === PredefinedPermissionDetailView);
    const model = attrsOf(dialog as AnyVnode).model as InboxModel;
    const surface = [
      ...Object.keys(model),
      ...Object.getOwnPropertyNames(Object.getPrototypeOf(model)),
    ].map((name) => name.toLowerCase());
    expect(surface.filter((name) => name.includes("autoopen"))).toEqual([]);
  });
});

describe("the request popup's selection", () => {
  it("opens on the request the route named", async () => {
    const { detailUrls } = await mountPopup([CARD_A, CARD_B], "evt-b");
    expect(detailUrls).toEqual(["/ui/api/inbox/evt-b/detail"]);
  });

  it("falls back to the head of the queue when the route named none", async () => {
    const { detailUrls } = await mountPopup([CARD_A, CARD_B], null);
    expect(detailUrls).toEqual(["/ui/api/inbox/evt-a/detail"]);
  });

  it("re-selects when a second entry point names another request", async () => {
    // A Waiting-on-you row clicked while the popup is up only changes the
    // query, which preserves this component instance.
    const { render, detailUrls } = await mountPopup([CARD_A, CARD_B], "evt-a");
    vi.spyOn(m.route, "param").mockImplementation(
      ((name: string) => (name === "selected" ? "evt-b" : undefined)) as typeof m.route.param,
    );

    render();
    await settle();

    expect(detailUrls).toEqual(["/ui/api/inbox/evt-a/detail", "/ui/api/inbox/evt-b/detail"]);
    expect(allText(render())).toContain("beta");
  });
});

describe("the request popup's resolution relay", () => {
  it("tells the asking workspace the moment its request is approved", async () => {
    const { render, sent } = await mountPopup([CARD_A, CARD_B], "evt-a");
    const dialog = collectVnodes(render()).find((vnode) => vnode.tag === PredefinedPermissionDetailView);
    const model = attrsOf(dialog as AnyVnode).model as InboxModel;

    await model.approve();

    expect(sent).toEqual([["evt-a", "granted"]]);
  });

  it("says nothing to a workspace that did not ask", async () => {
    // The popup floats over whatever is on screen, so the request being reviewed
    // often belongs to some other machine; its id must not be handed to the
    // workspace that happens to be displayed.
    const { render, sent } = await mountPopup([CARD_A], "evt-a", { displayed: "agent-other" });
    const dialog = collectVnodes(render()).find((vnode) => vnode.tag === PredefinedPermissionDetailView);

    (attrsOf(dialog as AnyVnode).model as InboxModel).deny();

    expect(sent).toEqual([]);
  });

  it("dismisses itself through the shell once nothing is left to review", async () => {
    const { render, closeCount } = await mountPopup([CARD_A], "evt-a");
    const dialog = collectVnodes(render()).find((vnode) => vnode.tag === PredefinedPermissionDetailView);
    const model = attrsOf(dialog as AnyVnode).model as InboxModel;
    // The queue is empty on the reload that follows the grant.
    vi.stubGlobal("fetch", (url: string) => {
      if (url.endsWith("/grant")) return Promise.resolve(jsonResponse({ outcome: "GRANTED" }));
      return Promise.resolve(jsonResponse({ cards: [] }));
    });

    await model.approve();
    await settle();

    expect(closeCount()).toBe(1);
  });
});
