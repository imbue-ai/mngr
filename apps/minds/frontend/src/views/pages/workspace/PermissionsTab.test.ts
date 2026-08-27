import m from "mithril";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { settle } from "../../../testing";
import {
  BROWSER_SIGN_IN,
  awsAvailable,
  credentialsSignIn,
  permissionsView,
  slackConnection,
} from "../../../models/workspacePermissions.testing";
import type { UiWorkspacePermissions } from "../../../generated/ui";
import { OPTIONS_TABS, WorkspaceOptionsModel, toOptionsTab } from "../../../models/workspaceOptions";
import { PermissionsModel } from "../../../models/workspacePermissions";
import { classifyRoute } from "../../shell/classify";
import { PermissionsTab } from "./PermissionsTab";
import { OptionsPanel } from "./OptionsPanel";
import { TITLEBAR_POPUP_ICONS } from "../../shell/RaisedTitlebarIcons";
import { forgetFailedServiceMarks } from "../../components/ServiceMark";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, classesOf, collectVnodes, withAttr } from "../../../testing";

const AGENT_ID = "agent-" + "c".repeat(8);

// Which marks 404'd is remembered app-wide, on purpose. Without this the
// test that exercises a missing mark would retire that service for every
// test after it in this file, making the suite order-dependent.
beforeEach(forgetFailedServiceMarks);

/** Copy carried by the disconnect confirm alone -- the panel's own Disconnect
 * block says something similar, so the dialog has to be read on its own. Empty
 * while the dialog is closed, since a closed one contributes nothing to the
 * tree. */
function disconnectDialogText(node: unknown): string {
  const dialog = collectVnodes(node).find((vnode) => attrsOf(vnode).id === "ws-perm-disconnect");
  return dialog === undefined ? "" : allText(dialog.children);
}

function switches(node: unknown): AnyVnode[] {
  return collectVnodes(node).filter((vnode) => classesOf(vnode).includes("perm-switch"));
}

