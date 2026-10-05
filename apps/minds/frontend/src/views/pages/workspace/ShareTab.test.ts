import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  renderRoot,
  settle,
  sharePanelOptions,
} from "../../../testing";
import { createAppQueryClient } from "../../../models/queryClient";
import type { MachineSharingResponse } from "../../../models/workspaceOptions";
import { RESOLVE_USER_URL } from "../../../models/workspaceOptions";
import type { SharePanelModelOptions } from "../../../models/sharePanel";
import { GRANT_ADD_KINDS, SharePanelModel } from "../../../models/sharePanel";
import { Modal } from "../../components/Modal";
import { Spinner } from "../../components/Spinner";
import { ShareTab } from "./ShareTab";

const WHOLE = "system_interface";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** A loaded panel over `response`, with the shared fixture's two apps: `web`,
 * whose address the backend knows, and `docs`, whose it does not. */
async function readyPanel(
  response: Partial<MachineSharingResponse> = {},
  overrides: Partial<SharePanelModelOptions> = {},
): Promise<SharePanelModel> {
  const model = new SharePanelModel(
    sharePanelOptions({
      fetchJson: () =>
        Promise.resolve({
          ok: true,
          status: 200,
          body: {
            enabled: false,
            url: null,
            grants: {
              workspace: { emails: [], email_domains: [] },
              services: {},
            },
            ...response,
          },
        }),
      ...overrides,
    }),
  );
  await model.load();
  return model;
}

/** A published panel whose link is live. */
async function publishedPanel(
  response: Partial<MachineSharingResponse> = {},
): Promise<SharePanelModel> {
  return readyPanel({
    enabled: true,
    url: "https://m.relay.example/",
    ...response,
  });
}

function renderTab(share: SharePanelModel): m.Vnode {
  return renderRoot(ShareTab, { share, workspaceName: "alpha" });
}

/** Draw the panel repeatedly against one component instance, as a mount does:
 * the dialog and the copy confirmation live in the view's own closure, which a
 * fresh instance per draw would forget. */
function mountedTab(share: SharePanelModel): () => m.Vnode {
  const instance = ShareTab() as unknown as m.Component;
  return () => {
    const vnode = m(instance, {
      share,
      workspaceName: "alpha",
    } as unknown as m.Attributes) as m.Vnode;
    return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
      instance,
      vnode,
    );
  };
}

function byId(root: unknown, id: string): AnyVnode | undefined {
  return collectVnodes(root).find((vnode) => attrsOf(vnode).id === id);
}

/** The nav entries, in the order the nav lists them. */
function navEntries(root: unknown): AnyVnode[] {
  return collectVnodes(root).filter(
    (vnode) => attrsOf(vnode)["data-share-target"] !== undefined,
  );
}

/** The grant rows, in the order the list draws them. */
function grantRows(root: unknown): AnyVnode[] {
  return collectVnodes(root).filter(
    (vnode) => attrsOf(vnode)["data-grant-row"] !== undefined,
  );
}

function rowByText(root: unknown, text: string): AnyVnode {
  const row = grantRows(root).find((vnode) => allText(vnode).includes(text));
  expect(row, `no grant row mentioning ${text}`).toBeDefined();
  return row as AnyVnode;
}

/** The controls of the add row, in the order they are read. */
function addControls(root: unknown): AnyVnode[] {
  return [
    byId(root, "ws-share-add-kind") as AnyVnode,
    byId(root, "ws-share-add-value") as AnyVnode,
    byId(root, "ws-share-add-btn") as AnyVnode,
  ];
}

function chooseKind(root: unknown, kind: string): void {
  const onchange = attrsOf(addControls(root)[0]).onchange as (
    event: Event,
  ) => void;
  onchange({ target: { value: kind } } as unknown as Event);
}

function type(root: unknown, value: string): void {
  const oninput = attrsOf(addControls(root)[1]).oninput as (
    event: InputEvent,
  ) => void;
  oninput({ target: { value } } as unknown as InputEvent);
}

function pressAdd(root: unknown): void {
  (attrsOf(addControls(root)[2]).onclick as () => void)();
}

