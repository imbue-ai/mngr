import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { settle } from "../../../testing";
import {
  DESKTOPS,
  awsAvailable,
  browserAvailable,
  credentialsSignIn,
  desktopEgress,
  pathSync,
  permissionsView,
  sharedPath,
  slackConnection,
  stravaAvailable,
} from "../../../models/workspacePermissions.testing";
import type { UiClientPermissions, UiWorkspacePermissions } from "../../../generated/ui";
import type { RequestResult } from "../../../models/errors";
import { clientPermissionsView, microphoneRow } from "../../../models/clientPermissions.testing";
import { defaultFetchJson } from "../../../models/workspaceOptions";
import { ADD_SHARED_PATH_ROW_KEY, PermissionsModel, isPathSyncOn } from "../../../models/workspacePermissions";
import type { ErrorDialogRequest } from "../../../models/errorDialog";
import { PermissionsTab, permissionsNav } from "./PermissionsTab";
import { forgetFailedServiceMarks } from "../../components/ServiceMark";
import { panelIdFor } from "../../components/Collapsible";
import {
  EXPERIMENTAL_CONNECTOR_RISK_ATTR,
  EXPERIMENTAL_CONNECTOR_RISK_DETAIL,
} from "../../components/ConnectorRisk";
import { IMPLICIT_PERMISSIONS_HINT_ATTR } from "../../components/ImplicitPermissionsHint";
import { Spinner } from "../../components/Spinner";
import { ErrorNotice } from "../../components/ErrorDisplay";
import type { ErrorAction, UserFacingError } from "../../../models/errors";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  byId,
  classesOf,
  collapsibleTrigger,
  collectText,
  collectVnodes,
  errorMarks,
  pressCollapsible,
  renderDeep,
  required,
  withAttr,
} from "../../../testing";

const AGENT_ID = "agent-" + "c".repeat(8);
const CLIENT_PERMISSIONS_URL = `/ui/api/workspaces/${AGENT_ID}/client-permissions`;

// Which marks 404'd is remembered app-wide, on purpose. Without this the
// test that exercises a missing mark would retire that service for every
// test after it in this file, making the suite order-dependent.
beforeEach(forgetFailedServiceMarks);
afterEach(() => {
  vi.unstubAllGlobals();
});

/** Copy carried by the disconnect confirm alone -- the panel's own Disconnect
 * block says something similar, so the dialog has to be read on its own. Empty
 * while the dialog is closed, since a closed one contributes nothing to the
 * tree. */
function disconnectDialogText(node: unknown): string {
  const dialog = collectVnodes(node).find((vnode) => attrsOf(vnode).id === "ws-perm-disconnect");
  return dialog === undefined ? "" : allText(dialog.children);
}

/** The error displays in the tree, keyed by id: each carries our sentence and
 * the details behind its See details. */
function errorNotices(node: unknown): Map<string, { error: UserFacingError; actions: ErrorAction[] }> {
  const notices = new Map<string, { error: UserFacingError; actions: ErrorAction[] }>();
  for (const vnode of collectVnodes(node)) {
    if (vnode.tag !== ErrorNotice) continue;
    const attrs = attrsOf(vnode);
    notices.set(String(attrs.id), {
      error: attrs.error as UserFacingError,
      actions: (attrs.actions as ErrorAction[] | undefined) ?? [],
    });
  }
  return notices;
}

function switches(node: unknown): AnyVnode[] {
  return collectVnodes(node).filter((vnode) => classesOf(vnode).includes("perm-switch"));
}

function hasClass(vnode: AnyVnode, name: string): boolean {
  return classesOf(vnode).split(/\s+/).includes(name);
}

/** The Spinner component vnodes in the tree. Matched on the component itself
 * rather than its class, since a closure component's markup only exists once
 * mithril has drawn it. */
function spinners(node: unknown): AnyVnode[] {
  return collectVnodes(node).filter((vnode) => vnode.tag === Spinner);
}

/** The mark wrappers only -- matched on the exact class token, since the
 * images inside them and the muted variant are all "service-mark"-prefixed. */
function marks(node: unknown): AnyVnode[] {
  return collectVnodes(node).filter((vnode) => hasClass(vnode, "service-mark"));
}

function markImages(mark: AnyVnode): AnyVnode[] {
  return collectVnodes(mark).filter((vnode) => vnode.tag === "img");
}

function markSources(mark: AnyVnode): string[] {
  return markImages(mark).map((img) => String(attrsOf(img).src));
}

interface RenderResult {
  root: m.Vnode;
  /** Draw the tab again, for a step that changed what it should show. */
  rerender: () => m.Vnode;
  /** Draw the same tab instance with another section asked for, the way a click in Settings' rail reaches it. */
  rerenderAt: (section: string | null) => m.Vnode;
  /** The rail entries Settings nests under its Permissions entry, for the section last asked for. */
  nav: () => unknown;
  /** Draw the same tab instance over another workspace's model, the way a
   * navigation between two workspaces' panes hands it new attrs. */
  rerenderFor: (other: PermissionsModel) => m.Vnode;
  /** Run the component's onremove, for a test that armed something with a
   * timer behind it -- an armed "Revoke all" outlives the test otherwise. */
  remove: () => void;
  model: PermissionsModel;
  selectedSections: string[];
  /** The request ids the pane asked to open on their own page. */
  reviewedRequests: string[];
  requests: { url: string; body: unknown }[];
  /** The Client permissions section's own requests. */
  clientRequests: { url: string; method: string; body: unknown }[];
}

/** Render the tab without a DOM: instantiate the closure component, run its
 * oninit (the lazy load) and call view() directly -- the idiom of
 * views/components/components.test.ts. */
async function render(
  view: UiWorkspacePermissions | null,
  options: {
    requestedSection?: string | null;
    workspaceName?: string;
    isReadRefused?: boolean;
    /** What the reload returns once a sign-in has succeeded, and what the
     * credential write answers with. */
    viewAfterConnect?: UiWorkspacePermissions;
    /** Refusal the credential write answers with instead of a view. */
    credentialRefusal?: string;
    /** Refusal the browser sign-in answers with, the way a service that turns
     * the user away does. */
    signInRefusal?: string;
    /** What the disconnect write answers with -- the connection is gone from
     * it, since the credential behind it has been cleared. */
    viewAfterDisconnect?: UiWorkspacePermissions;
    /** Answer a request yourself, for a test about how the pane behaves while
     * a write is in flight or after one is refused. Return null to fall
     * through to the ordinary answers below. */
    respond?: (url: string) => Promise<RequestResult> | null;
    /** Collects the error dialogs the pane raises. */
    shownDialogs?: ErrorDialogRequest[];
    /** What the Client permissions read answers with. */
    clientPermissions?: UiClientPermissions;
    /** Answer a Client permissions request yourself; null falls through to
     * the read's view, or for a write to the view that write produces. */
    respondClientPermissions?: (url: string, init?: RequestInit) => Promise<RequestResult> | null;
  } = {},
): Promise<RenderResult> {
  const requests: { url: string; body: unknown }[] = [];
  const clientRequests: { url: string; method: string; body: unknown }[] = [];
  let isSignedIn = false;
  const model = new PermissionsModel(AGENT_ID, {
    fetchJson: (url: string, init?: RequestInit) => {
      // The Client permissions section reads and writes on its own, so its
      // traffic is kept apart from the latchkey requests the other tests pin.
      if (url.startsWith(CLIENT_PERMISSIONS_URL)) {
        const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null;
        clientRequests.push({ url, method: init?.method ?? "GET", body });
        const answered = options.respondClientPermissions?.(url, init) ?? null;
        if (answered !== null) return answered;
        if (init?.method === "PUT") {
          const { decision } = body as { decision: "granted" | "denied" | "ask" | null };
          const row =
            decision === null ? microphoneRow() : microphoneRow({ decision, source: "workspace", stored: decision });
          return Promise.resolve({ ok: true, status: 200, body: clientPermissionsView([row]) });
        }
        return Promise.resolve({ ok: true, status: 200, body: options.clientPermissions ?? clientPermissionsView() });
      }
      requests.push({ url, body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null });
      const answered = options.respond?.(url) ?? null;
      if (answered !== null) return answered;
      if (options.isReadRefused === true) {
        return Promise.resolve({ ok: false, status: 503, body: { error: "gateway is down" } });
      }
      if (url === `/ui/api/workspaces/${AGENT_ID}/permissions/connect-browser`) {
        if (options.signInRefusal !== undefined) {
          return Promise.resolve({ ok: false, status: 400, body: { error: options.signInRefusal } });
        }
        isSignedIn = true;
        return Promise.resolve({ ok: true, status: 200, body: {} });
      }
      if (url.endsWith("/connector-disconnect")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          body: options.viewAfterDisconnect ?? permissionsView({ connections: [] }),
        });
      }
      if (url.endsWith("/connect-credentials")) {
        if (options.credentialRefusal !== undefined) {
          return Promise.resolve({ ok: false, status: 400, body: { error: options.credentialRefusal } });
        }
        isSignedIn = true;
      }
      const loaded = isSignedIn && options.viewAfterConnect !== undefined ? options.viewAfterConnect : view;
      return Promise.resolve({ ok: true, status: 200, body: loaded ?? permissionsView() });
    },
    redraw: () => undefined,
    errorDialogs: { show: (request) => void options.shownDialogs?.push(request) },
  });
  const selectedSections: string[] = [];
  const reviewedRequests: string[] = [];
  const attrs = {
    model,
    workspaceName: options.workspaceName ?? "alpha",
    requestedSection: options.requestedSection ?? null,
    onSelectSection: (section: string) => selectedSections.push(section),
    onReviewRequest: (requestId: string) => reviewedRequests.push(requestId),
  };
  const instance = PermissionsTab() as unknown as m.Component;
  const draw = (drawn: typeof attrs): m.Vnode =>
    (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
      instance,
      m(instance, drawn as unknown as m.Attributes) as m.Vnode,
    );
  (instance.oninit as unknown as (v: m.Vnode) => void).call(
    instance,
    m(instance, attrs as unknown as m.Attributes) as m.Vnode,
  );
  await settle();
  const rerender = (): m.Vnode => draw(attrs);
  const rerenderAt = (section: string | null): m.Vnode => {
    attrs.requestedSection = section;
    return draw(attrs);
  };
  const rerenderFor = (other: PermissionsModel): m.Vnode => draw({ ...attrs, model: other });
  const nav = (): unknown => permissionsNav(model, attrs.requestedSection, attrs.onSelectSection);
  const remove = (): void => (instance.onremove as unknown as () => void).call(instance);
  return {
    root: rerender(),
    rerender,
    rerenderAt,
    nav,
    rerenderFor,
    remove,
    model,
    selectedSections,
    reviewedRequests,
    requests,
    clientRequests,
  };
}

/** A connect awaits the sign-in, then the reload, then the selection -- more
 * promise hops than one settle covers. */
async function settleConnect(): Promise<void> {
  for (let index = 0; index < 4; index += 1) await settle();
}

describe("PermissionsTab loading", () => {
  it("reads the pane's own endpoint when the tab is first mounted", async () => {
    const { requests, clientRequests } = await render(permissionsView());
    expect(requests.map((request) => request.url)).toEqual([`/ui/api/workspaces/${AGENT_ID}/permissions`]);
    expect(clientRequests.map((request) => `${request.method} ${request.url}`)).toEqual([
      `GET ${CLIENT_PERMISSIONS_URL}`,
    ]);
  });

  it("offers a retry instead of an empty tree when the read fails", async () => {
    const { root, requests } = await render(null, { isReadRefused: true });
    const notice = errorNotices(root).get("ws-perm-load-error");
    expect(notice?.error).toEqual({ message: "gateway is down", details: "HTTP 503" });
    expect(switches(root)).toHaveLength(0);

    notice?.actions.find((action) => action.label === "Try again")?.onclick();
    await settle();
    expect(requests).toHaveLength(2);
  });

  it("renders the unavailable notice, never a 'nothing granted' screen", async () => {
    const { root } = await render(
      permissionsView({ connections: [], available_connections: [], permissions_unavailable: true }),
    );
    expect(errorNotices(root).get("ws-perm-unavailable")?.error.message).toBe(
      "Permissions can't be loaded right now. Try again in a moment.",
    );
    expect(allText(root)).not.toContain("Every available service already has an account connected.");
    expect(withAttr(root, "data-perm-panel")).toHaveLength(0);
  });

  const NEWER_FORMAT_VIEW: UiWorkspacePermissions = permissionsView({
    connections: [],
    available_connections: [],
    permissions_unavailable: true,
    unavailable_notice: {
      message: "This workspace's permissions were changed by a newer Imbue Studio. Update Imbue Studio to see and change them here.",
      detail: "permissions format 4 is newer than this build reads",
      remedy: "UPDATE_APP",
    },
  });

  it("offers only Update when a newer Imbue Studio wrote the permissions, since a re-read cannot help", async () => {
    vi.stubGlobal("window", { mindsNative: {} });
    const { root, requests } = await render(NEWER_FORMAT_VIEW);
    const notice = errorNotices(root).get("ws-perm-unavailable");
    expect(notice?.error).toEqual({
      message: "This workspace's permissions were changed by a newer Imbue Studio. Update Imbue Studio to see and change them here.",
      details: "permissions format 4 is newer than this build reads",
    });
    expect(notice?.actions.map((action) => action.label)).toEqual(["Update Imbue Studio"]);
    expect(requests).toHaveLength(1);
  });

  it("offers no Try again for permissions a newer Imbue Studio wrote even where it cannot update", async () => {
    // Outside the desktop app there is no updater, so there is no Update either.
    const { root } = await render(NEWER_FORMAT_VIEW);
    expect(errorNotices(root).get("ws-perm-unavailable")?.actions).toEqual([]);
  });

  it("leads with Restart when the permission service is down, keeping Try again as the lesser choice", async () => {
    // The service can come back by itself, so Try again stays; the restart is the sure fix.
    vi.stubGlobal("window", { mindsNative: { restartApp: () => {} } });
    const { root, requests } = await render(
      permissionsView({
        connections: [],
        available_connections: [],
        permissions_unavailable: true,
        unavailable_notice: {
          message: "Imbue Studio's permission service isn't running, so this workspace's permissions can't be shown.",
          detail: "Timed out waiting for the gateway port",
          remedy: "RESTART_APP",
        },
      }),
    );
    const actions = errorNotices(root).get("ws-perm-unavailable")?.actions ?? [];
    expect(actions.map((action) => action.label)).toEqual(["Restart Imbue Studio", "Try again"]);

    actions.find((action) => action.label === "Try again")?.onclick();
    await settle();
    expect(requests).toHaveLength(2);
  });

  it("draws the selected section's page alone, leaving the title and the list of sections to Settings", async () => {
    const { root } = await render(permissionsView(), { workspaceName: "alpha" });
    expect(withAttr(root, "data-perm-panel").map((panel) => attrsOf(panel)["data-perm-panel"])).toEqual([
      "conn:slack:",
    ]);
    expect(withAttr(root, "data-perm-nav")).toHaveLength(0);
    expect(collectVnodes(root).filter((vnode) => vnode.tag === "h1")).toHaveLength(0);
    expect(allText(root)).not.toContain("Permissions:");
  });

  it("calls the workspace 'this workspace' when the options load has not landed", async () => {
    // Permissions does not wait behind the workspace's options, so it can be drawn before, or without, a name.
    const { root, rerender } = await render(permissionsView(), { workspaceName: "" });
    expect(allText(root)).toContain("this workspace shares one sign-in with every other workspace on this computer");

    (attrsOf(withAttr(root, "data-perm-disconnect")[0]).onclick as () => void)();
    expect(disconnectDialogText(rerender())).toContain("from every workspace on this computer, including this workspace");
  });
});