function hasClass(vnode: AnyVnode, name: string): boolean {
  return classesOf(vnode).split(/\s+/).includes(name);
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
  /** Run the component's onremove, for a test that armed something with a
   * timer behind it -- an armed "Revoke all" outlives the test otherwise. */
  remove: () => void;
  model: PermissionsModel;
  selectedSections: string[];
  /** The request ids the pane asked to open on their own page. */
  reviewedRequests: string[];
  requests: { url: string; body: unknown }[];
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
     * it, since the server strips the account everywhere. */
    viewAfterDisconnect?: UiWorkspacePermissions;
    /** Answer a request yourself, for a test about how the pane behaves while
     * a write is in flight or after one is refused. Return null to fall
     * through to the ordinary answers below. */
    respond?: (url: string) => Promise<{ ok: boolean; status: number; body: unknown }> | null;
  } = {},
): Promise<RenderResult> {
  const requests: { url: string; body: unknown }[] = [];
  let isSignedIn = false;
  const model = new PermissionsModel(AGENT_ID, {
    fetchJson: (url: string, init?: RequestInit) => {
      requests.push({ url, body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null });
      const answered = options.respond?.(url) ?? null;
      if (answered !== null) return answered;
      if (options.isReadRefused === true) {
        return Promise.resolve({ ok: false, status: 503, body: { error: "gateway is down" } });
      }
      if (url === "/settings/connectors/add-account") {
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
  const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
  (instance.oninit as unknown as (v: m.Vnode) => void).call(instance, vnode);
  await settle();
  const rerender = (): m.Vnode => (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(instance, vnode);
  const remove = (): void => (instance.onremove as unknown as () => void).call(instance);
  return { root: rerender(), rerender, remove, model, selectedSections, reviewedRequests, requests };
}

/** A connect awaits the sign-in, then the reload, then the selection -- more
 * promise hops than one settle covers. */
async function settleConnect(): Promise<void> {
  for (let index = 0; index < 4; index += 1) await settle();
}

describe("PermissionsTab loading", () => {
  it("reads the pane's own endpoint when the tab is first mounted", async () => {
    const { requests } = await render(permissionsView());
    expect(requests.map((request) => request.url)).toEqual([`/ui/api/workspaces/${AGENT_ID}/permissions`]);
  });

  it("offers a retry instead of an empty tree when the read fails", async () => {
    const { root } = await render(null, { isReadRefused: true });
    expect(allText(root)).toContain("Permissions can't be loaded right now");
    expect(allText(root)).toContain("gateway is down");
    expect(allText(root)).toContain("Try again");
    expect(switches(root)).toHaveLength(0);
  });

  it("renders the unavailable notice, never a 'nothing granted' screen", async () => {
    const { root } = await render(
      permissionsView({ connections: [], available_connections: [], permissions_unavailable: true }),
    );
    const text = allText(root);
    expect(text).toContain("Permissions can't be loaded right now. Try again in a moment.");
    expect(text).not.toContain("Add connection");
    expect(text).not.toContain("Every available service already has an account connected.");
  });

  it("names the workspace in the heading and the intro", async () => {
    const { root } = await render(permissionsView(), { workspaceName: "alpha" });
    const text = allText(root);
    expect(text).toContain("Permissions:");
    expect(text).toContain("alpha");
    expect(text).toContain("They can never reach beyond the permissions you grant them.");
  });

  it("drops the name from the copy when the options load has not landed", async () => {
    const { root } = await render(permissionsView(), { workspaceName: "" });
    const text = allText(root);
    expect(text).toContain("What agents in this machine can access.");
    expect(text).not.toContain("Permissions:");
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

describe("PermissionsTab left nav", () => {
  it("lists the connections, Add connection, and the two self families", async () => {
    const { root } = await render(
      permissionsView({
        connections: [slackConnection(), slackConnection({ service_name: "notion", display_name: "Notion", account: "n@x.com" })],
      }),
    );
    const navSections = withAttr(root, "data-perm-nav").map((node) => attrsOf(node)["data-perm-nav"]);
    expect(navSections).toEqual([
      "conn:slack:",
      "conn:notion:n@x.com",
      "add-connection",
      "local-files",
      "other-machines",
    ]);
  });

  it("shows the account label under a connection only when it disambiguates", async () => {
    const shown = await render(
      permissionsView({ connections: [slackConnection({ show_account_label: true, account_label: "work@example.com" })] }),
    );
    const hidden = await render(permissionsView());
    const nav = (result: RenderResult) => allText(withAttr(result.root, "data-perm-nav"));
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
      withAttr(result.root, "data-perm-nav")
        .filter((node) => attrsOf(node)["aria-pressed"] === "true")
        .map((node) => attrsOf(node)["data-perm-nav"]);
    expect(pressed(first)).toEqual(["conn:slack:"]);
    expect(pressed(requested)).toEqual(["conn:notion:"]);
  });

  it("reports a nav click as a section change for the URL to remember", async () => {
    const { root, selectedSections } = await render(permissionsView());
    const localFiles = withAttr(root, "data-perm-nav").find(
      (node) => attrsOf(node)["data-perm-nav"] === "local-files",
    );
    (attrsOf(localFiles as AnyVnode).onclick as () => void)();
    expect(selectedSections).toEqual(["local-files"]);
  });
});

describe("PermissionsTab connection panel", () => {
  it("renders a switch per toggle carrying its granted state", async () => {
    const { root } = await render(permissionsView());
    const controls = switches(root);
    expect(controls).toHaveLength(2);
    expect(controls.map((node) => attrsOf(node)["aria-checked"])).toEqual(["false", "true"]);
    expect(controls.map((node) => attrsOf(node)["data-perm-permission"])).toEqual([
      "slack-chat-read",
      "slack-chat-write",
    ]);
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

  it("marks a row whose write is in flight as busy", async () => {
    const { root, rerender } = await render(permissionsView(), {
      // Never resolves: the flip stays in flight for the whole test.
      respond: (url) => (url.endsWith("/connector-toggle") ? new Promise(() => undefined) : null),
    });

    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    const after = rerender();

    expect(classesOf(switches(after)[0])).toContain("is-busy");
    expect(classesOf(switches(after)[1])).not.toContain("is-busy");
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
    // be blurred -- this is not scoped to the machine on screen -- is spelled out.
    expect(dialogText).toContain("Disconnect Slack · Default account from Minds?");
    expect(dialogText).toContain("not just alpha");
    expect(dialogText).toContain("from all of your machines in Minds");
    expect(dialogText).toContain("connect it again from scratch");
    expect(dialogText).toContain("Yes, disconnect");

    (attrsOf(withAttr(asked, "data-perm-disconnect-cancel")[0]).onclick as () => void)();
    await settle();
    const declined = rerender();

    expect(withAttr(declined, "data-perm-disconnect-confirm")).toHaveLength(0);
    // Only the initial read: declining posts nothing at all.
    expect(requests).toHaveLength(1);
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

  it("leaves no dead connection selected once the disconnect lands", async () => {
    const gone = await render(permissionsView());
    (attrsOf(withAttr(gone.root, "data-perm-disconnect")[0]).onclick as () => void)();
    (attrsOf(withAttr(gone.rerender(), "data-perm-disconnect-confirm")[0]).onclick as () => void)();
    await settleConnect();
    expect(gone.selectedSections).toEqual(["add-connection"]);

    const notion = slackConnection({ service_name: "notion", display_name: "Notion" });
    const survivor = await render(permissionsView({ connections: [slackConnection(), notion] }), {
      viewAfterDisconnect: permissionsView({ connections: [notion] }),
    });
    (attrsOf(withAttr(survivor.root, "data-perm-disconnect")[0]).onclick as () => void)();
    (attrsOf(withAttr(survivor.rerender(), "data-perm-disconnect-confirm")[0]).onclick as () => void)();
    await settleConnect();
    expect(survivor.selectedSections).toEqual(["conn:notion:"]);
  });

  it("offers no disconnect on an account that is not connected", async () => {
    // There is no stored sign-in left to forget; the leftover grants are
    // Revoke all's business.
    const { root } = await render(permissionsView({ connections: [slackConnection({ is_connected: false })] }));
    expect(withAttr(root, "data-perm-disconnect")).toHaveLength(0);
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

    expect(allText(after)).toContain("Could not save the change: gateway refused");
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
    expect(requests[1]).toEqual({ url: "/settings/connectors/add-account", body: { service_name: "notion" } });
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
    expect(requests[1]).toEqual({ url: "/settings/connectors/add-account", body: { service_name: "slack" } });
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
    expect(allText(form)).toContain("that is not an access key id");
    const keptValues = withAttr(form, "name")
      .filter((node) => String(attrsOf(node).name).startsWith("credential-"))
      .map((node) => attrsOf(node).value);
    expect(keptValues).toEqual(["nonsense", "s3cret"]);
    expect(model.errorMessage).toBe("");
  });

  it("offers no action for a service it cannot work out the credentials for", async () => {
    const { root } = await render(
      permissionsView({ connections: [], available_connections: [awsAvailable({ credential_parameters: [] })] }),
      { requestedSection: "add-connection" },
    );
    const connect = withAttr(root, "data-perm-connect")[0];
    expect(attrsOf(connect).disabled).toBe(true);
    expect(String(attrsOf(connect).title)).toContain("Minds can't work out which credentials AWS needs");
  });

  it("says so when every service already has an account", async () => {
    const { root } = await render(permissionsView({ available_connections: [] }), {
      requestedSection: "add-connection",
    });
    expect(allText(root)).toContain("Every available service already has an account connected.");
  });

  it("renders shared paths as self toggles and posts only the permission", async () => {
    const { root, requests } = await render(
      permissionsView({
        file_sharing_toggles: [
          {
            permission: "path-1",
            label: "/Users/me/notes",
            detail: "read + write",
            description: "",
            is_granted: true,
            can_enable: true,
          },
        ],
      }),
      { requestedSection: "local-files" },
    );
    expect(allText(root)).toContain("/Users/me/notes");
    (attrsOf(switches(root)[0]).onclick as () => void)();
    await settle();
    expect(requests[1]).toEqual({
      url: `/ui/api/workspaces/${AGENT_ID}/permissions/self-toggle`,
      body: { permission: "path-1", enabled: false },
    });
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
    expect(allText(localFiles.root)).toContain("No files are being shared with agents in this machine yet.");
    expect(allText(otherMachines.root)).toContain("Agents in this machine can't manage your other machines yet.");
  });
  it("raises a refused connection over the pane, and only the user closes it", async () => {
    // A service that turns the sign-in away usually says something the user has
    // to act on somewhere else entirely, at several lines' length. That is why
    // it gets a popup while a refused toggle stays inline beside its row -- if
    // this ever went back to the thin banner it would be missed on a pane the
    // user has scrolled down.
    const refusal =
      "your Zoom user is not allowed to create apps in the Zoom App Marketplace; ask an administrator";
    const { root, rerender, model } = await render(permissionsView(), {
      requestedSection: "add-connection",
      signInRefusal: refusal,
    });
    const connect = withAttr(root, "data-perm-connect")[0];

    (attrsOf(connect).onclick as () => void)();
    await settleConnect();
    const afterFailure = rerender();

    const popup = collectVnodes(afterFailure).find((vnode) => attrsOf(vnode).id === "ws-perm-alert");
    expect(popup).toBeDefined();
    expect(attrsOf(popup as AnyVnode).isOpen).toBe(true);
    expect(allText(popup)).toContain("Couldn't connect");
    expect(allText(popup)).toContain(refusal);
    // ...and NOT as the inline notice, which belongs to loads and toggles.
    expect(collectVnodes(afterFailure).some((vnode) => attrsOf(vnode).id === "ws-perm-error")).toBe(false);

    model.dismissAlert();

    const dismissed = collectVnodes(rerender()).find((vnode) => attrsOf(vnode).id === "ws-perm-alert");
    expect(attrsOf(dismissed as AnyVnode).isOpen).toBe(false);
  });

});

describe("PermissionsTab service marks", () => {
  const navMark = (root: unknown): AnyVnode => marks(withAttr(root, "data-perm-nav")[0])[0];
  const headingMark = (root: unknown): AnyVnode => marks(withAttr(root, "data-perm-panel")[0])[0];

  it("draws a connected service's own logo, as an image", async () => {
    // An <img> and not a masked box: a mask keeps only the artwork's alpha, so
    // it could never show a full-color logo.
    const { root } = await render(permissionsView());
    for (const mark of [navMark(root), headingMark(root)]) {
      expect(markSources(mark)).toEqual(["/_static/service_icons/slack.svg"]);
      expect(hasClass(mark, "service-mark-muted")).toBe(false);
    }
  });

  it("carries a second image only for a mark that vanishes on the dark surface", async () => {
    // GitHub's logo is near-black and disappears on the dark theme's surface,
    // so the vendor's own white variant ships beside it and CSS picks between
    // the two. A brand that reads on both keeps a single image, since the
    // alternative would be recoloring artwork we are not allowed to alter.
    const { root } = await render(
      permissionsView({ connections: [slackConnection({ service_name: "github", display_name: "GitHub" })] }),
    );
    const images = markImages(navMark(root));
    expect(images.map((img) => String(attrsOf(img).src))).toEqual([
      "/_static/service_icons/github.svg",
      "/_static/service_icons/github-on-dark.svg",
    ]);
    expect(hasClass(images[0], "on-light-surface")).toBe(true);
    expect(hasClass(images[1], "on-dark-surface")).toBe(true);

    const legible = await render(
      permissionsView({ connections: [slackConnection({ service_name: "gitlab", display_name: "GitLab" })] }),
    );
    expect(markSources(navMark(legible.root))).toEqual(["/_static/service_icons/gitlab.svg"]);
  });

  it("drains the same logo once the account is not connected", async () => {
    // The mark stays the service's own artwork -- only the wrapper changes, and
    // the CSS behind it greys and dims what is already there.
    const { root } = await render(permissionsView({ connections: [slackConnection({ is_connected: false })] }));
    for (const mark of [navMark(root), headingMark(root)]) {
      expect(hasClass(mark, "service-mark-muted")).toBe(true);
      expect(markSources(mark)).toEqual(["/_static/service_icons/slack.svg"]);
    }
  });

  it("drains the marks of services that have no account yet", async () => {
    const { root } = await render(
      permissionsView({
        available_connections: [{ service_name: "gitlab", display_name: "GitLab", sign_in: BROWSER_SIGN_IN }],
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
    const { root, rerender } = await render(permissionsView());
    const probe = markImages(navMark(root))[0];
    expect(attrsOf(probe).src).toBe("/_static/service_icons/slack.svg");

    (attrsOf(probe).onerror as () => void)();
    const nav = withAttr(rerender(), "data-perm-nav")[0];

    expect(marks(nav)).toHaveLength(0);
    expect(collectVnodes(nav).some((node) => attrsOf(node).name === "box")).toBe(true);
  });
});

describe("permissions tab registration", () => {
  it("keeps the raised icon strip, the ?tab parse, and the titlebar in agreement", () => {
    // The machine tabs lead the strip, in the ?tab order, with the two
    // window-wide icons after them.
    expect(TITLEBAR_POPUP_ICONS.map((icon) => icon.id)).toEqual([
      ...OPTIONS_TABS,
      "notifications",
      "help",
    ]);
    expect(TITLEBAR_POPUP_ICONS[0]).toEqual({
      id: "permissions",
      buttonId: "ws-tab-permissions",
      icon: "key",
      label: "Permissions",
    });
    for (const tab of OPTIONS_TABS) {
      expect(toOptionsTab(tab)).toBe(tab);
      expect(classifyRoute("/workspace/agent-ab12/options", `tab=${tab}`).activeTab).toBe(tab);
    }
    expect(toOptionsTab("nonsense")).toBe("share");
    expect(classifyRoute("/workspace/agent-ab12/options").activeTab).toBe("share");
  });

  it("renders Permissions even when the shared options load failed", async () => {
    const optionsModel = new WorkspaceOptionsModel(AGENT_ID, {
      fetchJson: () => Promise.resolve({ ok: false, status: 500, body: { error: "options are gone" } }),
      redraw: () => undefined,
    });
    await optionsModel.load();
    const permissions = new PermissionsModel(AGENT_ID, {
      fetchJson: () => Promise.resolve({ ok: true, status: 200, body: permissionsView() }),
      redraw: () => undefined,
    });

    const panel = OptionsPanel() as unknown as m.Component;
    const attrs = {
      model: optionsModel,
      permissions,
      tab: "permissions",
      group: "general",
      section: null,
      onSelectGroup: () => undefined,
      onSelectSection: () => undefined,
      onReviewRequest: () => undefined,
    };
    const vnode = m(panel, attrs as unknown as m.Attributes) as m.Vnode;
    const root = (panel.view as unknown as (v: m.Vnode) => m.Vnode).call(panel, vnode);

    // The Permissions pane is what the body dispatches to -- not the shared
    // options-load failure, which only the other two tabs wait behind.
    expect(allText(root)).not.toContain("Could not load this machine's options");
    const permissionsPane = collectVnodes(root).find((node) => node.tag === PermissionsTab);
    expect(permissionsPane).toBeDefined();
    expect(attrsOf(permissionsPane as AnyVnode).workspaceName).toBe("");
  });
});