describe("ShareTab publish widget", () => {
  it("heads the switch Enable sharing, reading No, over the sharing sentence", async () => {
    const share = await readyPanel();

    const widget = byId(renderTab(share), "ws-share-publish");

    expect(widget).toBeDefined();
    expect(allText(widget)).toContain("Enable sharing");
    expect(allText(widget)).not.toMatch(/publish/i);
    expect(allText(widget)).toContain(
      "Sharing gives this workspace an address on the internet. " +
        "Only people granted access can open it.",
    );
    const control = byId(widget, "ws-share-publish-switch");
    expect(attrsOf(control as AnyVnode).role).toBe("switch");
    expect(attrsOf(control as AnyVnode)["aria-label"]).toBe("Enable sharing");
    expect(attrsOf(control as AnyVnode)["aria-checked"]).toBe("false");
    expect(attrsOf(control as AnyVnode)["aria-busy"]).toBeUndefined();
    expect(classTokensOf(control as AnyVnode)).not.toContain("is-settling");
    expect(allText(widget)).toContain("No");
  });

  it("reads Yes and keeps the sharing sentence once the workspace is published", async () => {
    const share = await publishedPanel();

    const widget = byId(renderTab(share), "ws-share-publish");

    expect(
      attrsOf(byId(widget, "ws-share-publish-switch") as AnyVnode)[
        "aria-checked"
      ],
    ).toBe("true");
    expect(allText(widget)).toContain("Yes");
    expect(allText(widget)).toContain(
      "Sharing gives this workspace an address on the internet. " +
        "Only people granted access can open it.",
    );
  });

  it("draws no switch, and spins the wait at the off side, until the publication is known", async () => {
    const share = new SharePanelModel(
      sharePanelOptions({ fetchJson: () => new Promise(() => undefined) }),
    );
    void share.load();

    const widget = byId(renderTab(share), "ws-share-publish");

    expect(byId(widget, "ws-share-publish-switch")).toBeUndefined();
    const placeholder = byId(widget, "ws-share-publish-unknown") as AnyVnode;
    expect(placeholder.tag).toBe("span");
    expect(classTokensOf(placeholder)).toEqual(
      expect.arrayContaining(["perm-switch", "is-checking"]),
    );
    expect(attrsOf(placeholder).role).toBe("status");
    expect(attrsOf(placeholder)["aria-busy"]).toBe("true");
    expect(attrsOf(placeholder)["aria-label"]).toBe(
      "Checking whether sharing is enabled",
    );
    // The wait is the switch's own: no word beside it and no second spinner.
    expect(allText(widget)).not.toMatch(/\b(No|Yes|Checking)\b/);
    expect(collectVnodes(widget).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
    // The add row waits too, and says why in the same terms: nothing is off.
    for (const control of addControls(renderTab(share))) {
      expect(attrsOf(control)["aria-disabled"]).toBe("true");
      expect(attrsOf(control)["data-tooltip"]).toBe(
        "Permissions cannot be granted until the sharing status has loaded",
      );
    }
  });

  it("greys the switch out, rather than drawing it off, when the first read failed", async () => {
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: () =>
          Promise.resolve({
            ok: false,
            status: 502,
            body: { error: "relay down" },
          }),
      }),
    );
    await share.load();

    const widget = byId(renderTab(share), "ws-share-publish");

    expect(byId(widget, "ws-share-publish-switch")).toBeUndefined();
    const placeholder = byId(widget, "ws-share-publish-unknown") as AnyVnode;
    expect(classTokensOf(placeholder)).toEqual(
      expect.arrayContaining(["perm-switch", "is-unknown"]),
    );
    expect(classTokensOf(placeholder)).not.toContain("is-checking");
    expect(attrsOf(placeholder)["aria-busy"]).toBeUndefined();
    expect(attrsOf(placeholder)["aria-label"]).toBe("Sharing status unknown");
    expect(allText(widget)).not.toMatch(/\b(No|Yes|Checking|Unknown)\b/);
    expect(allText(widget)).toContain("relay down");
    expect(collectVnodes(widget).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
  });

  it("spins the wait in the switch's knob while a toggle is in flight, without dimming it", async () => {
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          init?.method === "PUT"
            ? new Promise(() => undefined)
            : Promise.resolve({
                ok: true,
                status: 200,
                body: {
                  enabled: false,
                  url: null,
                  grants: {
                    workspace: { emails: [], email_domains: [] },
                    services: {},
                  },
                },
              }),
      },
    );
    void share.publish();

    const widget = byId(renderTab(share), "ws-share-publish");

    const control = byId(widget, "ws-share-publish-switch") as AnyVnode;
    expect(attrsOf(control)["aria-checked"]).toBe("true");
    expect(attrsOf(control)["aria-busy"]).toBe("true");
    expect(classTokensOf(control)).toContain("is-settling");
    expect(classTokensOf(control)).not.toContain("is-busy");
    expect(allText(widget)).toContain("Yes");
  });

  it("draws the switch from what an earlier panel read, with the fresh read spinning in its knob", async () => {
    const queryClient = createAppQueryClient();
    const earlier = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      { queryClient },
    );
    earlier.dispose();
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: () => new Promise(() => undefined),
        queryClient,
      }),
    );
    void share.load();

    const widget = byId(renderTab(share), "ws-share-publish");

    const control = byId(widget, "ws-share-publish-switch") as AnyVnode;
    expect(attrsOf(control)["aria-checked"]).toBe("true");
    expect(attrsOf(control)["aria-busy"]).toBe("true");
    expect(classTokensOf(control)).toContain("is-settling");
    expect(classTokensOf(control)).not.toContain("is-busy");
    expect(allText(widget)).toContain("Yes");
    expect(byId(widget, "ws-share-publish-unknown")).toBeUndefined();
    expect(collectVnodes(widget).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
  });

  it("rules the switch and its sentence off from the targets below", async () => {
    const share = await publishedPanel();

    const vnodes = collectVnodes(renderTab(share));
    const widgetIdx = vnodes.findIndex(
      (vnode) => attrsOf(vnode).id === "ws-share-publish",
    );
    const ruleIdx = vnodes.findIndex((vnode) => vnode.tag === "hr");
    const navIdx = vnodes.findIndex((vnode) => vnode.tag === "nav");

    expect(widgetIdx).toBeGreaterThanOrEqual(0);
    expect(ruleIdx).toBeGreaterThan(widgetIdx);
    expect(navIdx).toBeGreaterThan(ruleIdx);
    expect(
      collectVnodes(vnodes[widgetIdx]).some((vnode) => vnode.tag === "hr"),
    ).toBe(false);
  });

  it("says the link is being generated and names only the step under way", async () => {
    const share = await publishedPanel();
    share.isLive = false;
    share.isCertIssued = true;

    const block = byId(renderTab(share), "ws-share-provisioning");

    expect(allText(block)).toContain(
      "Generating a secure link. This takes a while because we want to " +
        "protect you from bad actors on the internet.",
    );
    expect(allText(block)).toContain("Connecting to the relay");
    expect(allText(block)).not.toContain("Creating link");
    expect(allText(block)).not.toContain("Setting up encryption");
    expect(allText(block)).not.toContain("Verifying end to end");
    expect(allText(block)).toContain(
      "People can be added while the link is being prepared.",
    );
    expect(collectVnodes(block).some((vnode) => vnode.tag === Spinner)).toBe(
      true,
    );
  });
});