describe("PermissionsTab waiting strip", () => {
  const waiting = (index: number) => ({
    id: `evt-${index}`,
    title: `Request ${index}`,
    reason: `because ${index}`,
    service_name: "slack",
  });

  it("stays hidden when nothing is pending", async () => {
    const { root } = await render(permissionsView());
    expect(allText(root)).not.toContain("Waiting on you");
  });

  it("lists every waiting request, oldest first", async () => {
    // No fold: the pane scrolls, which is why the list was moved into it.
    const { root } = await render(permissionsView({ waiting_requests: [0, 1, 2, 3, 4].map(waiting) }));

    const rows = withAttr(root, "data-perm-waiting-id");
    expect(rows.map((row) => attrsOf(row)["data-perm-waiting-id"])).toEqual([
      "evt-0",
      "evt-1",
      "evt-2",
      "evt-3",
      "evt-4",
    ]);
    expect(allText(root)).toContain("because 0");
  });

  it("leads the pane with the requests rather than a connection", async () => {
    // Opening Permissions with something pending shows the thing that is
    // waiting on an answer; the rest of the pane is what past answers built.
    const { root } = await render(
      permissionsView({ connections: [slackConnection()], waiting_requests: [waiting(1)] }),
    );

    expect(withAttr(root, "data-perm-panel")[0]).toBeDefined();
    expect(attrsOf(withAttr(root, "data-perm-panel")[0])["data-perm-panel"]).toBe("waiting");
  });



  it("opens the clicked request on its own page, without navigating the pane", async () => {
    // The pane stays mounted underneath the request page, with its scroll and
    // its section intact, so opening one must not route the pane anywhere.
    const { root, reviewedRequests } = await render(permissionsView({ waiting_requests: [waiting(7)] }));
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    try {
      (attrsOf(withAttr(root, "data-perm-waiting-id")[0]).onclick as () => void)();

      expect(reviewedRequests).toEqual(["evt-7"]);
      expect(routeSet).not.toHaveBeenCalled();
    } finally {
      routeSet.mockRestore();
    }
  });
});

describe("permissionsNav", () => {
  const sectionsOf = (nav: unknown): unknown[] =>
    withAttr(nav, "data-perm-nav").map((node) => attrsOf(node)["data-perm-nav"]);

  it("lists nothing until the read has landed, or when it failed", async () => {
    const answers: ((result: RequestResult) => void)[] = [];
    const pending = await render(permissionsView(), {
      respond: () => new Promise<RequestResult>((resolve) => answers.push(resolve)),
    });
    expect(pending.nav()).toBeNull();

    answers[0]({ ok: true, status: 200, body: permissionsView() });
    await settle();
    expect(sectionsOf(pending.nav())).toContain("conn:slack:");

    const refused = await render(null, { isReadRefused: true });
    expect(refused.nav()).toBeNull();
  });

  it("leads with Waiting on you, and its count, only while something is waiting", async () => {
    const waiting = { id: "evt-1", title: "Request 1", reason: "because", service_name: "slack" };
    const asked = await render(permissionsView({ waiting_requests: [waiting, { ...waiting, id: "evt-2" }] }));
    const entries = withAttr(asked.nav(), "data-perm-nav");
    expect(attrsOf(entries[0])["data-perm-nav"]).toBe("waiting");
    expect(attrsOf(entries[0])["aria-pressed"]).toBe("true");
    expect(allText(entries[0])).toContain("Waiting on you");
    expect(collectVnodes(entries[0]).map((vnode) => attrsOf(vnode).count)).toContain(2);

    const quiet = await render(permissionsView());
    expect(sectionsOf(quiet.nav())).not.toContain("waiting");
  });

  it("lists the connections, Add connection, the three self families, and Client permissions last", async () => {
    const { nav } = await render(
      permissionsView({
        connections: [slackConnection(), slackConnection({ service_name: "notion", display_name: "Notion", account: "n@x.com" })],
      }),
    );
    const navSections = sectionsOf(nav());
    expect(navSections).toEqual([
      "conn:slack:",
      "conn:notion:n@x.com",
      "add-connection",
      "local-files",
      "other-machines",
      "this-computer",
      "client-permissions",
    ]);
    expect(allText(withAttr(nav(), "data-perm-nav").at(-1))).toBe("Client permissions");
  });

  it("shows the account label under a connection only when it disambiguates", async () => {
    const shown = await render(
      permissionsView({ connections: [slackConnection({ show_account_label: true, account_label: "work@example.com" })] }),
    );
    const hidden = await render(permissionsView());
    const nav = (result: RenderResult) => allText(withAttr(result.nav(), "data-perm-nav"));
    expect(nav(shown)).toContain("work@example.com");
    expect(nav(hidden)).not.toContain("Default account");
  });

  it("selects the first connection by default and honours ?section", async () => {
    const view = permissionsView({
      connections: [slackConnection(), slackConnection({ service_name: "notion", display_name: "Notion" })],
    });
    const first = await render(view);
    const requested = await render(view, { requestedSection: "conn:notion:" });
    const pressed = (result: RenderResult) =>
      withAttr(result.nav(), "data-perm-nav")
        .filter((node) => attrsOf(node)["aria-pressed"] === "true")
        .map((node) => attrsOf(node)["data-perm-nav"]);
    const shown = (result: RenderResult) =>
      withAttr(result.root, "data-perm-panel").map((node) => attrsOf(node)["data-perm-panel"]);
    expect(pressed(first)).toEqual(["conn:slack:"]);
    expect(pressed(requested)).toEqual(["conn:notion:"]);
    // The rail and the page resolve the same section, so the pressed entry is the page on screen.
    expect(shown(first)).toEqual(["conn:slack:"]);
    expect(shown(requested)).toEqual(["conn:notion:"]);
  });

  it("falls back to the first section when the one asked for is not there", async () => {
    const { root, nav } = await render(permissionsView(), { requestedSection: "conn:gone:" });
    const pressed = withAttr(nav(), "data-perm-nav")
      .filter((node) => attrsOf(node)["aria-pressed"] === "true")
      .map((node) => attrsOf(node)["data-perm-nav"]);
    expect(pressed).toEqual(["conn:slack:"]);
    expect(attrsOf(withAttr(root, "data-perm-panel")[0])["data-perm-panel"]).toBe("conn:slack:");
  });

  it("reports a nav click as a section change for the URL to remember", async () => {
    const { nav, selectedSections } = await render(permissionsView());
    const localFiles = withAttr(nav(), "data-perm-nav").find(
      (node) => attrsOf(node)["data-perm-nav"] === "local-files",
    );
    (attrsOf(localFiles as AnyVnode).onclick as () => void)();
    expect(selectedSections).toEqual(["local-files"]);
  });
});

describe("PermissionsTab leaving a section", () => {
  const TWO_CONNECTIONS = permissionsView({
    connections: [slackConnection(), slackConnection({ service_name: "notion", display_name: "Notion" })],
  });
  const revokeButton = (node: unknown): AnyVnode => withAttr(node, "data-perm-revoke-all")[0];

  it("disarms a Revoke all when the rail moves to another section", async () => {
    const { root, rerender, rerenderAt, remove, requests } = await render(TWO_CONNECTIONS);

    (attrsOf(revokeButton(root)).onclick as () => void)();
    expect(allText(revokeButton(rerender()))).toContain("Really revoke all?");

    rerenderAt("local-files");
    const back = rerenderAt("conn:slack:");
    expect(allText(revokeButton(back))).toBe("Revoke all");
    // One click on the way back arms it again rather than firing the revoke.
    (attrsOf(revokeButton(back)).onclick as () => void)();
    await settle();
    expect(allText(revokeButton(rerender()))).toContain("Really revoke all?");
    expect(requests).toHaveLength(1);
    remove();
  });

  it("closes an open disconnect dialog when the rail moves to another section", async () => {
    const { root, rerender, rerenderAt } = await render(TWO_CONNECTIONS);

    (attrsOf(withAttr(root, "data-perm-disconnect")[0]).onclick as () => void)();
    expect(withAttr(rerender(), "data-perm-disconnect-confirm")).toHaveLength(1);

    expect(withAttr(rerenderAt("conn:notion:"), "data-perm-disconnect-confirm")).toHaveLength(0);
    expect(withAttr(rerenderAt("conn:slack:"), "data-perm-disconnect-confirm")).toHaveLength(0);
  });

  it("drops a refused change's notice when the rail moves to another section", async () => {
    const { root, rerender, rerenderAt, model } = await render(TWO_CONNECTIONS, {
      respond: (url) =>
        url.endsWith("/connector-toggle")
          ? Promise.resolve({ ok: false, status: 502, body: { error: "gateway refused" } })
          : null,
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    expect(errorNotices(rerender()).has("ws-perm-error")).toBe(true);

    expect(errorNotices(rerenderAt("local-files")).has("ws-perm-error")).toBe(false);
    expect(model.writeError).toBeNull();
  });

  it("drops a refused client permission's message when the rail moves to another section", async () => {
    const { root, rerender, rerenderAt } = await render(permissionsView(), {
      requestedSection: "client-permissions",
      respondClientPermissions: (_url, init) =>
        init?.method === "PUT"
          ? Promise.resolve({ ok: false, status: 409, body: { error: "saved by a newer version" } })
          : null,
    });
    const granted = withAttr(root, "data-client-permission-choice").find(
      (vnode) => attrsOf(vnode)["data-client-permission-choice"] === "granted",
    );
    (attrsOf(collectVnodes(granted).find((vnode) => vnode.tag === "input") as AnyVnode).onchange as () => void)();
    await settle();
    expect(byId(rerender(), "client-permissions-error")).toBeDefined();

    rerenderAt("local-files");
    expect(byId(rerenderAt("client-permissions"), "client-permissions-error")).toBeUndefined();
  });

  it("keeps what is half-done while the section asked for stays the same", async () => {
    // Every redraw runs the same comparison, so only a change of section may reset anything.
    const { root, rerender, remove } = await render(TWO_CONNECTIONS, { requestedSection: "conn:slack:" });

    (attrsOf(revokeButton(root)).onclick as () => void)();
    rerender();
    expect(allText(revokeButton(rerender()))).toContain("Really revoke all?");

    (attrsOf(withAttr(rerender(), "data-perm-disconnect")[0]).onclick as () => void)();
    rerender();
    expect(withAttr(rerender(), "data-perm-disconnect-confirm")).toHaveLength(1);
    remove();
  });
});

describe("PermissionsTab connection panel", () => {
  it("renders a switch per toggle carrying its granted state", async () => {
    const { root } = await render(permissionsView());
    const controls = switches(root);
    expect(controls).toHaveLength(2);
    expect(controls.map((node) => attrsOf(node)["aria-checked"])).toEqual([
      "false",
      "true",
    ]);
    expect(
      controls.map((node) => attrsOf(node)["data-perm-permission"]),
    ).toEqual(["slack-chat-read", "slack-chat-write"]);
    expect(allText(root)).toContain("Read messages");
  });

  it("sets the account label at the title's size and tone", async () => {
    const { root } = await render(
      permissionsView({ connections: [slackConnection({ account_label: "work@example.com" })] }),
    );
    const heading = collectVnodes(withAttr(root, "data-perm-panel")[0]).find(
      (node) => node.tag === "h2",
    ) as AnyVnode;
    const accountLabel = collectVnodes(heading).find(
      (node) => node.tag === "span" && allText(node).includes("· work@example.com"),
    ) as AnyVnode;
    expect(classesOf(heading)).toContain("type-heading");
    expect(classesOf(heading)).not.toContain("type-heading-lg");
    expect(classesOf(accountLabel)).toContain("type-heading");
    expect(classesOf(accountLabel)).not.toContain("type-heading-lg");
    expect(classesOf(accountLabel)).toContain("text-primary");
    expect(classesOf(accountLabel)).not.toContain("text-tertiary");
  });

  it("flips a toggle through the model with the row's scope and account", async () => {
    const { root, requests } = await render(
      permissionsView({ connections: [slackConnection({ account: "work@example.com" })] }),
      { requestedSection: "conn:slack:work@example.com" },
    );
    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    expect(requests[1]).toEqual({
      url: `/ui/api/workspaces/${AGENT_ID}/permissions/connector-toggle`,
      body: { scope: "slack-api", account: "work@example.com", permission: "slack-chat-read", enabled: true },
    });
  });

  it("draws no desktop egress row when the server says it is not supported", async () => {
    const { root } = await render(permissionsView());
    expect(withAttr(root, "data-perm-desktop-egress")).toHaveLength(0);
    expect(allText(root)).not.toContain("Proxy through my desktop");
    expect(switches(root)).toHaveLength(2);
  });

  function egressView(route: string[]): UiWorkspacePermissions {
    return permissionsView({
      desktops: DESKTOPS,
      connections: [slackConnection({ desktop_egress: desktopEgress(route) })],
    });
  }

  function egressRow(root: unknown): AnyVnode {
    return withAttr(root, "data-perm-desktop-egress")[0];
  }

  function clickEgress(root: unknown, attr: string, hop?: string): void {
    const targets = withAttr(egressRow(root), attr).filter((node) => hop === undefined || attrsOf(node)[attr] === hop);
    expect(targets).toHaveLength(1);
    expect(attrsOf(targets[0]).disabled).not.toBe(true);
    (attrsOf(targets[0]).onclick as () => void)();
  }

  /** The text as a browser would show it: a line that draws a device id as
   * an identifier is several text nodes, with no space between them. */
  function exactText(node: unknown): string {
    return collectText(node).join("");
  }

  function editorHops(root: unknown): string[] {
    return withAttr(egressRow(root), "data-desktop-egress-hop-label").map((node) => exactText(node));
  }

  function addOptions(root: unknown): string[] {
    return collectVnodes(withAttr(egressRow(root), "data-desktop-egress-add")[0])
      .filter((node) => node.tag === "option")
      .map((node) => allText(node));
  }

  /** The texts drawn as identifiers under `node`. */
  function deviceIdTexts(node: unknown): string[] {
    return collectVnodes(node)
      .filter((child) => classesOf(child) === "font-mono")
      .map((child) => exactText(child));
  }

  it("draws the desktop egress row above the permission toggles when it is supported", async () => {
    const { root } = await render(egressView(["host-here"]));
    expect(withAttr(root, "data-perm-desktop-egress")).toHaveLength(1);
    expect(allText(egressRow(root))).toContain("Proxy through my desktop");
    const controls = switches(root);
    expect(controls.map((node) => attrsOf(node)["data-perm-permission"])).toEqual([
      "desktop-egress",
      "slack-chat-read",
      "slack-chat-write",
    ]);
    expect(attrsOf(controls[0])["aria-label"]).toBe("Proxy through my desktop");
  });

  it.each([
    { route: ["self"], mode: "OFF", checked: "false", summary: "Requests leave from the workspace's own machine." },
    { route: ["host-here"], mode: "ON", checked: "true", summary: "Requests leave from this computer." },
    { route: ["host-office"], mode: "CUSTOM", checked: "true", summary: "host-office" },
    {
      route: ["host-here", "self"],
      mode: "CUSTOM",
      checked: "true",
      summary: "This computer, then the workspace's own machine",
    },
    {
      route: ["host-here", "host-office", "self"],
      mode: "CUSTOM",
      checked: "true",
      summary: "This computer, then host-office, then the workspace's own machine",
    },
  ])("draws the desktop egress route $route as $mode", async ({ route, mode, checked, summary }) => {
    const { root } = await render(egressView(route));
    const row = egressRow(root);
    expect(attrsOf(withAttr(row, "data-desktop-egress-mode")[0])["data-desktop-egress-mode"]).toBe(mode);
    const control = attrsOf(switches(row)[0]);
    expect(control["aria-checked"]).toBe(checked);
    expect(control["aria-label"]).toBe(
      mode === "CUSTOM" ? "Proxy through my desktop (custom route)" : "Proxy through my desktop",
    );
    const summaryNode = withAttr(row, "data-desktop-egress-summary")[0];
    expect(exactText(summaryNode)).toBe(summary);
    expect(control["aria-describedby"]).toBe("desktop-egress-summary-slack");
    expect(attrsOf(summaryNode).id).toBe("desktop-egress-summary-slack");
    expect(deviceIdTexts(summaryNode)).toEqual(route.filter((hop) => hop === "host-office"));
    const customMarks = withAttr(row, "data-desktop-egress-custom");
    expect(customMarks.map((node) => allText(node))).toEqual(mode === "CUSTOM" ? ["Custom"] : []);
    expect(withAttr(row, "data-desktop-egress-editor")).toHaveLength(0);
  });

  it.each([
    { from: ["self"], to: ["host-here"] },
    { from: ["host-here"], to: ["self"] },
    { from: ["host-here", "self"], to: ["self"] },
    { from: ["host-office"], to: ["self"] },
  ])("flips desktop egress from $from to $to at once", async ({ from, to }) => {
    const { root, requests } = await render(
      permissionsView({
        desktops: DESKTOPS,
        connections: [slackConnection({ account: "work@example.com", desktop_egress: desktopEgress(from) })],
      }),
      { requestedSection: "conn:slack:work@example.com" },
    );
    (attrsOf(switches(egressRow(root))[0]).onclick as () => void)();
    await settle();
    expect(requests.slice(1)).toEqual([
      {
        url: `/ui/api/workspaces/${AGENT_ID}/permissions/desktop-egress-route`,
        body: { service_name: "slack", route: to },
      },
    ]);
  });

  function egressViewWithoutThisComputer(route: string[]): UiWorkspacePermissions {
    return permissionsView({
      desktops: DESKTOPS.filter((desktop) => !desktop.is_this_computer),
      connections: [slackConnection({ desktop_egress: desktopEgress(route) })],
    });
  }

  it("cannot turn desktop egress on when no desktop is this computer", async () => {
    const { root, requests } = await render(egressViewWithoutThisComputer(["self"]));
    const control = attrsOf(switches(egressRow(root))[0]);
    expect(control["aria-checked"]).toBe("false");
    expect(control.disabled).toBe(true);
    (control.onclick as () => void)();
    await settle();
    expect(requests).toHaveLength(1);
    expect(attrsOf(withAttr(egressRow(root), "data-desktop-egress-adjust")[0]).disabled).toBe(false);
  });

  it("can still turn desktop egress off when no desktop is this computer", async () => {
    const { root, requests } = await render(egressViewWithoutThisComputer(["host-here"]));
    const row = egressRow(root);
    expect(attrsOf(withAttr(row, "data-desktop-egress-mode")[0])["data-desktop-egress-mode"]).toBe("CUSTOM");
    expect(exactText(withAttr(row, "data-desktop-egress-summary")[0])).toBe("host-here");
    const control = attrsOf(switches(row)[0]);
    expect(control.disabled).toBe(false);
    expect(control.title).toBeUndefined();
    (control.onclick as () => void)();
    await settle();
    expect(requests.slice(1)).toEqual([
      {
        url: `/ui/api/workspaces/${AGENT_ID}/permissions/desktop-egress-route`,
        body: { service_name: "slack", route: ["self"] },
      },
    ]);
  });

  it("edits a desktop egress route as a draft and posts it on Save", async () => {
    const saved = ["host-here", "host-studio", "host-office", "self"];
    const { root, rerender, requests } = await render(egressView(["host-here", "self"]), {
      respond: (url) =>
        url.endsWith("/desktop-egress-route")
          ? Promise.resolve({ ok: true, status: 200, body: egressView(saved) })
          : null,
    });

    clickEgress(root, "data-desktop-egress-adjust");
    expect(editorHops(rerender())).toEqual(["1. This computer", "2. The workspace's own machine"]);
    const add = withAttr(egressRow(rerender()), "data-desktop-egress-add")[0];
    expect(addOptions(rerender())).toEqual(["Add…", "host-office", "host-studio"]);
    expect(allText(withAttr(egressRow(rerender()), "data-desktop-egress-add-hint")[0])).toBe(
      "Another computer is added from Imbue Studio on that computer.",
    );
    const onAdd = attrsOf(add).onchange as (event: unknown) => void;
    onAdd({ target: { value: "host-office" } });
    (attrsOf(withAttr(egressRow(rerender()), "data-desktop-egress-add")[0]).onchange as typeof onAdd)({
      target: { value: "host-studio" },
    });
    clickEgress(rerender(), "data-desktop-egress-move-up", "host-studio");
    expect(editorHops(rerender())).toEqual([
      "1. This computer",
      "2. host-studio",
      "3. host-office",
      "4. The workspace's own machine",
    ]);
    expect(deviceIdTexts(withAttr(egressRow(rerender()), "data-desktop-egress-editor")[0])).toEqual([
      "host-studio",
      "host-office",
    ]);
    expect(addOptions(rerender())).toEqual([]);
    // Nothing is written, and the row still shows the saved route, until Save.
    expect(requests).toHaveLength(1);
    expect(exactText(withAttr(egressRow(rerender()), "data-desktop-egress-summary")[0])).toBe(
      "This computer, then the workspace's own machine",
    );

    clickEgress(rerender(), "data-desktop-egress-save");
    await settle();

    expect(requests.slice(1)).toEqual([
      {
        url: `/ui/api/workspaces/${AGENT_ID}/permissions/desktop-egress-route`,
        body: { service_name: "slack", route: saved },
      },
    ]);
    const after = egressRow(rerender());
    expect(withAttr(after, "data-desktop-egress-editor")).toHaveLength(0);
    expect(exactText(withAttr(after, "data-desktop-egress-summary")[0])).toBe(
      "This computer, then host-studio, then host-office, then the workspace's own machine",
    );
  });

  it("discards a desktop egress draft when the rail moves to another section", async () => {
    const { root, rerender, rerenderAt, requests } = await render(egressView(["host-here", "host-office", "self"]));

    clickEgress(root, "data-desktop-egress-adjust");
    clickEgress(rerender(), "data-desktop-egress-remove", "host-office");
    expect(withAttr(egressRow(rerender()), "data-desktop-egress-editor")).toHaveLength(1);
    rerenderAt("local-files");

    expect(withAttr(egressRow(rerenderAt("conn:slack:")), "data-desktop-egress-editor")).toHaveLength(0);
    expect(requests).toHaveLength(1);
    clickEgress(rerender(), "data-desktop-egress-adjust");
    expect(editorHops(rerender())).toEqual([
      "1. This computer",
      "2. host-office",
      "3. The workspace's own machine",
    ]);
  });

  it("discards a desktop egress draft when the page itself reports a change of section", async () => {
    // A disconnect that leaves the account's grants behind lands on the section it started from, so nothing
    // arrives from the rail: the page has to drop the draft before it reports the section.
    const leftover = permissionsView({
      desktops: DESKTOPS,
      connections: [
        slackConnection({ is_connected: false, desktop_egress: desktopEgress(["host-here", "host-office", "self"]) }),
      ],
    });
    const { root, rerender, selectedSections } = await render(egressView(["host-here", "host-office", "self"]), {
      viewAfterDisconnect: leftover,
    });

    clickEgress(root, "data-desktop-egress-adjust");
    (attrsOf(withAttr(rerender(), "data-perm-disconnect")[0]).onclick as () => void)();
    const asked = rerender();
    expect(withAttr(egressRow(asked), "data-desktop-egress-editor")).toHaveLength(1);
    (attrsOf(withAttr(asked, "data-perm-disconnect-confirm")[0]).onclick as () => void)();
    await settleConnect();

    expect(selectedSections).toEqual(["conn:slack:"]);
    expect(withAttr(egressRow(rerender()), "data-desktop-egress-editor")).toHaveLength(0);
  });

  it("shows a desktop egress draft only on the workspace it was opened for", async () => {
    const { root, rerender, rerenderFor } = await render(egressView(["host-here", "host-office", "self"]));
    clickEgress(root, "data-desktop-egress-adjust");
    clickEgress(rerender(), "data-desktop-egress-remove", "host-office");

    const other = new PermissionsModel("agent-" + "d".repeat(8), {
      fetchJson: () => Promise.resolve({ ok: true, status: 200, body: egressView(["self"]) }),
      redraw: () => undefined,
    });
    await other.load();
    const otherRow = egressRow(rerenderFor(other));

    expect(exactText(withAttr(otherRow, "data-desktop-egress-summary")[0])).toBe(
      "Requests leave from the workspace's own machine.",
    );
    expect(withAttr(otherRow, "data-desktop-egress-editor")).toHaveLength(0);
    expect(editorHops(rerender())).toEqual(["1. This computer", "2. The workspace's own machine"]);
  });

  it("discards a desktop egress draft on Cancel", async () => {
    const { root, rerender, requests } = await render(egressView(["host-here", "host-office", "self"]));

    clickEgress(root, "data-desktop-egress-adjust");
    expect(attrsOf(withAttr(egressRow(rerender()), "data-desktop-egress-save")[0]).disabled).toBe(true);
    clickEgress(rerender(), "data-desktop-egress-remove", "host-office");
    expect(editorHops(rerender())).toEqual(["1. This computer", "2. The workspace's own machine"]);
    expect(attrsOf(withAttr(egressRow(rerender()), "data-desktop-egress-save")[0]).disabled).toBe(false);
    clickEgress(rerender(), "data-desktop-egress-cancel");

    expect(withAttr(egressRow(rerender()), "data-desktop-egress-editor")).toHaveLength(0);
    expect(requests).toHaveLength(1);
    clickEgress(rerender(), "data-desktop-egress-adjust");
    expect(editorHops(rerender())).toEqual([
      "1. This computer",
      "2. host-office",
      "3. The workspace's own machine",
    ]);
  });

  it("offers no move that would break a desktop egress route, and never removes its last hop", async () => {
    const { root, rerender } = await render(egressView(["host-here", "self"]));
    clickEgress(root, "data-desktop-egress-adjust");
    const isDisabled = (attr: string, hop: string): unknown =>
      attrsOf(withAttr(egressRow(rerender()), attr).filter((node) => attrsOf(node)[attr] === hop)[0]).disabled;

    expect(isDisabled("data-desktop-egress-move-up", "host-here")).toBe(true);
    expect(isDisabled("data-desktop-egress-move-down", "host-here")).toBe(true);
    expect(isDisabled("data-desktop-egress-move-up", "self")).toBe(true);
    expect(isDisabled("data-desktop-egress-move-down", "self")).toBe(true);
    expect(isDisabled("data-desktop-egress-remove", "self")).toBe(false);

    expect(addOptions(rerender())).toEqual(["Add…", "host-office", "host-studio"]);
    clickEgress(rerender(), "data-desktop-egress-remove", "self");
    expect(editorHops(rerender())).toEqual(["1. This computer"]);
    expect(isDisabled("data-desktop-egress-remove", "host-here")).toBe(true);
    expect(addOptions(rerender())).toEqual(["Add…", "host-office", "host-studio", "The workspace's own machine"]);
  });

  it("keeps a desktop egress draft open when the machine refuses it", async () => {
    const { root, rerender } = await render(egressView(["host-here", "self"]), {
      respond: (url) =>
        url.endsWith("/desktop-egress-route")
          ? Promise.resolve({ ok: false, status: 502, body: { error: "the machine did not take it" } })
          : null,
    });
    clickEgress(root, "data-desktop-egress-adjust");
    clickEgress(rerender(), "data-desktop-egress-remove", "self");
    clickEgress(rerender(), "data-desktop-egress-save");
    await settle();

    const after = rerender();
    expect(editorHops(after)).toEqual(["1. This computer"]);
    expect(errorNotices(after).get("ws-perm-error")?.error.message).toBe("the machine did not take it");
  });

  it.each([
    { outcome: "closes the editor once the flip is saved", isRefused: false },
    { outcome: "keeps the draft when the flip is refused", isRefused: true },
  ])(
    "flips the switch over an open desktop egress draft and $outcome",
    async ({ isRefused }) => {
      const { root, rerender, requests } = await render(
        egressView(["host-here", "host-office", "self"]),
        {
          respond: (url) => {
            if (!url.endsWith("/desktop-egress-route")) return null;
            return Promise.resolve(
              isRefused
                ? {
                    ok: false,
                    status: 502,
                    body: { error: "the machine did not take it" },
                  }
                : { ok: true, status: 200, body: egressView(["self"]) },
            );
          },
        },
      );
      clickEgress(root, "data-desktop-egress-adjust");
      clickEgress(rerender(), "data-desktop-egress-remove", "host-office");

      (attrsOf(switches(egressRow(rerender()))[0]).onclick as () => void)();
      await settle();

      expect(requests.slice(1)).toEqual([
        {
          url: `/ui/api/workspaces/${AGENT_ID}/permissions/desktop-egress-route`,
          body: { service_name: "slack", route: ["self"] },
        },
      ]);
      expect(editorHops(rerender())).toEqual(
        isRefused ? ["1. This computer", "2. The workspace's own machine"] : [],
      );
    },
  );

  it("locks the open desktop egress editor while its save is in flight, except for Cancel", async () => {
    const { root, rerender, requests } = await render(
      egressView(["host-here", "host-office", "host-studio"]),
      {
        respond: (url) =>
          url.endsWith("/desktop-egress-route")
            ? new Promise(() => undefined)
            : null,
      },
    );
    clickEgress(root, "data-desktop-egress-adjust");
    clickEgress(rerender(), "data-desktop-egress-remove", "host-studio");
    const disabledStates = (attr: string): unknown[] =>
      withAttr(egressRow(rerender()), attr).map((node) => attrsOf(node).disabled);
    // Before the save there is something to lock: both hops can swap places.
    expect(disabledStates("data-desktop-egress-move-up")).toEqual([true, false]);
    expect(disabledStates("data-desktop-egress-move-down")).toEqual([false, true]);

    clickEgress(rerender(), "data-desktop-egress-save");
    await settle();

    expect(disabledStates("data-desktop-egress-save")).toEqual([true]);
    expect(disabledStates("data-desktop-egress-move-up")).toEqual([true, true]);
    expect(disabledStates("data-desktop-egress-move-down")).toEqual([true, true]);
    expect(disabledStates("data-desktop-egress-remove")).toEqual([true, true]);
    expect(disabledStates("data-desktop-egress-add")).toEqual([true]);
    expect(editorHops(rerender())).toEqual(["1. This computer", "2. host-office"]);

    clickEgress(rerender(), "data-desktop-egress-cancel");

    expect(withAttr(egressRow(rerender()), "data-desktop-egress-editor")).toHaveLength(0);
    expect(requests).toHaveLength(2);
  });

  it("spins the desktop egress row while its write is in flight and locks the other toggles", async () => {
    const { root, rerender } = await render(egressView(["self"]), {
      respond: (url) =>
        url.endsWith("/desktop-egress-route")
          ? new Promise(() => undefined)
          : null,
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    const after = rerender();

    expect(classesOf(switches(after)[0])).toContain("is-settling");
    expect(attrsOf(switches(after)[0]).disabled).toBe(true);
    // The wait is the switch's own ring; nothing spins beside it.
    expect(spinners(after)).toHaveLength(0);
    expect(attrsOf(withAttr(after, "data-desktop-egress-adjust")[0]).disabled).toBe(true);
    expect(attrsOf(switches(after)[1]).disabled).toBe(true);
    expect(attrsOf(switches(after)[1]).title).toBe("Waiting for the last change to reach this workspace.");
  });

  it("warns and blocks new grants when a connection is not connected", async () => {
    const { root } = await render(permissionsView({ connections: [slackConnection({ is_connected: false })] }));
    expect(allText(root)).toContain("This account isn't connected");
    const [ungranted, granted] = switches(root);
    // A leftover grant can always be turned off; only turning one on needs a
    // live connection.
    expect(attrsOf(ungranted).disabled).toBe(true);
    expect(attrsOf(ungranted).title).toBe("Connect this account before granting permissions.");
    expect(attrsOf(granted).disabled).toBe(false);
  });

  it("spins the row whose write is in flight and locks every other action", async () => {
    // The write does not answer until the workspace's own machine has taken
    // it, so the pane has to say so and refuse to be raced meanwhile.
    const { root, rerender } = await render(permissionsView(), {
      // Never resolves: the flip stays in flight for the whole test.
      respond: (url) =>
        url.endsWith("/connector-toggle") ? new Promise(() => undefined) : null,
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    const after = rerender();

    expect(classesOf(switches(after)[0])).toContain("is-settling");
    expect(classesOf(switches(after)[1])).not.toContain("is-settling");
    // The acting switch spins in its own knob; it and every other switch and
    // the panel's other actions are inert until the machine has answered.
    expect(spinners(after)).toHaveLength(0);
    expect(attrsOf(switches(after)[0]).disabled).toBe(true);
    expect(attrsOf(switches(after)[1]).disabled).toBe(true);
    expect(attrsOf(switches(after)[1]).title).toBe("Waiting for the last change to reach this workspace.");
    expect(attrsOf(withAttr(after, "data-perm-revoke-all")[0]).disabled).toBe(true);
    expect(attrsOf(withAttr(after, "data-perm-disconnect")[0]).disabled).toBe(true);
  });

  it("hides Revoke all until something is granted, and confirms before firing", async () => {
    const empty = await render(permissionsView({ connections: [slackConnection({ granted_count: 0 })] }));
    expect(withAttr(empty.root, "data-perm-revoke-all")).toHaveLength(0);

    const { rerender: renderOnce, remove, requests } = await render(permissionsView());
    const revokeButton = (node: unknown): AnyVnode => withAttr(node, "data-perm-revoke-all")[0];

    (attrsOf(revokeButton(renderOnce())).onclick as () => void)();
    const armed = renderOnce();
    expect(allText(revokeButton(armed))).toContain("Really revoke all?");
    expect(requests).toHaveLength(1);

    (attrsOf(revokeButton(armed)).onclick as () => void)();
    await settle();
    expect(requests[1].url).toBe(`/ui/api/workspaces/${AGENT_ID}/permissions/connector-revoke-all`);
    // The arm timer would otherwise outlive the test.
    remove();
  });

  it("asks before disconnecting, and does nothing at all when the confirm is declined", async () => {
    const { root, rerender, requests } = await render(permissionsView());

    // The confirm does not exist until the button is pressed: this is what
    // fails if Disconnect ever regresses to a no-confirm button.
    expect(withAttr(root, "data-perm-disconnect-confirm")).toHaveLength(0);
    expect(disconnectDialogText(root)).toBe("");

    (attrsOf(withAttr(root, "data-perm-disconnect")[0]).onclick as () => void)();
    const asked = rerender();

    expect(withAttr(asked, "data-perm-disconnect-confirm")).toHaveLength(1);
    const dialogText = disconnectDialogText(asked);
    // The account and the service are named, and the consequence that must not
    // be blurred -- which machines lose the sign-in -- is spelled out. This one
    // reads this computer's shared store, so all of its machines do.
    expect(dialogText).toContain("Disconnect Slack · Default account from this computer?");
    expect(dialogText).toContain("from every workspace on this computer, including alpha");
    expect(dialogText).toContain("connect it again");
    expect(dialogText).toContain("Yes, disconnect");
    expect(allText(asked)).toContain("alpha shares one sign-in with every other workspace on this computer");

    (attrsOf(withAttr(asked, "data-perm-disconnect-cancel")[0]).onclick as () => void)();
    await settle();
    const declined = rerender();

    expect(withAttr(declined, "data-perm-disconnect-confirm")).toHaveLength(0);
    // Only the initial read: declining posts nothing at all.
    expect(requests).toHaveLength(1);
  });

  it("scopes the disconnect copy to this machine when it holds its own credentials", async () => {
    const { root, rerender } = await render(permissionsView({ is_credential_store_shared: false }));

    expect(allText(root)).toContain("Only alpha loses this access");
    (attrsOf(withAttr(root, "data-perm-disconnect")[0]).onclick as () => void)();
    const dialogText = disconnectDialogText(rerender());

    expect(dialogText).toContain("Disconnect Slack · Default account from alpha?");
    expect(dialogText).toContain("Every other workspace keeps its own sign-in.");
    expect(dialogText).not.toContain("every workspace on this computer");
  });

  it("disconnects the account from latchkey once the confirm is accepted", async () => {
    const { root, rerender, requests } = await render(
      permissionsView({ connections: [slackConnection({ account: "work@example.com" })] }),
      { requestedSection: "conn:slack:work@example.com" },
    );

    (attrsOf(withAttr(root, "data-perm-disconnect")[0]).onclick as () => void)();
    (attrsOf(withAttr(rerender(), "data-perm-disconnect-confirm")[0]).onclick as () => void)();
    await settleConnect();

    expect(requests[1]).toEqual({
      url: `/ui/api/workspaces/${AGENT_ID}/permissions/connector-disconnect`,
      body: { service_name: "slack", account: "work@example.com" },
    });
  });

  it("offers the fix a refused disconnect names inside the dialog that holds its reason", async () => {
    vi.stubGlobal("window", { mindsNative: { restartApp: () => {} } });
    const { root, rerender } = await render(
      permissionsView({ connections: [slackConnection({ account: "work@example.com" })] }),
      {
        requestedSection: "conn:slack:work@example.com",
        respond: (url) =>
          url.endsWith("/connector-disconnect")
            ? Promise.resolve({
                ok: false,
                status: 502,
                body: {
                  error: "Imbue Studio's permission service isn't running, so this couldn't be saved.",
                  detail: "Timed out waiting for the gateway port",
                  remedy: "RESTART_APP",
                },
              })
            : null,
      },
    );

    (attrsOf(withAttr(root, "data-perm-disconnect")[0]).onclick as () => void)();
    (attrsOf(withAttr(rerender(), "data-perm-disconnect-confirm")[0]).onclick as () => void)();
    await settleConnect();

    const notice = errorNotices(rerender()).get("ws-perm-disconnect-error");
    expect(notice?.actions.map((action) => action.label)).toEqual(["Restart Imbue Studio"]);
  });

  it("leaves no dead connection selected once the disconnect lands", async () => {
    const gone = await render(permissionsView());
    (attrsOf(withAttr(gone.root, "data-perm-disconnect")[0]).onclick as () => void)();
    (attrsOf(withAttr(gone.rerender(), "data-perm-disconnect-confirm")[0]).onclick as () => void)();
    await settleConnect();
    expect(gone.selectedSections).toEqual(["add-connection"]);

    const notion = slackConnection({
      service_name: "notion",
      display_name: "Notion",
    });
    const survivor = await render(
      permissionsView({ connections: [slackConnection(), notion] }),
      {
        viewAfterDisconnect: permissionsView({ connections: [notion] }),
      },
    );
    (
      attrsOf(withAttr(survivor.root, "data-perm-disconnect")[0])
        .onclick as () => void
    )();
    (
      attrsOf(withAttr(survivor.rerender(), "data-perm-disconnect-confirm")[0])
        .onclick as () => void
    )();
    await settleConnect();
    expect(survivor.selectedSections).toEqual(["conn:notion:"]);
  });

  it("offers no disconnect on an account that is not connected", async () => {
    // There is no stored sign-in left to forget; the leftover grants are
    // Revoke all's business.
    const { root } = await render(permissionsView({ connections: [slackConnection({ is_connected: false })] }));
    expect(withAttr(root, "data-perm-disconnect")).toHaveLength(0);
  });

  it("offers to ask the agent to request a permission whose definition is gone", async () => {
    const { root, rerender } = await render(permissionsView(), {
      respond: (url) =>
        url.endsWith("/connector-toggle")
          ? Promise.resolve({
              ok: false,
              status: 409,
              body: {
                error: "Imbue Studio no longer knows what this permission covers.",
                detail: "Permission 'slack-chat-read' cannot be re-enabled because its definition is gone.",
                remedy: "ASK_AGENT",
              },
            })
          : null,
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();

    const notice = errorNotices(rerender()).get("ws-perm-error");
    expect(notice?.error.message).toBe("Imbue Studio no longer knows what this permission covers.");
    expect(notice?.actions.map((action) => action.label)).toEqual(["Ask the agent to request it again"]);
  });

  it("shows the fallback sentence, with the text behind See details, when the read fails with a plain-text 500", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("Internal Server Error: KeyError('host_id')", { status: 500 })),
    );
    const { root } = await render(permissionsView(), {
      respond: (url) => (url === `/ui/api/workspaces/${AGENT_ID}/permissions` ? defaultFetchJson(url) : null),
    });

    const notice = errorNotices(root).get("ws-perm-load-error");
    expect(notice?.error.message).toBe(
      "Permissions can't be loaded right now. Try again. If this persists, report a bug.",
    );
    expect(notice?.error.details).toContain("Internal Server Error: KeyError('host_id')");
  });

  it("shows the fallback sentence, not the text, when a change is refused with a plain-text 500", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Internal Server Error: KeyError('scope')", { status: 500 })));
    const { root, rerender } = await render(permissionsView(), {
      respond: (url) => (url.endsWith("/connector-toggle") ? defaultFetchJson(url, { method: "POST" }) : null),
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();

    const notice = errorNotices(rerender()).get("ws-perm-error");
    expect(notice?.error.message).not.toContain("KeyError");
    expect(notice?.error.message).toMatch(/Try again\. If this persists, report a bug\.$/);
    expect(notice?.error.details).toContain("Internal Server Error: KeyError('scope')");
    expect(notice?.actions).toEqual([]);
  });

  it("shows a failed write's message without dropping the rendered tree", async () => {
    const { root, rerender } = await render(permissionsView(), {
      respond: (url) =>
        url.endsWith("/connector-toggle")
          ? Promise.resolve({ ok: false, status: 502, body: { error: "gateway refused" } })
          : null,
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    const after = rerender();

    expect(errorNotices(after).get("ws-perm-error")?.error.message).toBe("gateway refused");
    expect(switches(after)).toHaveLength(2);
    expect(attrsOf(switches(after)[0])["aria-checked"]).toBe("false");
  });
});

describe("PermissionsTab add connection and self panels", () => {
  it("offers every unconnected service a Connect action", async () => {
    const { root, requests } = await render(permissionsView(), { requestedSection: "add-connection" });
    expect(allText(root)).toContain("Notion");
    const connect = withAttr(root, "data-perm-connect").find(
      (node) => attrsOf(node)["data-perm-connect"] === "notion",
    ) as AnyVnode;
    expect(allText(connect)).toContain("Connect");
    (attrsOf(connect).onclick as () => void)();
    await settle();
    expect(requests[1]).toEqual({ url: `/ui/api/workspaces/${AGENT_ID}/permissions/connect-browser`, body: { service_name: "notion" } });
  });

  it("offers a connected service a second account, once, above the unconnected ones", async () => {
    const { root, requests } = await render(
      permissionsView({ connections: [slackConnection(), slackConnection({ account: "work@example.com" })] }),
      { requestedSection: "add-connection" },
    );
    const rows = withAttr(root, "data-perm-connect");
    // Slack holds two accounts but offers one row, and it leads the catalog.
    expect(rows.map((node) => attrsOf(node)["data-perm-connect"])).toEqual(["slack", "notion"]);
    expect(allText(rows[0])).toContain("Add account");
    expect(allText(rows[1])).toContain("Connect");
    expect(allText(root)).toContain("Add another account");
    expect(allText(root)).toContain("Connect a new service");

    (attrsOf(rows[0]).onclick as () => void)();
    await settle();
    expect(requests[1]).toEqual({ url: `/ui/api/workspaces/${AGENT_ID}/permissions/connect-browser`, body: { service_name: "slack" } });
  });

  it("moves to the connection a completed sign-in added", async () => {
    const existing = slackConnection({ account: "work@example.com" });
    const added = slackConnection({ account: "personal@example.com" });
    const { root, selectedSections } = await render(
      permissionsView({ connections: [existing], available_connections: [] }),
      {
        requestedSection: "add-connection",
        viewAfterConnect: permissionsView({ connections: [existing, added], available_connections: [] }),
      },
    );

    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settleConnect();

    expect(selectedSections).toEqual(["conn:slack:personal@example.com"]);
  });

  it("moves to the connection a sign-in retried from its error dialog added", async () => {
    const existing = slackConnection({ account: "work@example.com" });
    const added = slackConnection({ account: "personal@example.com" });
    const shownDialogs: ErrorDialogRequest[] = [];
    let isRefused = true;
    const { root, selectedSections } = await render(
      permissionsView({ connections: [existing], available_connections: [] }),
      {
        requestedSection: "add-connection",
        viewAfterConnect: permissionsView({ connections: [existing, added], available_connections: [] }),
        shownDialogs,
        respond: (url) => {
          if (!url.endsWith("/connect-browser") || !isRefused) return null;
          isRefused = false;
          return Promise.resolve({ ok: false, status: 502, body: { error: "The sign-in window was closed." } });
        },
      },
    );

    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settleConnect();
    shownDialogs[0]?.actions?.find((action) => action.label === "Try again")?.onclick();
    await settleConnect();

    expect(selectedSections).toEqual(["conn:slack:personal@example.com"]);
  });

  it("stays put when the sign-in added no connection", async () => {
    const { root, selectedSections } = await render(permissionsView(), { requestedSection: "add-connection" });
    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settleConnect();
    expect(selectedSections).toEqual([]);
  });

  it("opens a credential form instead of a sign-in for a service with no browser flow", async () => {
    const { root, rerender, requests, model } = await render(
      permissionsView({ connections: [], available_connections: [awsAvailable()] }),
      { requestedSection: "add-connection" },
    );
    const connect = withAttr(root, "data-perm-connect").find(
      (node) => attrsOf(node)["data-perm-connect"] === "aws",
    ) as AnyVnode;
    expect(withAttr(root, "data-perm-credential-form")).toHaveLength(0);

    (attrsOf(connect).onclick as () => void)();
    await settle();
    const opened = rerender();

    // Nothing was posted: the browser sign-in is not what connects this one.
    expect(requests).toHaveLength(1);
    expect(model.credentialFormServiceName).toBe("aws");
    const form = withAttr(opened, "data-perm-credential-form")[0];
    expect(allText(form)).toContain("Access key id");
    expect(allText(form)).toContain("Secret access key");
    expect(allText(form)).toContain("AWS can't be signed in to through a browser");
  });

  it("stores what the form carries and lands on the connection it created", async () => {
    const aws = slackConnection({ service_name: "aws", display_name: "AWS", sign_in: credentialsSignIn() });
    const { root, rerender, requests, selectedSections } = await render(
      permissionsView({ connections: [], available_connections: [awsAvailable()] }),
      {
        requestedSection: "add-connection",
        viewAfterConnect: permissionsView({ connections: [aws], available_connections: [] }),
      },
    );
    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settle();
    const opened = rerender();

    const inputs = withAttr(opened, "name").filter((node) =>
      String(attrsOf(node).name).startsWith("credential-"),
    );
    expect(inputs.map((node) => attrsOf(node).name)).toEqual([
      "credential-access-key-id",
      "credential-secret-access-key",
    ]);
    (attrsOf(inputs[0]).oninput as (event: unknown) => void)({ target: { value: "AKIA" } });
    (attrsOf(inputs[1]).oninput as (event: unknown) => void)({ target: { value: "s3cret" } });
    const filled = rerender();
    const submit = withAttr(filled, "data-perm-credential-submit")[0];
    expect(attrsOf(submit).disabled).toBe(false);
    (attrsOf(submit).onclick as () => void)();
    await settleConnect();

    expect(requests[1]).toEqual({
      url: `/ui/api/workspaces/${AGENT_ID}/permissions/connect-credentials`,
      body: {
        service_name: "aws",
        value_by_parameter_name: { "access-key-id": "AKIA", "secret-access-key": "s3cret" },
        account_name: "",
      },
    });
    expect(selectedSections).toEqual(["conn:aws:"]);
    expect(withAttr(rerender(), "data-perm-credential-form")).toHaveLength(0);
  });

  it("holds the submit back until every input is filled in", async () => {
    const { root, rerender } = await render(
      permissionsView({
        connections: [],
        available_connections: [awsAvailable({ is_account_name_required: true })],
      }),
      { requestedSection: "add-connection" },
    );
    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settle();
    const opened = rerender();

    expect(attrsOf(withAttr(opened, "data-perm-credential-submit")[0]).disabled).toBe(true);
    // A named account is asked for on top of the command's own values.
    expect(withAttr(opened, "name").map((node) => attrsOf(node).name)).toContain("account_name");
  });

  it("reports a refused credential next to the fields, with what was typed still there", async () => {
    const { root, rerender, model } = await render(
      permissionsView({ connections: [], available_connections: [awsAvailable()] }),
      {
        requestedSection: "add-connection",
        credentialRefusal: "AWS rejected those credentials: that is not an access key id",
      },
    );
    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settle();
    const opened = rerender();
    const inputs = withAttr(opened, "name").filter((node) =>
      String(attrsOf(node).name).startsWith("credential-"),
    );
    (attrsOf(inputs[0]).oninput as (event: unknown) => void)({ target: { value: "nonsense" } });
    (attrsOf(inputs[1]).oninput as (event: unknown) => void)({ target: { value: "s3cret" } });
    (attrsOf(withAttr(rerender(), "data-perm-credential-submit")[0]).onclick as () => void)();
    await settleConnect();
    const refused = rerender();

    const form = withAttr(refused, "data-perm-credential-form")[0];
    expect(errorNotices(form).get("ws-perm-credential-error")?.error.message).toContain(
      "that is not an access key id",
    );
    const keptValues = withAttr(form, "name")
      .filter((node) => String(attrsOf(node).name).startsWith("credential-"))
      .map((node) => attrsOf(node).value);
    expect(keptValues).toEqual(["nonsense", "s3cret"]);
    expect(model.writeError).toBeNull();
  });

  it("offers the fix a refused credential write names beside the form", async () => {
    vi.stubGlobal("window", { mindsNative: { restartApp: () => {} } });
    const { root, rerender } = await render(
      permissionsView({ connections: [], available_connections: [awsAvailable()] }),
      {
        requestedSection: "add-connection",
        respond: (url) =>
          url.endsWith("/connect-credentials")
            ? Promise.resolve({
                ok: false,
                status: 502,
                body: {
                  error: "Imbue Studio couldn't read its settings for where your workspaces run.",
                  detail: "config.toml: invalid TOML",
                  remedy: "RESTART_APP",
                },
              })
            : null,
      },
    );
    (attrsOf(withAttr(root, "data-perm-connect")[0]).onclick as () => void)();
    await settle();
    const inputs = withAttr(rerender(), "name").filter((node) =>
      String(attrsOf(node).name).startsWith("credential-"),
    );
    (attrsOf(inputs[0]).oninput as (event: unknown) => void)({ target: { value: "AKIA" } });
    (attrsOf(inputs[1]).oninput as (event: unknown) => void)({ target: { value: "s3cret" } });
    (attrsOf(withAttr(rerender(), "data-perm-credential-submit")[0]).onclick as () => void)();
    await settleConnect();

    const form = withAttr(rerender(), "data-perm-credential-form")[0];
    const notice = errorNotices(form).get("ws-perm-credential-error");
    expect(notice?.actions.map((action) => action.label)).toEqual(["Restart Imbue Studio"]);
  });

  it("offers no action for a service it cannot work out the credentials for", async () => {
    const { root } = await render(
      permissionsView({ connections: [], available_connections: [awsAvailable({ credential_parameters: [] })] }),
      { requestedSection: "add-connection" },
    );
    const connect = withAttr(root, "data-perm-connect")[0];
    expect(attrsOf(connect).disabled).toBe(true);
    expect(String(attrsOf(connect).title)).toBe("Imbue Studio can't set up AWS from here yet.");
  });

  it("says so when every service already has an account", async () => {
    const { root } = await render(permissionsView({ available_connections: [] }), {
      requestedSection: "add-connection",
    });
    expect(allText(root)).toContain("Every available service already has an account connected.");
  });

  it("disables a revoked grant whose schema is gone, explaining why", async () => {
    const { root } = await render(
      permissionsView({
        workspace_toggles: [
          {
            permission: "verb-1",
            label: "Destroy machines",
            detail: "beta",
            description: "",
            is_granted: false,
            can_enable: false,
          },
        ],
      }),
      { requestedSection: "other-machines" },
    );
    const control = switches(root)[0];
    expect(attrsOf(control).disabled).toBe(true);
    expect(attrsOf(control).title).toBe(
      "This grant can't be re-enabled; ask the agent to request it again.",
    );
  });

  it("says so when neither self family has anything yet", async () => {
    const localFiles = await render(permissionsView(), { requestedSection: "local-files" });
    const otherMachines = await render(permissionsView(), { requestedSection: "other-machines" });
    expect(allText(localFiles.root)).toContain("No files are being shared with agents in this workspace yet.");
    expect(allText(otherMachines.root)).toContain("Agents in this workspace can't manage your other workspaces yet.");
  });
  it("raises a refused connection as an error dialog over the pane, with a Try again that repeats the sign-in", async () => {
    // A service that turns the sign-in away usually says something the user has
    // to act on somewhere else entirely, at several lines' length. That is why
    // it gets a dialog while a refused toggle stays inline beside its row -- if
    // this ever went back to the thin banner it would be missed on a pane the
    // user has scrolled down.
    const refusal =
      "your Zoom user is not allowed to create apps in the Zoom App Marketplace; ask an administrator";
    const shownDialogs: ErrorDialogRequest[] = [];
    const { root, rerender, requests } = await render(permissionsView(), {
      requestedSection: "add-connection",
      signInRefusal: refusal,
      shownDialogs,
    });
    const connect = withAttr(root, "data-perm-connect")[0];

    (attrsOf(connect).onclick as () => void)();
    await settleConnect();

    expect(shownDialogs).toHaveLength(1);
    const dialog = shownDialogs[0];
    expect(dialog?.title).toBe("Couldn't connect");
    expect(dialog?.error.message).toContain(refusal);
    expect(dialog?.workspaceAgentId).toBe(AGENT_ID);
    // ...and NOT as the inline notice, which belongs to loads and toggles.
    expect(collectVnodes(rerender()).some((vnode) => attrsOf(vnode).id === "ws-perm-error")).toBe(false);

    const actions = dialog?.actions ?? [];
    expect(actions.map((action) => action.label)).toEqual(["Try again"]);
    const signIns = (): number => requests.filter((request) => request.url.endsWith("/connect-browser")).length;
    const signInsBefore = signIns();
    actions[0]?.onclick();
    await settleConnect();
    expect(signIns()).toBe(signInsBefore + 1);
  });
});

describe("PermissionsTab experimental connectors", () => {
  function section(node: unknown): AnyVnode {
    return withAttr(node, "data-perm-experimental-connectors")[0];
  }

  function connectNames(node: unknown): unknown[] {
    return withAttr(node, "data-perm-connect").map((row) => attrsOf(row)["data-perm-connect"]);
  }

  it("lists an experimental service under its own heading, apart from the catalog", async () => {
    const { root } = await render(
      permissionsView({ available_connections: [browserAvailable("notion", "Notion"), stravaAvailable()] }),
      { requestedSection: "add-connection" },
    );

    const panel = withAttr(root, "data-perm-panel")[0];
    const text = allText(panel);
    expect(text.indexOf("Notion")).toBeLessThan(text.indexOf("Experimental connectors"));
    expect(text.indexOf("Experimental connectors")).toBeLessThan(text.indexOf("Strava"));
    const risk = withAttr(renderDeep(section(root)), EXPERIMENTAL_CONNECTOR_RISK_ATTR)[0];
    expect(collectText(risk)).toEqual(["Use at your own risk.", " ", "See more"]);
    // Connected like any other service, with the service's own mark on the row.
    expect(connectNames(section(root))).toEqual(["strava"]);
    expect(markSources(marks(section(root))[0])).toEqual(["/_static/service_icons/strava.svg"]);
    expect(connectNames(root)).toEqual(["slack", "notion", "strava"]);
  });

  it("says what the risk is once See more is pressed, and puts it away when pressed again", async () => {
    const { root, rerender } = await render(permissionsView({ available_connections: [stravaAvailable()] }), {
      requestedSection: "add-connection",
    });
    const RISK_ID = "ws-perm-experimental-risk";
    const riskOf = (node: unknown): AnyVnode => withAttr(renderDeep(node), EXPERIMENTAL_CONNECTOR_RISK_ATTR)[0];

    const shut = riskOf(root);
    expect(attrsOf(collapsibleTrigger(shut, RISK_ID))["aria-expanded"]).toBe("false");
    expect(allText(shut)).not.toContain(EXPERIMENTAL_CONNECTOR_RISK_DETAIL);
    // The detail is reached by the press alone: no tooltip repeats it.
    expect(withAttr(shut, "data-tooltip")).toEqual([]);

    pressCollapsible(shut, RISK_ID);
    const opened = riskOf(rerender());
    expect(attrsOf(collapsibleTrigger(opened, RISK_ID))["aria-expanded"]).toBe("true");
    const detail = required(byId(opened, panelIdFor(RISK_ID)));
    expect(collectText(detail)).toEqual([EXPERIMENTAL_CONNECTOR_RISK_DETAIL]);

    pressCollapsible(opened, RISK_ID);
    expect(allText(riskOf(rerender()))).not.toContain(EXPERIMENTAL_CONNECTOR_RISK_DETAIL);
  });

  it("is left out when nothing experimental is on offer, and out of every other section", async () => {
    const addConnection = await render(permissionsView(), { requestedSection: "add-connection" });
    expect(withAttr(addConnection.root, "data-perm-experimental-connectors")).toEqual([]);
    expect(allText(withAttr(addConnection.root, "data-perm-panel")[0])).not.toContain("Experimental");

    const elsewhere = await render(permissionsView({ available_connections: [stravaAvailable()] }));
    expect(withAttr(elsewhere.root, "data-perm-experimental-connectors")).toEqual([]);
  });

  it("says that the catalog is exhausted only of the ordinary services", async () => {
    const { root } = await render(permissionsView({ available_connections: [stravaAvailable()] }), {
      requestedSection: "add-connection",
    });
    const text = allText(withAttr(root, "data-perm-panel")[0]);
    expect(text).toContain("Every available service already has an account connected.");
    expect(text).toContain("Strava");
  });

  it("marks a connected experimental service's panel, and nobody else's", async () => {
    const strava = slackConnection({ service_name: "strava", display_name: "Strava", is_experimental: true });
    const { root } = await render(permissionsView({ connections: [slackConnection(), strava] }), {
      requestedSection: "conn:strava:",
    });
    const badge = withAttr(root, "data-perm-experimental-badge")[0];
    expect(allText(badge)).toBe("Experimental");
    // The delegated [data-tooltip] chrome, not a native title: the two would
    // otherwise show two tooltips on the same badge.
    expect(attrsOf(badge)["data-tooltip"]).toBe(EXPERIMENTAL_CONNECTOR_RISK_DETAIL);
    expect(attrsOf(badge).title).toBeUndefined();

    const slack = await render(permissionsView());
    expect(withAttr(slack.root, "data-perm-experimental-badge")).toEqual([]);
  });

  it("offers a second account for an experimental service with the connected ones", async () => {
    const strava = slackConnection({ service_name: "strava", display_name: "Strava", is_experimental: true });
    const { root } = await render(permissionsView({ connections: [strava], available_connections: [] }), {
      requestedSection: "add-connection",
    });
    expect(connectNames(root)).toEqual(["strava"]);
    expect(withAttr(root, "data-perm-experimental-connectors")).toEqual([]);
  });
});

describe("PermissionsTab service marks", () => {
  const navMark = (result: RenderResult): AnyVnode => marks(withAttr(result.nav(), "data-perm-nav")[0])[0];
  const headingMark = (result: RenderResult): AnyVnode => marks(withAttr(result.root, "data-perm-panel")[0])[0];

  it("draws a connected service's own logo, as an image", async () => {
    // An <img> and not a masked box: a mask keeps only the artwork's alpha, so
    // it could never show a full-color logo.
    const result = await render(permissionsView());
    for (const mark of [navMark(result), headingMark(result)]) {
      expect(markSources(mark)).toEqual(["/_static/service_icons/slack.svg"]);
      expect(hasClass(mark, "service-mark-muted")).toBe(false);
    }
  });

  it("carries a second image only for a mark that vanishes on the dark surface", async () => {
    // GitHub's logo is near-black and disappears on the dark theme's surface,
    // so the vendor's own white variant ships beside it and CSS picks between
    // the two. A brand that reads on both keeps a single image, since the
    // alternative would be recoloring artwork we are not allowed to alter.
    const github = await render(
      permissionsView({ connections: [slackConnection({ service_name: "github", display_name: "GitHub" })] }),
    );
    const images = markImages(navMark(github));
    expect(images.map((img) => String(attrsOf(img).src))).toEqual([
      "/_static/service_icons/github.svg",
      "/_static/service_icons/github-on-dark.svg",
    ]);
    expect(hasClass(images[0], "on-light-surface")).toBe(true);
    expect(hasClass(images[1], "on-dark-surface")).toBe(true);

    const legible = await render(
      permissionsView({ connections: [slackConnection({ service_name: "gitlab", display_name: "GitLab" })] }),
    );
    expect(markSources(navMark(legible))).toEqual(["/_static/service_icons/gitlab.svg"]);
  });

  it("drains the same logo once the account is not connected", async () => {
    // The mark stays the service's own artwork -- only the wrapper changes, and
    // the CSS behind it greys and dims what is already there.
    const result = await render(permissionsView({ connections: [slackConnection({ is_connected: false })] }));
    for (const mark of [navMark(result), headingMark(result)]) {
      expect(hasClass(mark, "service-mark-muted")).toBe(true);
      expect(markSources(mark)).toEqual(["/_static/service_icons/slack.svg"]);
    }
  });

  it("drains the marks of services that have no account yet", async () => {
    const { root } = await render(
      permissionsView({
        available_connections: [browserAvailable("gitlab", "GitLab")],
      }),
      { requestedSection: "add-connection" },
    );
    // The catalog's own mark, not the connection panel's: both panels render,
    // so it is found by the service it draws rather than by panel order.
    // "Connect a new service" lists what this machine has no account on, so
    // its marks read grey for the same reason a disconnected nav row does --
    // color is what says "this service is connected here".
    const catalogMark = marks(root).find((mark) => markSources(mark).some((src) => src.includes("gitlab.svg")));
    expect(catalogMark).toBeDefined();
    expect(hasClass(catalogMark as AnyVnode, "service-mark-muted")).toBe(true);
  });

  it("draws the fallback glyph once a service's mark 404s", async () => {
    // The visible image is its own load probe now, so the mask's separate
    // hidden <img> is gone.
    const result = await render(permissionsView());
    const probe = markImages(navMark(result))[0];
    expect(attrsOf(probe).src).toBe("/_static/service_icons/slack.svg");

    (attrsOf(probe).onerror as () => void)();
    const nav = withAttr(result.nav(), "data-perm-nav")[0];

    expect(marks(nav)).toHaveLength(0);
    // A globe, not the box "Local files" draws further down the same nav: a
    // service with no usable mark should not read as the same kind of entry.
    expect(collectVnodes(nav).some((node) => attrsOf(node).name === "globe")).toBe(true);
  });
});

describe("PermissionsTab client permissions", () => {
  const SECTION = "client-permissions";

  function radios(node: unknown): AnyVnode[] {
    return collectVnodes(node).filter((vnode) => vnode.tag === "input" && attrsOf(vnode).type === "radio");
  }

  /** Each radio's label and whether it is checked, in the order drawn. */
  function choices(node: unknown): { label: string; isChecked: boolean }[] {
    return withAttr(node, "data-client-permission-choice").map((choice) => ({
      label: allText(choice),
      isChecked: attrsOf(radios(choice)[0]).checked === true,
    }));
  }

  function choose(node: unknown, choice: string): void {
    const label = withAttr(node, "data-client-permission-choice").find(
      (vnode) => attrsOf(vnode)["data-client-permission-choice"] === choice,
    );
    (attrsOf(radios(label)[0]).onchange as () => void)();
  }

  it("is a ?section the pane and the rail honour", async () => {
    const { root, nav } = await render(permissionsView(), { requestedSection: SECTION });
    const pressed = withAttr(nav(), "data-perm-nav")
      .filter((node) => attrsOf(node)["aria-pressed"] === "true")
      .map((node) => attrsOf(node)["data-perm-nav"]);
    expect(pressed).toEqual([SECTION]);
    expect(withAttr(root, "data-perm-panel").map((node) => attrsOf(node)["data-perm-panel"])).toEqual([SECTION]);
  });

  it("says the answers stay on this computer and draws a row per permission", async () => {
    const { root } = await render(permissionsView(), { requestedSection: SECTION });
    const panel = withAttr(root, "data-perm-panel").find((node) => attrsOf(node)["data-perm-panel"] === SECTION);
    const text = allText(panel);
    expect(text).toContain("Client permissions");
    expect(text).toContain(
      "Persistent permissions you grant this workspace that apply only on this computer. Imbue Studio asks " +
        "before a page of the workspace uses one. Your other computers keep their own answers, and the " +
        "workspace itself never sees them.",
    );
    const rows = withAttr(root, "data-client-permission");
    expect(rows.map((row) => attrsOf(row)["data-client-permission"])).toEqual(["microphone"]);
    expect(allText(rows[0])).toContain("Microphone");
    expect(allText(rows[0])).toContain("Listen to an audio input");
    const group = collectVnodes(rows[0]).find((vnode) => attrsOf(vnode).role === "radiogroup");
    expect(attrsOf(group as AnyVnode)["aria-label"]).toBe("Microphone");
  });

  it.each([
    [null, "Use the default (Ask)"],
    ["ask", "Ask"],
    ["granted", "Allow"],
    ["denied", "Don't allow"],
  ] as const)("checks the radio for a stored %j", async (stored, checkedLabel) => {
    const row =
      stored === null ? microphoneRow() : microphoneRow({ stored, decision: stored, source: "workspace" });
    const { root } = await render(permissionsView(), {
      requestedSection: SECTION,
      clientPermissions: clientPermissionsView([row]),
    });
    const drawn = choices(root);
    expect(drawn.map((choice) => choice.label)).toEqual(["Use the default (Ask)", "Ask", "Allow", "Don't allow"]);
    expect(drawn.filter((choice) => choice.isChecked).map((choice) => choice.label)).toEqual([checkedLabel]);
  });

  it.each([
    ["default", null],
    ["ask", "ask"],
    ["granted", "granted"],
    ["denied", "denied"],
  ] as const)("writes %s as %j and shows what the server answered", async (choice, decision) => {
    // Start from a state other than the one chosen, so every choice is a change.
    const startingStored = decision === "denied" ? "granted" : "denied";
    const { root, rerender, clientRequests } = await render(permissionsView(), {
      requestedSection: SECTION,
      clientPermissions: clientPermissionsView([
        microphoneRow({ stored: startingStored, decision: startingStored, source: "workspace" }),
      ]),
    });

    choose(root, choice);
    await settle();

    expect(clientRequests.at(-1)).toEqual({
      url: `${CLIENT_PERMISSIONS_URL}/microphone`,
      method: "PUT",
      body: { decision },
    });
    const checked = choices(rerender()).filter((drawn) => drawn.isChecked);
    expect(checked).toHaveLength(1);
    expect(checked[0].label).toBe(
      { default: "Use the default (Ask)", ask: "Ask", granted: "Allow", denied: "Don't allow" }[choice],
    );
  });

  it("locks every radio and spins while a write is in flight", async () => {
    let release: () => void = () => undefined;
    const { root, rerender } = await render(permissionsView(), {
      requestedSection: SECTION,
      respondClientPermissions: (_url, init) =>
        init?.method === "PUT"
          ? new Promise((resolve) => {
              release = () => resolve({ ok: true, status: 200, body: clientPermissionsView() });
            })
          : null,
    });

    choose(root, "granted");
    const locked = rerender();
    expect(radios(locked).map((radio) => attrsOf(radio).disabled)).toEqual([true, true, true, true]);
    expect(spinners(locked)).toHaveLength(1);

    release();
    await settle();
    expect(radios(rerender()).map((radio) => attrsOf(radio).disabled)).toEqual([false, false, false, false]);
  });

  it("keeps the selection and says why when a write is refused", async () => {
    const { root, rerender } = await render(permissionsView(), {
      requestedSection: SECTION,
      respondClientPermissions: (_url, init) =>
        init?.method === "PUT"
          ? Promise.resolve({ ok: false, status: 409, body: { error: "saved by a newer version" } })
          : null,
    });

    choose(root, "granted");
    await settle();

    const after = rerender();
    expect(allText(byId(after, "client-permissions-error"))).toContain(
      "Could not save the change: saved by a newer version",
    );
    expect(choices(after).filter((choice) => choice.isChecked).map((choice) => choice.label)).toEqual([
      "Use the default (Ask)",
    ]);
  });

  it("offers a retry when its own read fails, whatever the rest of the pane did", async () => {
    const { root } = await render(permissionsView(), {
      requestedSection: SECTION,
      respondClientPermissions: () => Promise.resolve({ ok: false, status: 500, body: { error: "store is unreadable" } }),
    });
    expect(allText(byId(root, "client-permissions-load-failed"))).toContain("store is unreadable");
    expect(allText(root)).toContain("Try again");
    expect(radios(root)).toHaveLength(0);
  });

  it("renders when the gateway behind the other sections cannot be reached", async () => {
    const unavailable = permissionsView({ connections: [], available_connections: [], permissions_unavailable: true });
    const clientSection = await render(unavailable, { requestedSection: SECTION });
    expect(allText(withAttr(clientSection.nav(), "data-perm-nav"))).toContain("Client permissions");
    expect(withAttr(clientSection.root, "data-client-permission")).toHaveLength(1);
    expect(radios(clientSection.root)).toHaveLength(4);
    expect(errorNotices(clientSection.root).has("ws-perm-unavailable")).toBe(false);

    const localFiles = await render(unavailable, { requestedSection: "local-files" });
    expect(errorNotices(localFiles.root).get("ws-perm-unavailable")?.error.message).toBe(
      "Permissions can't be loaded right now. Try again in a moment.",
    );
    expect(radios(localFiles.root)).toHaveLength(0);
  });
});

describe("PermissionsTab shared paths", () => {
  function renderLocalFiles(
    view: UiWorkspacePermissions,
    options: Parameters<typeof render>[1] = {},
  ): Promise<RenderResult> {
    return render(view, { ...options, requestedSection: "local-files" });
  }

  const pathRows = (node: unknown): AnyVnode[] => withAttr(node, "data-shared-path");
  const syncBoxes = (node: unknown): AnyVnode[] => withAttr(node, "data-sync-path");
  const accessSelects = (node: unknown): AnyVnode[] => withAttr(node, "data-access-path");
  const syncPanels = (node: unknown): AnyVnode[] => withAttr(node, "data-sync-detail");
  const removeButtons = (node: unknown): AnyVnode[] => withAttr(node, "data-remove-path");
  const conflictSelects = (node: unknown): AnyVnode[] => withAttr(node, "data-conflict-path");

  it("spins while a change of access is on its way to the machine", async () => {
    // Every other control in the pane does this; these two did not, which read
    // as the change having landed the moment it was clicked. The write is not
    // done until the workspace's own machine has taken it, so it has not.
    // A no-op to begin with rather than null: it is only ever replaced inside
    // the promise executor, which the compiler cannot see run.
    let releaseWrite: () => void = () => {};
    const { root, rerender, remove, model } = await render(
      permissionsView({ shared_paths: [sharedPath()] }),
      {
        requestedSection: "local-files",
        respond: (url) =>
          url.endsWith("/shared-path")
            ? new Promise((resolve) => {
                releaseWrite = () => resolve({ ok: true, status: 200, body: permissionsView() });
              })
            : null,
      },
    );
    expect(spinners(root)).toHaveLength(0);

    // Changing the access: the round trip that writes the new grant.
    const accessWrite = model.setSharedPath("/home/me/notes", "WRITE");
    await settle();
    const duringAccess = rerender();
    expect(spinners(duringAccess)).toHaveLength(1);
    expect(attrsOf(accessSelects(duringAccess)[0]).disabled).toBe(true);
    // And looks it. A bare `<select disabled>` stops responding but goes on
    // looking exactly as it did, which reads as a click that failed to land.
    expect(classesOf(accessSelects(duringAccess)[0])).toContain("disabled:opacity-40");
    releaseWrite();
    await accessWrite;

    // Adding a path makes the same write under its own busy key, since it has
    // no row to spin on until the write comes back with one. Not asserted on
    // the buttons here: they are drawn only in the desktop app, and this suite
    // renders to vnodes with no DOM, so `electronBridge.isDesktop` is false
    // and the whole affordance is absent. The key is what the buttons read.
    const addWrite = model.addSharedPath("/home/me/other");
    await settle();
    expect(model.isRowBusy(ADD_SHARED_PATH_ROW_KEY)).toBe(true);
    releaseWrite();
    await addWrite;
    expect(model.isRowBusy(ADD_SHARED_PATH_ROW_KEY)).toBe(false);

    remove();
  });

  it("gives every shared path an access dropdown and a remove button", async () => {
    const { root, remove } = await renderLocalFiles(permissionsView({ shared_paths: [sharedPath()] }));
    expect(pathRows(root)).toHaveLength(1);
    expect(accessSelects(root)).toHaveLength(1);
    expect(removeButtons(root)).toHaveLength(1);
    const text = allText(root);
    expect(text).toContain("~/notes");
    expect(text).toContain("Agents in this workspace may");
    expect(text).toContain("Revoke access");
    remove();
  });

  it("shows the whole path in a hover tooltip, since the pill cuts a long one short", async () => {
    const { root, remove } = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({ path: "/home/me/notes", path_label: "~/notes" }),
        ],
      }),
    );
    const pills = collectVnodes(pathRows(root)[0]).filter(
      (node) => node.tag === "code",
    );
    expect(pills).toHaveLength(1);
    expect(attrsOf(pills[0])["data-tooltip"]).toBe("/home/me/notes");
    remove();
  });

  it("says on the remove button when removing would take files with it", async () => {
    // The copy goes with the grant, and this is the only place that can be
    // said in every case -- a path whose copy is gone draws no notice at all.
    const noCopy = await renderLocalFiles(permissionsView({ shared_paths: [sharedPath()] }));
    expect(allText(noCopy.root)).not.toContain("remove copy");
    noCopy.remove();

    const running = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ activity: "ACTIVE" }) })] }),
    );
    expect(allText(running.root)).toContain("Revoke access and remove copy");
    running.remove();

    const setAside = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ sync: pathSync({ state: "STOPPED", activity: "INACTIVE" }) })],
      }),
    );
    expect(allText(setAside.root)).toContain("Revoke access and remove copy");
    setAside.remove();

    const discarded = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ sync: pathSync({ state: "STOPPED", activity: "DISCARDED" }) })],
      }),
    );
    expect(allText(discarded.root)).not.toContain("remove copy");
    discarded.remove();
  });

  it("says what syncing buys, in the terms the user cares about", async () => {
    // "Keep in sync" said nothing about why anyone would want it.
    const { root, remove } = await renderLocalFiles(permissionsView({ shared_paths: [sharedPath()] }));
    const text = allText(root);
    expect(text).toContain("Keep a synchronized copy in the workspace");
    expect(text).toContain("when Imbue Studio is not running or your computer is offline");
    remove();
  });

  it("offers the clash rule only to a sync that can actually have a clash", async () => {
    // Only a two-way sync can, and only read-and-write access makes it two-way.
    const readOnly = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ access: "READ", sync: pathSync() })] }),
    );
    expect(syncPanels(readOnly.root)).toHaveLength(1);
    expect(conflictSelects(readOnly.root)).toHaveLength(0);
    readOnly.remove();

    const readWrite = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ access: "WRITE", sync: pathSync() })] }),
    );
    expect(conflictSelects(readWrite.root)).toHaveLength(1);
    readWrite.remove();
  });

  it("never hides the status of a sync the user believes is on", async () => {
    // A folder the user still wants synced, with nothing running, is the app
    // saying one thing and doing another -- which is UNKNOWN, not STOPPED, and
    // must not be silent the way a settled-off row is.
    const believedOn = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ sync: pathSync({ state: "UNKNOWN", activity: "ACTIVE" }) })],
      }),
    );
    expect(allText(believedOn.root)).toContain("Not running");
    believedOn.remove();
  });

  it("swaps Try again for the sync switch when the app no longer holds the folder's sync", async () => {
    const failed = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({ sync: pathSync({ state: "FAILED", activity: "ACTIVE", message: "machine is asleep" }) }),
        ],
      }),
      {
        respond: (url) =>
          url.endsWith("/folder-syncs/retry")
            ? Promise.resolve({
                ok: false,
                status: 409,
                body: {
                  error: "This folder isn't set up to sync any more. Turn syncing on for it again.",
                  reason: "NO_SYNC_RECORD",
                },
              })
            : null,
      },
    );
    const retry = withAttr(failed.root, "data-sync-retry-path")[0];
    (attrsOf(retry).onclick as () => void)();
    await settle();

    const after = failed.rerender();
    expect(withAttr(after, "data-sync-retry-path")).toHaveLength(0);
    expect(allText(renderDeep(after))).toContain("This folder isn't set up to sync any more. Turn syncing on for it again.");
    expect(isPathSyncOn((failed.model.data?.shared_paths ?? [])[0])).toBe(false);
    failed.remove();
  });

  it("marks a refused sync retry's notice, so it reads as a failure rather than red prose", async () => {
    const failed = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({ sync: pathSync({ state: "FAILED", activity: "ACTIVE", message: "machine is asleep" }) }),
        ],
      }),
      {
        respond: (url) =>
          url.endsWith("/folder-syncs/retry")
            ? Promise.resolve({ ok: false, status: 503, body: { error: "the workspace is asleep" } })
            : null,
      },
    );
    (attrsOf(withAttr(failed.root, "data-sync-retry-path")[0]).onclick as () => void)();
    await settle();

    const notice = renderDeep(collectVnodes(failed.rerender()).find((vnode) => attrsOf(vnode).id === "ws-perm-error"));
    expect(allText(notice)).toContain("the workspace is asleep");
    expect(errorMarks(notice)).toHaveLength(1);
    failed.remove();
  });

  it("offers to try a failed sync again, and says why it failed", async () => {
    const failed = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({ sync: pathSync({ state: "FAILED", activity: "ACTIVE", message: "machine is asleep" }) }),
        ],
      }),
    );
    const text = allText(renderDeep(failed.root));
    expect(text).toContain("machine is asleep");
    expect(text).toContain("Try again");
    expect(withAttr(failed.root, "data-sync-retry-path")).toHaveLength(1);
    failed.remove();

    // Nothing to retry on a sync that is merely working.
    const running = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync() })] }),
    );
    expect(withAttr(running.root, "data-sync-retry-path")).toHaveLength(0);
    running.remove();
  });

  it("badges work in flight and says nothing at all about a sync that is simply off", async () => {
    // The unticked checkbox already says the sync is off; a "Stopped" badge
    // beside it only asks the reader to reconcile the two.
    const stopped = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ state: "STOPPED", activity: "INACTIVE" }) })] }),
    );
    expect(allText(stopped.root)).not.toContain("Stopped");
    stopped.remove();

    const restarting = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ state: "RESTARTING" }) })] }),
    );
    expect(allText(restarting.root)).toContain("Restarting");
    restarting.remove();

    const stopping = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ state: "DEACTIVATING" }) })] }),
    );
    expect(allText(stopping.root)).toContain("Stopping");
    stopping.remove();
  });

  it("warns about another workspace's sync only while this folder is syncing", async () => {
    // It describes what two running syncs do to each other, which is not yet
    // true of a folder whose copy is not being kept.
    const warning = "This folder is also synced with another workspace.";
    const synced = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ sync: pathSync(), sync_overlap_warning: warning })],
      }),
    );
    expect(allText(synced.root)).toContain(warning);
    synced.remove();

    const off = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync_overlap_warning: warning })] }),
    );
    expect(allText(off.root)).not.toContain(warning);
    off.remove();
  });

  it("states which way changes travel, from the access rather than as a second choice", async () => {
    const readOnly = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ access: "READ", sync: pathSync() })],
      }),
    );
    expect(allText(readOnly.root)).toContain(
      "changes from your computer to this workspace in one direction",
    );
    readOnly.remove();

    const readWrite = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ access: "WRITE", sync: pathSync() })] }),
    );
    expect(allText(readWrite.root)).toContain("in both directions");
    readWrite.remove();
  });

  it("offers a way in for a file and for a folder, and names the other route", async () => {
    // The pickers only exist in the desktop shell, so the buttons only do.
    const surface = globalThis as unknown as {
      window?: { mindsNative?: unknown };
    };
    const restore = surface.window;
    surface.window = { mindsNative: {} };
    const { root, remove } = await renderLocalFiles(permissionsView());
    const text = allText(root);
    expect(text).toContain("Add file");
    expect(text).toContain("Add folder");
    expect(text).toContain("Or tell an agent to request access");
    remove();
    surface.window = restore;
  });

  it("greys out syncing for a folder that cannot have it, and says why", async () => {
    // The reason comes from the server, which answers it with the same checks
    // the click would have been refused by -- so the row can never grey out a
    // folder that would have worked, or offer one that would not.
    const { root, remove } = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({
            sync_unavailable_reason: "Only folders can be synced, and /home/me/notes is a file.",
          }),
        ],
      }),
    );

    // Still there, so the row reads the same as every other; just not usable.
    expect(syncBoxes(root)).toHaveLength(1);
    expect(attrsOf(syncBoxes(root)[0]).disabled).toBe(true);
    const text = allText(root);
    expect(text).toContain("Only folders can be synced");
    // The reason stands in for the explanation rather than sitting beside it.
    expect(text).not.toContain("agents can continue to access");
    remove();
  });

  it("offers syncing normally when nothing stands in the way", async () => {
    const { root, remove } = await renderLocalFiles(permissionsView({ shared_paths: [sharedPath()] }));
    expect(attrsOf(syncBoxes(root)[0]).disabled).toBeFalsy();
    expect(allText(root)).toContain("agents can continue to access");
    remove();
  });

  it("shows turning arrows only while bytes move, and a check once they have", async () => {
    const iconNames = (node: unknown): string[] =>
      collectVnodes(node)
        .map((vnode) => attrsOf(vnode).name)
        .filter((name): name is string => typeof name === "string");

    const moving = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ state: "SYNCING" }) })] }),
    );
    expect(allText(moving.root)).toContain("Syncing");
    expect(iconNames(moving.root)).toContain("arrows-sync");
    moving.remove();

    const settled = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ state: "SYNCED" }) })] }),
    );
    expect(allText(settled.root)).toContain("Synced");
    expect(iconNames(settled.root)).toContain("badge-check");
    expect(iconNames(settled.root)).not.toContain("arrows-sync");
    settled.remove();
  });

  it("says how far through a transfer is, from unison's own count", async () => {
    const { root, remove } = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({
            sync: pathSync({ state: "SYNCING", bytes_done: 12 * 1024 ** 2, bytes_total: 17 * 1024 ** 2 }),
          }),
        ],
      }),
    );
    expect(allText(root)).toContain("12.0 MB of 17.0 MB");
    remove();
  });

  it("says only that it is syncing when unison gave no count", async () => {
    const { root, remove } = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ state: "SYNCING" }) })] }),
    );
    const text = allText(root);
    expect(text).toContain("Syncing");
    expect(text).not.toContain(" of ");
    remove();
  });

  it("hides the sync settings until syncing is on", async () => {
    const off = await renderLocalFiles(permissionsView({ shared_paths: [sharedPath()] }));
    expect(conflictSelects(off.root)).toHaveLength(0);
    expect(conflictSelects(off.root)).toHaveLength(0);
    off.remove();

    // A clash is only possible when the agent may write, which is the same
    // condition that makes the sync two-way.
    const readOnly = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ access: "READ", sync: pathSync() })] }),
    );
    expect(conflictSelects(readOnly.root)).toHaveLength(0);
    readOnly.remove();

    const on = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ access: "WRITE", sync: pathSync() })] }),
    );
    expect(conflictSelects(on.root)).toHaveLength(1);
    on.remove();
  });

  it("offers to delete the copy the machine kept when syncing was turned off", async () => {
    const setAside = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ sync: pathSync({ activity: "INACTIVE", state: "STOPPED" }) })],
      }),
    );
    const text = allText(setAside.root);
    expect(text).toContain("A copy is still in the workspace");
    expect(text).toContain("Remove copy");
    // Not the settings: those belong to a sync that is running.
    expect(conflictSelects(setAside.root)).toHaveLength(0);
    // Quieter than the row's own Remove, which takes the whole path away:
    // two filled reds in one card read as two equal warnings.
    const discard = withAttr(setAside.root, "data-discard-copy-path")[0];
    expect(attrsOf(discard).variant).toBe("danger-soft");
    expect(attrsOf(removeButtons(setAside.root)[0]).variant).toBe("danger");
    setAside.remove();
  });

  it("offers nothing to delete once the copy is gone, or while the sync is running", async () => {
    const discarded = await renderLocalFiles(
      permissionsView({
        shared_paths: [sharedPath({ sync: pathSync({ activity: "DISCARDED", state: "STOPPED" }) })],
      }),
    );
    expect(allText(discarded.root)).not.toContain("Remove copy");
    discarded.remove();

    const running = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync() })] }),
    );
    expect(allText(running.root)).not.toContain("Remove copy");
    running.remove();
  });

  it("drops the clash question from a one-way sync, which cannot have one", async () => {
    const { root, remove } = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath({ sync: pathSync({ direction: "TO_WORKSPACE" }) })] }),
    );
    expect(conflictSelects(root)).toHaveLength(0);
    remove();
  });

  it("offers no sync control at all when this build cannot run one", async () => {
    const { root, remove } = await renderLocalFiles(
      permissionsView({ shared_paths: [sharedPath()], is_sync_supported: false }),
    );
    expect(pathRows(root)).toHaveLength(1);
    expect(syncBoxes(root)).toHaveLength(0);
    expect(allText(root)).not.toContain("Keep a copy in the workspace");
    remove();
  });

  it("says why a sync failed, and leaves its switch where the user set it", async () => {
    const { root, remove } = await renderLocalFiles(
      permissionsView({
        shared_paths: [
          sharedPath({ sync: pathSync({ state: "FAILED", message: "Agent directory does not exist: /w/notes" }) }),
        ],
      }),
    );
    const text = allText(renderDeep(pathRows(root)[0]));
    expect(text).toContain("Failed");
    expect(text).toContain("Agent directory does not exist");
    // The user still wants this folder synced; the failure is news about it,
    // not a reason to move the switch they set.
    expect(syncBoxes(root)[0].attrs?.["aria-checked"]).toBe("true");
    remove();
  });

  it("posts the removal when Remove is pressed", async () => {
    const row = sharedPath();
    const { root, requests, remove } = await renderLocalFiles(permissionsView({ shared_paths: [row] }));
    (removeButtons(root)[0].attrs?.onclick as () => void)();
    await settle();
    expect(requests[requests.length - 1]).toEqual({
      url: `/ui/api/workspaces/${AGENT_ID}/permissions/shared-path-remove`,
      body: { path: row.path },
    });
    remove();
  });

  it("posts the path when its sync switch is flipped on", async () => {
    const row = sharedPath();
    const { root, requests, remove } = await renderLocalFiles(permissionsView({ shared_paths: [row] }));
    (syncBoxes(root)[0].attrs?.onclick as () => void)();
    await settle();
    expect(requests[requests.length - 1]).toEqual({
      url: `/ui/api/workspaces/${AGENT_ID}/folder-syncs/toggle`,
      body: { path: row.path, enabled: true, conflict: "NEWER" },
    });
    remove();
  });
});