describe("ShareTab target nav", () => {
  it("lists the whole workspace first, then each app with its count", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: {
          emails: ["friend@example.com"],
          email_domains: ["example.org"],
        },
        services: { web: { emails: ["dev@example.com"], email_domains: [] } },
      },
    });

    const entries = navEntries(renderTab(share));

    expect(entries.map((entry) => attrsOf(entry)["data-share-target"])).toEqual(
      [WHOLE, "web", "docs"],
    );
    expect(allText(entries[0])).toContain("Whole workspace");
    expect(allText(entries[0])).toContain("2");
    expect(allText(entries[1])).toContain("1");
    expect(allText(entries[2])).toContain("0");
  });

  it("holds only the entries, with no rule between the whole workspace and the apps", async () => {
    const share = await publishedPanel();

    const nav = collectVnodes(renderTab(share)).find(
      (vnode) => vnode.tag === "nav",
    ) as AnyVnode;

    expect((nav.children as AnyVnode[]).map((child) => child.tag)).toEqual([
      "button",
      "button",
      "button",
    ]);
  });

  it("puts each count in a badge rather than leaving it a bare numeral", async () => {
    const share = await publishedPanel();

    for (const entry of navEntries(renderTab(share))) {
      const count = collectVnodes(entry).find(
        (vnode) => attrsOf(vnode)["data-share-count"] !== undefined,
      );
      expect(count).toBeDefined();
      const tokens = classTokensOf(count as AnyVnode);
      expect(tokens).toContain("bg-fill-subtle");
      expect(tokens).toContain("rounded-md");
      expect(tokens).toContain("type-helper");
      expect(tokens).toContain("text-secondary");
    }
  });

  it("marks only an app whose address the backend does not know yet", async () => {
    const share = await publishedPanel();

    const entries = navEntries(renderTab(share));

    expect(allText(entries[0])).not.toContain("no link yet");
    expect(allText(entries[1])).not.toContain("no link yet");
    expect(allText(entries[2])).toContain("no link yet");
  });

  it("selects the target it is pressed on", async () => {
    const share = await publishedPanel();

    const entries = navEntries(renderTab(share));
    (attrsOf(entries[1]).onclick as () => void)();

    expect(share.currentTarget).toBe("web");
    expect(allText(renderTab(share))).toContain("Permissions for web");
  });
});

describe("ShareTab link section", () => {
  it("shows the target's link and who it opens for while published", async () => {
    const share = await publishedPanel();

    const link = byId(renderTab(share), "ws-share-link");

    expect(allText(link)).toContain("Link");
    expect(
      collectVnodes(link).some(
        (vnode) =>
          attrsOf(vnode).value === "https://shell-r4nd.m.relay.example/",
      ),
    ).toBe(true);
    expect(allText(link)).toContain(
      "Only people granted permission can open this link.",
    );
  });

  it("waits for the link rather than showing an empty field", async () => {
    const share = await publishedPanel();
    share.isLive = false;
    share.selectTarget("docs");

    const link = byId(renderTab(share), "ws-share-link");

    expect(allText(link)).toContain("Preparing the link");
    expect(allText(link)).not.toContain("Only people granted permission");
  });

  it("has no link section at all while publishing is off", async () => {
    const share = await readyPanel();

    expect(byId(renderTab(share), "ws-share-link")).toBeUndefined();
  });
});

describe("ShareTab target pane", () => {
  it("heads the pane with the target and what a permission there covers", async () => {
    const share = await publishedPanel();

    expect(allText(renderTab(share))).toContain(
      "Permissions for the whole workspace",
    );
    expect(allText(renderTab(share))).toContain(
      "Permissions below apply to every app in this workspace, and also grant " +
        "access to files, agent chats and terminal.",
    );

    share.selectTarget("web");

    expect(allText(renderTab(share))).toContain("Permissions for web");
    expect(allText(renderTab(share))).toContain(
      "Permissions below apply only to the web app.",
    );
  });
});

describe("ShareTab add row", () => {
  it("places the example the chosen kind calls for", async () => {
    const share = await publishedPanel();

    expect(attrsOf(addControls(renderTab(share))[1]).placeholder).toBe(
      "name@example.com",
    );

    chooseKind(renderTab(share), "email_domain");

    expect(attrsOf(addControls(renderTab(share))[1]).placeholder).toBe(
      "example.com",
    );
  });

  it("offers the kinds the model grants, and reads any other as one person", async () => {
    const share = await publishedPanel();

    const options = collectVnodes(addControls(renderTab(share))[0]).filter(
      (vnode) => vnode.tag === "option",
    );
    expect(options.map((option) => attrsOf(option).value)).toEqual([
      ...GRANT_ADD_KINDS,
    ]);
    expect(options.map((option) => allText(option))).toEqual([
      "one person by email",
      "everyone at a domain",
    ]);

    chooseKind(renderTab(share), "nonsense");

    expect(attrsOf(addControls(renderTab(share))[1]).placeholder).toBe(
      "name@example.com",
    );
    expect(share.addRow(WHOLE).kind).toBe("email");
  });

  it("grants what was typed and empties the row", async () => {
    const share = await publishedPanel();

    type(renderTab(share), "friend@example.com");
    pressAdd(renderTab(share));

    expect(share.grantsFor(WHOLE).map((grant) => grant.grantee.value)).toEqual([
      "friend@example.com",
    ]);
    expect(attrsOf(addControls(renderTab(share))[1]).value).toBe("");
  });

  it("says why nothing can be granted while sharing is off, and grants nothing", async () => {
    const share = await readyPanel();

    const root = renderTab(share);
    for (const control of addControls(root)) {
      expect(attrsOf(control)["aria-disabled"]).toBe("true");
      expect(attrsOf(control)["data-tooltip"]).toBe(
        "Permissions cannot be granted while sharing is off",
      );
    }

    type(root, "friend@example.com");
    pressAdd(root);

    expect(share.grantsFor(WHOLE)).toHaveLength(0);
  });

  it("shows a refused entry's reason where it was typed", async () => {
    const share = await publishedPanel();

    type(renderTab(share), "gmail.com");
    chooseKind(renderTab(share), "email_domain");
    pressAdd(renderTab(share));

    expect(allText(byId(renderTab(share), "ws-share-refusal"))).toBe(
      "gmail.com cannot be granted permissions because it is a public email provider.",
    );
    expect(share.grantsFor(WHOLE)).toHaveLength(0);
    expect(String(attrsOf(addControls(renderTab(share))[1]).extra)).toContain(
      "!border-important",
    );
  });

  it("clears the refusal, and the mark on the input, as soon as it is retyped", async () => {
    const share = await publishedPanel();
    chooseKind(renderTab(share), "email_domain");
    type(renderTab(share), "gmail.com");
    pressAdd(renderTab(share));

    type(renderTab(share), "gmail.co");

    expect(byId(renderTab(share), "ws-share-refusal")).toBeUndefined();
    expect(
      String(attrsOf(addControls(renderTab(share))[1]).extra),
    ).not.toContain("!border-important");
  });
});

describe("ShareTab grant list", () => {
  /** A published panel granting one domain, one account, one invited address
   * and one address whose write failed. */
  async function panelWithGrants(): Promise<SharePanelModel> {
    const share = await publishedPanel({
      grants: {
        workspace: {
          users: ["user-2"],
          emails: ["newcomer@example.com"],
          email_domains: ["acme.example"],
        },
        services: {},
      },
      identities: {
        "user-2": {
          user_id: "user-2",
          email: "carol@example.net",
          display_name: "Carol Reyes",
          profile_picture_url: null,
        },
      },
    });
    share.grantsFor(WHOLE);
    type(renderTab(share), "erin@example.org");
    pressAdd(renderTab(share));
    return share;
  }

  it("says so in words when nobody has been granted access", async () => {
    const share = await publishedPanel();

    const empty = byId(renderTab(share), "ws-share-empty") as AnyVnode;
    expect(allText(empty)).toBe("Nobody has been granted access yet.");
    expect(grantRows(renderTab(share))).toHaveLength(0);
    // Boxed, and clear of the disabled add row's tooltip.
    expect(classTokensOf(empty)).toContain("border-dashed");
    expect(classTokensOf(empty)).toContain("text-tertiary");
    expect(classTokensOf(empty)).toContain("mt-6");
  });

  it("leads the list with the domain grants", async () => {
    const share = await panelWithGrants();

    const rows = grantRows(renderTab(share));

    expect(allText(rows[0])).toContain("Anyone at acme.example");
    expect(allText(rows[1])).toContain("Carol Reyes");
  });

  it("outlines every row, and tints a domain row's outline with its surface", async () => {
    const share = await panelWithGrants();

    const rows = grantRows(renderTab(share));

    for (const row of rows) expect(classTokensOf(row)).toContain("border");
    for (const row of rows)
      expect(classTokensOf(row)).not.toContain("grayscale");
    expect(classTokensOf(rows[0])).toContain(
      "border-[color-mix(in_srgb,var(--c-info)_28%,transparent)]",
    );
    expect(classTokensOf(rows[1])).toContain("border-subtle");
  });

  it("waits for the lookup before saying nobody has signed up", async () => {
    const share = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      {
        fetchJson: (url: string, init?: RequestInit) =>
          url === RESOLVE_USER_URL || init?.method === "PUT"
            ? new Promise(() => undefined)
            : Promise.resolve({
                ok: true,
                status: 200,
                body: {
                  enabled: true,
                  url: "https://m.relay.example/",
                  grants: {
                    workspace: { emails: [], email_domains: [] },
                    services: {},
                  },
                },
              }),
      },
    );

    type(renderTab(share), "newcomer@example.com");
    pressAdd(renderTab(share));

    const row = rowByText(renderTab(share), "newcomer@example.com");
    expect(share.grantsFor(WHOLE)[0].status.state).toBe("saving");
    expect(allText(row)).not.toContain("signed up");
  });

  it("gives every row the same height and never shortens a name", async () => {
    const share = await panelWithGrants();

    const rows = grantRows(renderTab(share));

    expect(rows).toHaveLength(4);
    for (const row of rows) {
      expect(classTokensOf(row)).toContain("h-10");
      expect(classTokensOf(row)).toContain("flex-none");
      const name = collectVnodes(row).find(
        (vnode) => attrsOf(vnode)["data-grant-name"] !== undefined,
      );
      expect(name).toBeDefined();
      expect(classTokensOf(name as AnyVnode)).toContain("whitespace-nowrap");
      expect(classTokensOf(name as AnyVnode)).not.toContain("truncate");
    }
  });

  it("keeps an empty status slot and an empty action slot on every row", async () => {
    const share = await panelWithGrants();

    for (const row of grantRows(renderTab(share))) {
      const slots = collectVnodes(row).filter(
        (vnode) => attrsOf(vnode)["data-slot"] !== undefined,
      );
      expect(slots.map((slot) => attrsOf(slot)["data-slot"])).toEqual([
        "status",
        "action",
      ]);
      for (const slot of slots) expect(allText(slot)).toBe("");
    }
  });

  it("scrolls the list alone, with nothing above it in the scroller", async () => {
    const share = await panelWithGrants();

    const list = byId(renderTab(share), "ws-share-grants");

    expect(classTokensOf(list as AnyVnode)).toContain("overflow-y-auto");
    expect(classTokensOf(list as AnyVnode)).toContain("flex-1");
    expect(classTokensOf(list as AnyVnode)).toContain("min-h-0");
    expect(allText(list)).not.toContain("Grant permission to");
  });

  it("marks a row while it saves, and offers a retry once it cannot", async () => {
    const share = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? { ok: false, status: 500, body: {} }
              : {
                  ok: true,
                  status: 200,
                  body: {
                    enabled: true,
                    url: "https://m.relay.example/",
                    grants: {
                      workspace: { emails: [], email_domains: [] },
                      services: {},
                    },
                  },
                },
          ),
      },
    );

    type(renderTab(share), "erin@example.org");
    pressAdd(renderTab(share));

    expect(allText(rowByText(renderTab(share), "erin@example.org"))).toContain(
      "Securely granting access",
    );

    await settle();

    const row = rowByText(renderTab(share), "erin@example.org");
    expect(allText(row)).toContain("Could not save");
    expect(allText(row)).toContain("Retry");
  });

  it("names the person each remove control takes access from", async () => {
    const share = await panelWithGrants();

    const labels = grantRows(renderTab(share)).map((row) => {
      const button = collectVnodes(row).find(
        (vnode) =>
          typeof attrsOf(vnode)["aria-label"] === "string" &&
          String(attrsOf(vnode)["aria-label"]).startsWith("Remove"),
      );
      return attrsOf(button as AnyVnode)["aria-label"];
    });

    expect(labels).toEqual([
      "Remove anyone at acme.example",
      "Remove Carol Reyes",
      "Remove erin@example.org",
      "Remove newcomer@example.com",
    ]);
  });

  it("takes a row out the moment its remove control is pressed", async () => {
    const share = await panelWithGrants();

    const row = rowByText(renderTab(share), "newcomer@example.com");
    const button = collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["aria-label"] === "Remove newcomer@example.com",
    );
    (attrsOf(button as AnyVnode).onclick as () => void)();

    expect(allText(renderTab(share))).not.toContain("newcomer@example.com");
  });
});