describe("the This computer panel", () => {
  const thisComputerGrant = {
    permission: "remote-execution-host-3f9c",
    label: "This computer",
    detail: "",
    description: "Agents in this workspace may run any command there, as you.",
    is_granted: true,
    can_enable: false,
  };

  it("says so when no computer has been granted", async () => {
    const { root } = await render(permissionsView(), { requestedSection: "this-computer" });
    expect(allText(root)).toContain("Agents in this workspace can't run commands on your computers.");
  });

  it("lists each granted computer with a switch that can only be turned off", async () => {
    const { root, requests } = await render(
      permissionsView({ remote_execution_toggles: [thisComputerGrant], is_remote_execution_enabled: true }),
      { requestedSection: "this-computer" },
    );
    expect(allText(root)).toContain("This computer");
    expect(collectVnodes(root).some((vnode) => attrsOf(vnode).id === "remote-execution-disabled-notice")).toBe(false);
    const control = switches(root)[0];
    expect(attrsOf(control)["aria-checked"]).toBe("true");

    (attrsOf(control).onclick as () => void)();
    await settle();

    const flip = requests.find((request) => request.url.endsWith("/permissions/self-toggle"));
    expect(flip?.body).toEqual({ permission: "remote-execution-host-3f9c", enabled: false });
  });

  it("says the grants are inert while this device's switch is off", async () => {
    const { root } = await render(
      permissionsView({ remote_execution_toggles: [thisComputerGrant], is_remote_execution_enabled: false }),
      { requestedSection: "this-computer" },
    );
    expect(collectVnodes(root).some((vnode) => attrsOf(vnode).id === "remote-execution-disabled-notice")).toBe(true);
    expect(allText(root)).toContain("Settings > This computer");
  });
});