describe("ShareTab publish confirmation", () => {
  /** A panel that remembers the methods its sharing calls used, and answers a
   * publish with a published workspace. */
  async function recordingPanel(): Promise<{
    share: SharePanelModel;
    methods: string[];
  }> {
    const methods: string[] = [];
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) => {
          const method = init?.method ?? "GET";
          methods.push(method);
          return Promise.resolve({
            ok: true,
            status: 200,
            body: {
              enabled: method === "PUT",
              url: method === "PUT" ? "https://m.relay.example/" : null,
              grants: {
                workspace: { emails: [], email_domains: [] },
                services: {},
              },
            },
          });
        },
      },
    );
    return { share, methods };
  }

  function pressSwitch(root: unknown): void {
    (
      attrsOf(byId(root, "ws-share-publish-switch") as AnyVnode)
        .onclick as () => void
    )();
  }

  function isDialogOpen(root: unknown): boolean {
    const dialog = collectVnodes(root).find((vnode) => vnode.tag === Modal);
    expect(dialog, "the panel drew no dialog").toBeDefined();
    return attrsOf(dialog as AnyVnode).isOpen === true;
  }

  it("names the consequence before anything is created", async () => {
    const { share, methods } = await recordingPanel();
    const draw = mountedTab(share);

    pressSwitch(draw());

    const root = draw();
    expect(isDialogOpen(root)).toBe(true);
    expect(allText(root)).toContain(
      "Anyone granted access to this workspace will be able to see and change " +
        "everything within it, including all files, agent chats and terminal. " +
        "If you only grant access to an app, they will be able to access all " +
        "data that app provides to them.",
    );
    expect(allText(root)).toContain("Enable sharing?");
    expect(allText(byId(root, "ws-share-publish-confirm"))).toBe(
      "Enable sharing",
    );
    expect(
      attrsOf(byId(root, "ws-share-publish-switch") as AnyVnode)[
        "aria-checked"
      ],
    ).toBe("false");
    expect(methods).toEqual(["GET"]);
  });

  it("leaves the switch where it was, and creates nothing, on Cancel", async () => {
    const { share, methods } = await recordingPanel();
    const draw = mountedTab(share);

    pressSwitch(draw());
    (
      attrsOf(byId(draw(), "ws-share-publish-cancel") as AnyVnode)
        .onclick as () => void
    )();

    const root = draw();
    expect(isDialogOpen(root)).toBe(false);
    expect(
      attrsOf(byId(root, "ws-share-publish-switch") as AnyVnode)[
        "aria-checked"
      ],
    ).toBe("false");
    expect(share.isPublished).toBe(false);
    expect(methods).toEqual(["GET"]);
  });

  it("publishes once the question is answered", async () => {
    const { share, methods } = await recordingPanel();
    const draw = mountedTab(share);

    pressSwitch(draw());
    (
      attrsOf(byId(draw(), "ws-share-publish-confirm") as AnyVnode)
        .onclick as () => void
    )();
    await settle();

    expect(isDialogOpen(draw())).toBe(false);
    expect(share.isPublished).toBe(true);
    expect(methods).toContain("PUT");
  });
});

describe("ShareTab off notice", () => {
  /** An unpublished panel that still grants one person everywhere. */
  async function offPanel(): Promise<SharePanelModel> {
    return readyPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: { web: { emails: ["dev@example.com"], email_domains: [] } },
      },
    });
  }

  it("says the workspace cannot be opened, and the list is kept", async () => {
    const share = await offPanel();

    expect(allText(byId(renderTab(share), "ws-share-off-notice"))).toBe(
      "This workspace cannot be accessed while sharing is off. " +
        "Your list of permissions is preserved but inactive.",
    );

    share.selectTarget("web");

    expect(allText(byId(renderTab(share), "ws-share-off-notice"))).toBe(
      "This app cannot be accessed while sharing is off. " +
        "Your list of permissions is preserved but inactive.",
    );
  });

  it("draws every row flat, grey and dim while sharing is off", async () => {
    const share = await readyPanel({
      grants: {
        workspace: {
          emails: ["friend@example.com"],
          email_domains: ["acme.example"],
        },
        services: {},
      },
    });

    const rows = grantRows(renderTab(share));

    expect(rows).toHaveLength(2);
    for (const row of rows) {
      const tokens = classTokensOf(row);
      expect(tokens).toContain("grayscale");
      expect(tokens).toContain("opacity-60");
      expect(tokens).toContain("bg-fill-subtle");
      expect(tokens).toContain("border-subtle");
      expect(tokens).not.toContain("bg-[var(--c-info-surface)]");
    }
  });

  it("still offers to remove a grant while publishing is off", async () => {
    const share = await offPanel();

    const row = rowByText(renderTab(share), "friend@example.com");
    const button = collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["aria-label"] === "Remove friend@example.com",
    );
    (attrsOf(button as AnyVnode).onclick as () => void)();

    expect(share.grantsFor(WHOLE)).toHaveLength(0);
  });

  it("has no off notice once the workspace is published", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {},
      },
    });

    expect(byId(renderTab(share), "ws-share-off-notice")).toBeUndefined();
  });
});

describe("ShareTab inherited grants", () => {
  async function appPanel(workspaceGrants: {
    emails?: string[];
    email_domains?: string[];
  }): Promise<SharePanelModel> {
    const share = await publishedPanel({
      grants: {
        workspace: {
          emails: workspaceGrants.emails ?? [],
          email_domains: workspaceGrants.email_domains ?? [],
        },
        services: {},
      },
    });
    share.selectTarget("web");
    return share;
  }

  it("counts the inherited permissions and keeps them shut until asked", async () => {
    const share = await appPanel({
      emails: ["one@example.com", "two@example.com"],
      email_domains: ["acme.example"],
    });

    const group = byId(renderTab(share), "ws-share-inherited");

    expect(allText(group)).toContain(
      "3 permissions inherited from the whole workspace",
    );
    // Shut, the rows are not drawn at all, so nothing reaches them -- not the
    // keyboard, not a screen reader.
    expect(allText(group)).not.toContain("one@example.com");
  });

  it("counts a lone inherited permission in the singular", async () => {
    const share = await appPanel({ emails: ["one@example.com"] });

    expect(allText(byId(renderTab(share), "ws-share-inherited"))).toContain(
      "1 permission inherited from the whole workspace",
    );
  });

  it("shows the inherited grants under the header once it is opened", async () => {
    const share = await appPanel({
      emails: ["one@example.com", "two@example.com"],
      email_domains: ["acme.example"],
    });

    const draw = mountedTab(share);

    const trigger = byId(draw(), "ws-share-inherited-trigger") as AnyVnode;
    expect(attrsOf(trigger)["aria-expanded"]).toBe("false");
    (attrsOf(trigger).onclick as () => void)();

    const opened = byId(draw(), "ws-share-inherited");
    expect(
      attrsOf(byId(draw(), "ws-share-inherited-trigger") as AnyVnode)[
        "aria-expanded"
      ],
    ).toBe("true");
    const text = allText(opened);
    expect(text).toContain("Anyone at acme.example");
    expect(text.indexOf("acme.example")).toBeLessThan(
      text.indexOf("one@example.com"),
    );
    expect(text.indexOf("one@example.com")).toBeLessThan(
      text.indexOf("two@example.com"),
    );
  });

  it("ties the header to the rows it opens, for a screen reader", async () => {
    const share = await appPanel({ emails: ["one@example.com"] });

    const draw = mountedTab(share);
    const trigger = byId(draw(), "ws-share-inherited-trigger") as AnyVnode;
    (attrsOf(trigger).onclick as () => void)();

    const root = draw();
    const opened = byId(root, "ws-share-inherited-trigger") as AnyVnode;
    const panel = byId(root, "ws-share-inherited-panel") as AnyVnode;
    expect(attrsOf(opened)["aria-controls"]).toBe("ws-share-inherited-panel");
    expect(attrsOf(panel).role).toBe("region");
    expect(attrsOf(panel)["aria-labelledby"]).toBe(
      "ws-share-inherited-trigger",
    );
  });

  it("rails and sets in the rows this pane only inherits", async () => {
    const share = await appPanel({ emails: ["one@example.com"] });
    const draw = mountedTab(share);

    (
      attrsOf(byId(draw(), "ws-share-inherited-trigger") as AnyVnode)
        .onclick as () => void
    )();

    // A rail marks them as the whole workspace's rather than this app's, and
    // the indent beside it still lands a row's contents under the summary.
    const panel = byId(draw(), "ws-share-inherited-panel") as AnyVnode;
    const tokens = classTokensOf(panel);
    expect(tokens).toContain("border-l-2");
    expect(tokens).toContain("ml-3");
    expect(tokens).toContain("pl-[10px]");
  });

  it("offers no remove control on a row this pane only inherits", async () => {
    const share = await appPanel({
      emails: ["one@example.com"],
      email_domains: ["acme.example"],
    });
    const draw = mountedTab(share);

    (
      attrsOf(byId(draw(), "ws-share-inherited-trigger") as AnyVnode)
        .onclick as () => void
    )();

    const panel = byId(draw(), "ws-share-inherited-panel") as AnyVnode;
    const removes = collectVnodes(panel).filter((vnode) =>
      String(attrsOf(vnode)["aria-label"] ?? "").startsWith("Remove"),
    );
    // The grant is the whole workspace's, so a control here would either do
    // nothing or revoke it somewhere the reader is not looking.
    expect(removes).toEqual([]);
  });

  it("keeps the header above the rows it names while they are in view", async () => {
    const share = await appPanel({ emails: ["one@example.com"] });

    const trigger = byId(
      renderTab(share),
      "ws-share-inherited-trigger",
    ) as AnyVnode;
    // Sticky against the group rather than the scroller, so the header leaves
    // with the rows it labels instead of outliving them.
    expect(classTokensOf(trigger)).toContain("sticky");
    expect(classTokensOf(trigger)).toContain("top-0");
  });

  it("says nothing about inherited grants on the whole workspace itself", async () => {
    const share = await publishedPanel();

    expect(byId(renderTab(share), "ws-share-inherited")).toBeUndefined();
  });
});