describe("PermissionsTab implicit permissions", () => {
  const NOTE = "Granted along with anything below: Open and maintain an MCP session.";

  function hints(node: unknown): AnyVnode[] {
    return withAttr(node, IMPLICIT_PERMISSIONS_HINT_ATTR);
  }

  function scopePanel(note: string, scope = "notion-mcp-api", heading = "Notion (MCP)") {
    return {
      scope,
      heading,
      implicit_note: note,
      groups: [
        {
          heading: "Full access",
          toggles: [{ permission: `${scope}-read-all`, label: "Read everything", description: "", is_granted: true }],
        },
      ],
    };
  }

  it("draws nothing for a connection whose grants carry no handshake", async () => {
    const { root } = await render(permissionsView());
    expect(hints(root)).toHaveLength(0);
  });

  it("hangs the note on the intro of a single-scope connection, as a tooltip rather than a row", async () => {
    const { root } = await render(
      permissionsView({ connections: [slackConnection({ scopes: [scopePanel(NOTE)] })] }),
    );
    const found = hints(root);
    expect(found).toHaveLength(1);
    expect(attrsOf(found[0])["data-tooltip"]).toBe(NOTE);
    expect(allText(root)).not.toContain(NOTE);
    expect(switches(root)).toHaveLength(1);
  });

  it("hangs each scope's note on that scope's heading when a connection has several", async () => {
    const { root } = await render(
      permissionsView({
        connections: [
          slackConnection({
            scopes: [scopePanel(NOTE, "fastmail-api", "Fastmail"), scopePanel("", "fastmail-dav-api", "Fastmail (DAV)")],
          }),
        ],
      }),
    );
    const found = hints(root);
    expect(found).toHaveLength(1);
    const heading = collectVnodes(root).find(
      (node) => node.tag === "h3" && allText(node).includes("Fastmail") && !allText(node).includes("DAV"),
    ) as AnyVnode;
    expect(hints(heading)).toHaveLength(1);
  });
});