describe("ShareTab copy control", () => {
  it("copies the target's link and confirms it", async () => {
    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: (text: string) => {
          written.push(text);
          return Promise.resolve();
        },
      },
    });
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const share = await publishedPanel();

    const draw = mountedTab(share);

    const copy = byId(draw(), "ws-share-copy") as AnyVnode;
    expect(attrsOf(copy)["aria-label"]).toBe("Copy the link");
    (attrsOf(copy).onclick as () => void)();
    await settle();

    expect(written).toEqual(["https://shell-r4nd.m.relay.example/"]);
    expect(
      collectVnodes(byId(draw(), "ws-share-copy")).some(
        (vnode) => attrsOf(vnode).name === "check",
      ),
    ).toBe(true);
  });

  it("names the app a link opens", async () => {
    const share = await publishedPanel();
    share.selectTarget("web");

    expect(
      attrsOf(byId(renderTab(share), "ws-share-copy") as AnyVnode)[
        "aria-label"
      ],
    ).toBe("Copy the link to web");
  });
});

describe("ShareTab while the link is being prepared", () => {
  /** B2.3: published a moment ago, two steps done, one grant still saving. */
  async function provisioningPanel(): Promise<SharePanelModel> {
    const share = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      {
        fetchJson: (url: string, init?: RequestInit) =>
          url === RESOLVE_USER_URL || init?.method === "PUT"
            ? new Promise(() => undefined)
            : Promise.resolve({
                ok: true,
                status: 200,
                body: {
                  enabled: true,
                  url: "https://m.relay.example/",
                  grants: {
                    workspace: { emails: [], email_domains: [] },
                    services: {},
                  },
                },
              }),
      },
    );
    // The fixture holds every PUT, so this publish stays in flight.
    void share.publish();
    share.isLive = false;
    share.isCertIssued = true;
    type(renderTab(share), "bob@example.org");
    pressAdd(renderTab(share));
    return share;
  }

  it("names the step under way over a list that is still live", async () => {
    const share = await provisioningPanel();

    const root = renderTab(share);

    expect(allText(byId(root, "ws-share-provisioning"))).toContain(
      "Connecting to the relay",
    );
    expect(allText(rowByText(root, "bob@example.org"))).toContain(
      "Securely granting access",
    );
    expect(allText(byId(root, "ws-share-link"))).toContain(
      "Preparing the link",
    );
  });

  it("takes another grant while the publish write is still in flight", async () => {
    const share = await provisioningPanel();
    expect(share.publishWrite).toEqual({ state: "publishing" });

    const root = renderTab(share);
    for (const control of addControls(root))
      expect(attrsOf(control)["aria-disabled"]).toBeUndefined();

    type(root, "carol@example.net");
    pressAdd(root);

    expect(share.grantCount(WHOLE)).toBe(2);
  });
});

describe("ShareTab row states together", () => {
  /** B2.4: one row saving, one failed, one just added, on one clock. */
  async function mixedPanel(): Promise<{
    share: SharePanelModel;
    setNow: (ms: number) => void;
  }> {
    let now = 1000;
    let isFirstWrite = true;
    const share = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      {
        monotonicNowMs: () => now,
        fetchJson: (url: string, init?: RequestInit) => {
          if (url === RESOLVE_USER_URL) return new Promise(() => undefined);
          if (init?.method === "PUT") {
            if (!isFirstWrite) return new Promise(() => undefined);
            isFirstWrite = false;
            return Promise.resolve({ ok: false, status: 500, body: {} });
          }
          return Promise.resolve({
            ok: true,
            status: 200,
            body: {
              enabled: true,
              url: "https://m.relay.example/",
              grants: {
                workspace: { emails: [], email_domains: [] },
                services: {},
              },
            },
          });
        },
      },
    );
    type(renderTab(share), "erin@example.org");
    pressAdd(renderTab(share));
    await settle();
    type(renderTab(share), "dan@example.org");
    pressAdd(renderTab(share));
    return { share, setNow: (ms: number) => (now = ms) };
  }

  it("marks the failed row, the saving row and the new row each on its own", async () => {
    const { share } = await mixedPanel();

    const root = renderTab(share);
    const failed = rowByText(root, "erin@example.org");
    const saving = rowByText(root, "dan@example.org");

    expect(allText(failed)).toContain("Could not save");
    expect(allText(failed)).toContain("Retry");
    expect(allText(saving)).toContain("Securely granting access");
    expect(allText(saving)).not.toContain("Could not save");
  });

  it("fades the highlight off a row once its moment has passed", async () => {
    const { share, setNow } = await mixedPanel();

    expect(
      classTokensOf(rowByText(renderTab(share), "dan@example.org")),
    ).toContain("grant-row-added");

    setNow(1000 + 60_000);

    expect(
      classTokensOf(rowByText(renderTab(share), "dan@example.org")),
    ).not.toContain("grant-row-added");
  });

  it("never highlights a row the workspace handed back", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {},
      },
    });

    expect(
      classTokensOf(rowByText(renderTab(share), "friend@example.com")),
    ).not.toContain("grant-row-added");
  });
});

describe("ShareTab on an app pane", () => {
  it("refuses beside the line that says what is already inherited", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: {
          emails: ["one@example.com"],
          email_domains: ["acme.example"],
        },
        services: {},
      },
    });
    share.selectTarget("web");

    chooseKind(renderTab(share), "email_domain");
    type(renderTab(share), "gmail.com");
    pressAdd(renderTab(share));

    const root = renderTab(share);
    expect(allText(byId(root, "ws-share-inherited"))).toContain(
      "2 permissions inherited from the whole workspace",
    );
    expect(allText(byId(root, "ws-share-refusal"))).toBe(
      "gmail.com cannot be granted permissions because it is a public email provider.",
    );
    expect(share.grantCount("web")).toBe(0);
  });

  it("says the app cannot be opened even when only the workspace grants anyone", async () => {
    const share = await readyPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {},
      },
    });
    share.selectTarget("web");

    expect(share.grantCount("web")).toBe(0);
    expect(allText(byId(renderTab(share), "ws-share-off-notice"))).toBe(
      "This app cannot be accessed while sharing is off. " +
        "Your list of permissions is preserved but inactive.",
    );
  });
});

describe("ShareTab grantee rows", () => {
  /** One grant of every kind: an account with a picture, an account without,
   * an invited address, and a domain. */
  async function everyKindPanel(): Promise<SharePanelModel> {
    return publishedPanel({
      grants: {
        workspace: {
          users: ["user-2", "user-3"],
          emails: ["newcomer@example.com"],
          email_domains: ["acme.example"],
        },
        services: {},
      },
      identities: {
        "user-2": {
          user_id: "user-2",
          email: "carol@example.net",
          display_name: "Carol Reyes",
          profile_picture_url: "https://pictures.example/carol.png",
        },
        "user-3": {
          user_id: "user-3",
          email: "frank@example.net",
          display_name: "Frank Ito",
          profile_picture_url: null,
        },
      },
    });
  }

  /** The lead box a row starts with. */
  function leadBox(row: AnyVnode): AnyVnode {
    const lead = collectVnodes(row).find((vnode) => {
      const tokens = classTokensOf(vnode);
      return tokens.includes("h-6") && tokens.includes("rounded-full");
    });
    expect(lead, "row has no lead box").toBeDefined();
    return lead as AnyVnode;
  }

  it("leads an account row with its picture, or with its initials", async () => {
    const share = await everyKindPanel();

    const root = renderTab(share);
    const picture = collectVnodes(leadBox(rowByText(root, "Carol Reyes"))).find(
      (vnode) => vnode.tag === "img",
    );
    expect(attrsOf(picture as AnyVnode).src).toBe(
      "https://pictures.example/carol.png",
    );
    expect(allText(leadBox(rowByText(root, "Frank Ito")))).toBe("FI");
  });

  it("leads an invited address with a dashed box, and a domain with an @", async () => {
    const share = await everyKindPanel();

    const root = renderTab(share);
    const invited = leadBox(rowByText(root, "newcomer@example.com"));
    expect(classTokensOf(invited)).toContain("border-dashed");
    expect(
      collectVnodes(invited).some(
        (vnode) => attrsOf(vnode).name === "user-plus",
      ),
    ).toBe(true);

    const domain = leadBox(rowByText(root, "Anyone at acme.example"));
    expect(classTokensOf(domain)).toContain("border-dashed");
    expect(allText(domain)).toBe("@");
  });

  it("says nobody hears about a domain grant, on the row that made it", async () => {
    const share = await everyKindPanel();

    const row = rowByText(renderTab(share), "Anyone at acme.example");

    expect(attrsOf(row)["data-tooltip"]).toBe(
      "People at acme.example will not hear about this unless you send " +
        "them the link yourself",
    );
  });

  it("says an invited address has not signed up only once it is saved", async () => {
    const share = await everyKindPanel();

    expect(
      allText(rowByText(renderTab(share), "newcomer@example.com")),
    ).toContain("(hasn't signed up yet)");
    expect(allText(rowByText(renderTab(share), "Carol Reyes"))).not.toContain(
      "signed up",
    );
  });
});

describe("ShareTab provisioning trouble", () => {
  /** A published workspace whose link is not live yet. */
  async function stuckPanel(): Promise<SharePanelModel> {
    const share = await publishedPanel();
    share.isLive = false;
    return share;
  }

  it("says a halted bring-up only publishing again can clear", async () => {
    const share = await stuckPanel();
    share.gatewayState = "halted";
    share.gatewayError = "the certificate order was refused.";

    const trouble = byId(renderTab(share), "ws-share-gateway-trouble");

    expect(allText(trouble)).toContain("the certificate order was refused");
    expect(allText(trouble)).toContain(
      "Turn sharing off and on again to retry",
    );
  });

  it("says when a failed attempt will be tried again", async () => {
    const share = await stuckPanel();
    share.gatewayState = "retrying";
    share.gatewayFailedAttemptCount = 2;
    share.gatewayError = "the relay refused the tunnel";

    const trouble = byId(renderTab(share), "ws-share-gateway-trouble");

    expect(allText(trouble)).toContain("The last attempt failed");
    expect(allText(trouble)).toContain("the relay refused the tunnel");
  });

  it("says nothing about trouble while the bring-up is going well", async () => {
    const share = await stuckPanel();

    expect(byId(renderTab(share), "ws-share-gateway-trouble")).toBeUndefined();
  });
});

describe("ShareTab moved address", () => {
  it("says the old links no longer work when the read moved the share", async () => {
    const share = await publishedPanel();
    expect(allText(renderTab(share))).not.toContain("moved to a new address");

    share.migratedDomainFrom = "old.relay.example";

    expect(allText(renderTab(share))).toContain(
      "Sharing moved to a new address. Links you shared before no longer work.",
    );
  });
});
