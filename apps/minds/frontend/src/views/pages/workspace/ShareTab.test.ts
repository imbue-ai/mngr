import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  classesOf,
  collapsibleTrigger,
  collectVnodes,
  errorMarks,
  heldFetch,
  jsonResponse,
  pressCollapsible,
  renderDeep,
  renderRoot,
  required,
  settle,
  SHARING_LOCAL_READ_SUFFIX,
  UNVERIFIED_REFUSAL,
  sharePanelOptions,
  withAttr,
  withMindsNative,
} from "../../../testing";
import type {
  ErrorAction,
  RequestResult,
  ResponseBody,
} from "../../../models/errors";
import { createAppQueryClient } from "../../../models/queryClient";
import type {
  MachineSharingResponse,
  VisitingAgentEntry,
} from "../../../models/workspaceOptions";
import { RESOLVE_USER_URL } from "../../../models/workspaceOptions";
import type { Grant, SharePanelModelOptions } from "../../../models/sharePanel";
import { SharePanelModel } from "../../../models/sharePanel";
import { slackConnection } from "../../../models/workspacePermissions.testing";
import { clearAppContextForTests, registerAppContext } from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import { ShellState } from "../../shell/shell-state";
import { ownerSectionQuery } from "../settings/settings-route";
import { shareTargetIconMarkup } from "../../components/appIcon";
import { QR_CODE_ROW_MIN_HEIGHT_CLASS, QrCode } from "../../components/QrCode";
import { errorDialogs } from "../../../models/errorDialog";
import {
  NO_RESPONSE_SENTENCE,
  fallbackErrorSentence,
} from "../../../models/errors";
import { CoachBubble } from "../../components/CoachBubble";
import { Modal } from "../../components/Modal";
import { Skeleton, SkeletonRegion } from "../../components/Skeleton";
import { Spinner } from "../../components/Spinner";
import type { ShareTabAttrs } from "./ShareTab";
import { ShareTab, sharingNav } from "./ShareTab";

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
  overrides: Partial<SharePanelModelOptions> = {},
): Promise<SharePanelModel> {
  return readyPanel(
    { enabled: true, url: "https://m.relay.example/", ...response },
    overrides,
  );
}

/** A panel whose reads never answer, so everything drawn from it stays loading. */
function loadingPanel(): SharePanelModel {
  const share = new SharePanelModel(
    sharePanelOptions({ fetchJson: () => new Promise(() => undefined) }),
  );
  void share.load();
  return share;
}

/** A loaded panel whose every read failed, so it has no document to draw; `urls` lists what it asked for. */
async function failedPanel(): Promise<{ share: SharePanelModel; urls: string[] }> {
  const urls: string[] = [];
  const share = new SharePanelModel(
    sharePanelOptions({
      fetchJson: (url) => {
        urls.push(url);
        return Promise.resolve({ ok: false, status: 502, body: { error: "relay down" } });
      },
    }),
  );
  await share.load();
  return { share, urls };
}

/** What Settings hands the page besides the panel: the workspace's name and colour, and its connections. */
type PageAttrs = Partial<Omit<ShareTabAttrs, "share">>;

function tabAttrs(share: SharePanelModel, page: PageAttrs = {}): ShareTabAttrs {
  return {
    share,
    workspaceName: "alpha",
    accent: null,
    connections: null,
    onOpenConnection: () => undefined,
    ...page,
  };
}

function renderTab(share: SharePanelModel, page: PageAttrs = {}): m.Vnode {
  return renderRoot(ShareTab, tabAttrs(share, page));
}

/** The same, rendered through its components, for the assertions that are
 * about the markup a component draws rather than what was handed to it. */
function renderTabDeep(share: SharePanelModel, page: PageAttrs = {}): m.Vnode {
  return renderDeep(renderTab(share, page)) as m.Vnode;
}

/** The rail entries Settings nests under its Sharing entry. */
function renderNav(share: SharePanelModel): m.Children {
  return sharingNav(share);
}

/** Draw the panel repeatedly against one component instance, as a mount does:
 * the dialog and the copy confirmation live in the view's own closure, which a
 * fresh instance per draw would forget. */
function mountedTab(share: SharePanelModel, page: PageAttrs = {}): () => m.Vnode {
  const instance = ShareTab() as unknown as m.Component;
  return () => {
    const vnode = m(instance, tabAttrs(share, page) as unknown as m.Attributes) as m.Vnode;
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

/** The error an error display with this id was handed, or undefined. */
function errorOf(
  root: unknown,
  id: string,
): { message: string; details: string } | undefined {
  const display = byId(root, id);
  return display === undefined
    ? undefined
    : (attrsOf(display).error as { message: string; details: string });
}

function rowByText(root: unknown, text: string): AnyVnode {
  const row = grantRows(root).find((vnode) => allText(vnode).includes(text));
  expect(row, `no grant row mentioning ${text}`).toBeDefined();
  return row as AnyVnode;
}

/** The note saying whether a row's person has opened the workspace. */
function accessNoteOf(row: AnyVnode): AnyVnode | undefined {
  return withAttr(row, "data-grant-access")[0];
}

/** A row's first line: its name and its controls. */
function firstLineOf(row: AnyVnode): AnyVnode {
  const [line] = row.children as AnyVnode[];
  expect(line, "the row has no first line").toBeDefined();
  return line;
}

/** The line under a row's name, saying what is known about its person. */
function detailsOf(row: AnyVnode): AnyVnode | undefined {
  return withAttr(row, "data-grant-details")[0];
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

const PAGE_BLOCK_IDS = [
  "ws-share-off-notice",
  "ws-share-title",
  "ws-share-open-elsewhere",
  "ws-share-invite",
  "ws-share-agents",
  "ws-share-connections",
  "ws-share-apps",
  "ws-share-publish",
];

/** The page's blocks, by id, in the order they are drawn. */
function pageBlocks(root: unknown): string[] {
  return collectVnodes(root)
    .map((vnode) => String(attrsOf(vnode).id))
    .filter((id) => PAGE_BLOCK_IDS.includes(id));
}

/** The line the list of who has access folds behind, on a page rendered through. */
function accessTrigger(root: unknown): AnyVnode {
  return collapsibleTrigger(root, "ws-share-access");
}

/** Unfold the list of who has access on a mounted page, as a press on its line does. */
function openAccessList(draw: () => m.Vnode): void {
  pressCollapsible(renderDeep(draw()), "ws-share-access");
}

function qrCodeOf(root: unknown): AnyVnode {
  return required(collectVnodes(root).find((vnode) => vnode.tag === QrCode));
}

describe("ShareTab publish widget", () => {
  it("heads the switch Enable sharing and web access, reading No, over the sharing sentence", async () => {
    const share = await readyPanel();

    const widget = byId(renderTab(share), "ws-share-publish");

    expect(widget).toBeDefined();
    expect(allText(widget)).toContain("Enable sharing and web access");
    expect(allText(widget)).not.toMatch(/publish/i);
    expect(allText(widget)).toContain(
      "Sharing gives this workspace an address on the internet, which allows you " +
        "to access it over the web and on mobile. No one else will get access to " +
        "your workspace unless you explicitly grant them access.",
    );
    const control = byId(widget, "ws-share-publish-switch");
    expect(attrsOf(control as AnyVnode).role).toBe("switch");
    expect(attrsOf(control as AnyVnode)["aria-label"]).toBe(
      "Enable sharing and web access",
    );
    expect(attrsOf(control as AnyVnode)["aria-checked"]).toBe("false");
    expect(attrsOf(control as AnyVnode)["aria-busy"]).toBeUndefined();
    expect(classTokensOf(control as AnyVnode)).not.toContain("is-settling");
    expect(allText(byId(widget, "ws-share-publish-word"))).toBe("No");
  });

  it("reads Yes and keeps the sharing sentence once the workspace is published", async () => {
    const share = await publishedPanel();

    const widget = byId(renderTab(share), "ws-share-publish");

    expect(
      attrsOf(byId(widget, "ws-share-publish-switch") as AnyVnode)[
        "aria-checked"
      ],
    ).toBe("true");
    expect(allText(byId(widget, "ws-share-publish-word"))).toBe("Yes");
    expect(allText(widget)).toContain(
      "Sharing gives this workspace an address on the internet, which allows you " +
        "to access it over the web and on mobile. No one else will get access to " +
        "your workspace unless you explicitly grant them access.",
    );
  });

  it("stands the checking placeholder where the switch goes while the first read is out", () => {
    const share = loadingPanel();

    const root = renderTab(share);

    expect(byId(root, "ws-share-publish")).toBeDefined();
    expect(byId(root, "ws-share-publish-switch")).toBeUndefined();
    const placeholder = byId(root, "ws-share-publish-unknown") as AnyVnode;
    expect(attrsOf(placeholder)["aria-label"]).toBe("Checking whether sharing is enabled");
  });

  it("spins the wait at the off side, in place of the switch, while an unread status is read again", async () => {
    let readCount = 0;
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: () => {
          readCount += 1;
          return readCount > 1
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
                  publication_unknown_reason: "UNREADABLE",
                },
              });
        },
      }),
    );
    await share.load();
    void share.load();

    const root = renderTab(share);
    const widget = byId(root, "ws-share-publish");

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
    expect(allText(byId(widget, "ws-share-publish-word"))).toBe("");
    expect(allText(widget)).not.toMatch(/\bChecking\b/);
    expect(collectVnodes(widget).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
    for (const control of addControls(root))
      expect(attrsOf(control)["data-tooltip"]).toBe(
        "Permissions cannot be granted until the sharing status has loaded",
      );
  });

  it("greys the switch out, rather than drawing it off, when the first read failed", async () => {
    const { share } = await failedPanel();

    const root = renderTab(share);
    const widget = byId(root, "ws-share-publish");

    expect(byId(widget, "ws-share-publish-switch")).toBeUndefined();
    const placeholder = byId(widget, "ws-share-publish-unknown") as AnyVnode;
    expect(classTokensOf(placeholder)).toEqual(
      expect.arrayContaining(["perm-switch", "is-unknown"]),
    );
    expect(classTokensOf(placeholder)).not.toContain("is-checking");
    expect(attrsOf(placeholder)["aria-busy"]).toBeUndefined();
    expect(attrsOf(placeholder)["aria-label"]).toBe("Sharing status unknown");
    expect(allText(byId(widget, "ws-share-publish-word"))).toBe("");
    expect(allText(widget)).not.toMatch(/\b(Checking|Unknown)\b/);
    // The failure is said among the page's notices, not beside the switch.
    expect(byId(widget, "ws-share-load-error")).toBeUndefined();
    expect(errorOf(root, "ws-share-load-error")).toEqual({
      message:
        "Couldn't check who this workspace is shared with. Try again. If this persists, report a bug.",
      details: expect.stringContaining("relay down"),
    });
    expect(collectVnodes(widget).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
  });

  /** A loaded panel whose publish the backend refuses with `body`. */
  async function refusingPanel(status: number, body: ResponseBody): Promise<SharePanelModel> {
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          init?.method === "PUT"
            ? Promise.resolve({ ok: false, status, body })
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
    await share.publish();
    return share;
  }

  it("offers the shared-workspaces page for a full share quota, and nothing more for a waitlisted account", async () => {
    const quota = await refusingPanel(403, {
      error: "Could not publish this workspace: Shared workspace limit reached (3 of 3).",
      reason: "SHARE_LIMIT_REACHED",
      code: "quota_exceeded",
      shared_workspaces_path: "/workspaces/shared",
      limit: 3,
      current: 3,
    });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const quotaError = attrsOf(byId(renderTab(quota), "ws-share-publish-error") as AnyVnode);
    expect(quotaError.error).toEqual({
      message:
        "Your plan allows 3 shared workspaces and 3 are shared already. *Turn sharing off for one of them, then try again.*",
      details: expect.stringContaining("Shared workspace limit reached (3 of 3)"),
    });
    const actions = quotaError.actions as { label: string; onclick: () => void }[];
    expect(actions.map((action) => action.label)).toEqual(["Review shared workspaces"]);
    actions[0].onclick();
    expect(routeSet).toHaveBeenCalledWith("/workspaces/shared");

    const waitlisted = await refusingPanel(502, {
      error: "Could not publish this workspace: waitlisted",
      reason: "WAITLISTED",
    });
    const waitlistedError = attrsOf(byId(renderTab(waitlisted), "ws-share-publish-error") as AnyVnode);
    expect((waitlistedError.error as { message: string }).message).toContain("still on the waitlist");
    expect(waitlistedError.actions).toEqual([]);
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
    expect(allText(widget)).toContain("Yes");
    expect(byId(widget, "ws-share-publish-unknown")).toBeUndefined();
    expect(collectVnodes(widget).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
  });

  it("puts the switch last on the whole workspace's page, under a rule of its own", async () => {
    const share = await publishedPanel();

    const vnodes = collectVnodes(renderTab(share));
    const widgetIdx = vnodes.findIndex(
      (vnode) => attrsOf(vnode).id === "ws-share-publish",
    );
    const lastRuleIdx = vnodes.map((vnode) => vnode.tag).lastIndexOf("hr");

    // A rule has no children, so the vnode after it is what it rules off.
    expect(widgetIdx).toBe(lastRuleIdx + 1);
    expect(pageBlocks(vnodes).at(-1)).toBe("ws-share-publish");
    expect(
      collectVnodes(vnodes[widgetIdx]).some((vnode) => vnode.tag === "hr"),
    ).toBe(false);
  });

  it("draws the switch on no app's page: sharing is turned off in one place", async () => {
    const share = await publishedPanel();
    share.selectTarget("web");

    const root = renderTab(share);

    expect(byId(root, "ws-share-publish")).toBeUndefined();
    expect(byId(root, "ws-share-publish-switch")).toBeUndefined();
  });

  it("says a failed publish among the notices under the title, above the link", async () => {
    const share = await refusingPanel(502, {
      error: "Could not publish this workspace: waitlisted",
      reason: "WAITLISTED",
    });

    const ids = collectVnodes(renderTab(share)).map((vnode) =>
      String(attrsOf(vnode).id),
    );

    expect(ids.indexOf("ws-share-publish-error")).toBeGreaterThan(
      ids.indexOf("ws-share-title"),
    );
    expect(ids.indexOf("ws-share-publish-error")).toBeLessThan(
      ids.indexOf("ws-share-open-elsewhere"),
    );
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
    expect(allText(block)).toContain("Connecting this workspace");
    expect(allText(block)).not.toContain("Creating link");
    expect(allText(block)).not.toContain("Setting up encryption");
    expect(allText(block)).not.toContain("Checking the link works");
    expect(allText(block)).toContain(
      "People can be added while the link is being prepared.",
    );
    expect(collectVnodes(block).some((vnode) => vnode.tag === Spinner)).toBe(
      true,
    );
  });
});

describe("sharingNav", () => {
  it("lists the whole workspace first, then each app, with how many people each admits", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: {
          emails: ["friend@example.com"],
          email_domains: ["example.org"],
        },
        services: { web: { emails: ["dev@example.com"], email_domains: [] } },
      },
    });

    const entries = navEntries(renderNav(share));

    expect(entries.map((entry) => attrsOf(entry)["data-share-target"])).toEqual(
      [WHOLE, "web", "docs"],
    );
    expect(allText(entries[0])).toContain("Full workspace");
    expect(allText(withAttr(entries[0], "data-share-count"))).toBe("2");
    expect(allText(withAttr(entries[1], "data-share-count"))).toBe("1");
    // A target that admits nobody says so by carrying no count at all.
    expect(withAttr(entries[2], "data-share-count")).toEqual([]);
    expect(allText(entries[2])).toBe("docs");
  });

  it("calls each app what the workspace calls it", async () => {
    const share = await readyPanel(
      {},
      {
        appServices: ["files", "web"],
        serviceDisplayNames: { files: "File Viewer" },
      },
    );

    const entries = navEntries(renderNav(share));

    expect(allText(entries[1])).toContain("File Viewer");
    // An app that registered no display name is listed by its own name.
    expect(allText(entries[2])).toContain("web");
  });

  it("shows an app's own name only when another app is called the same", async () => {
    const share = await readyPanel(
      {},
      {
        appServices: ["hud", "hud-dev", "files"],
        serviceDisplayNames: {
          hud: "Imbue HUD",
          "hud-dev": "Imbue HUD",
          files: "File Viewer",
        },
      },
    );

    const entries = navEntries(renderNav(share));

    expect(allText(entries[1])).toContain("hud");
    expect(allText(entries[2])).toContain("hud-dev");
    // The one name nothing collides with stands alone.
    expect(allText(entries[3])).not.toContain("files");
  });

  it("holds only the entries, with no rule or label between the whole workspace and the apps", async () => {
    const share = await publishedPanel();

    expect(
      (renderNav(share) as AnyVnode[]).map((child) => child.tag),
    ).toEqual(["button", "button", "button"]);
  });

  it("presses the entry of the page on screen, and no other", async () => {
    const share = await publishedPanel();
    const pressed = (): unknown[] =>
      navEntries(renderNav(share))
        .filter((entry) => attrsOf(entry)["aria-pressed"] === "true")
        .map((entry) => attrsOf(entry)["data-share-target"]);

    expect(pressed()).toEqual([WHOLE]);

    share.selectTarget("docs");

    expect(pressed()).toEqual(["docs"]);
  });

  it("puts each count in a badge rather than leaving it a bare numeral", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {
          web: { emails: ["dev@example.com"], email_domains: [] },
          docs: { emails: ["writer@example.com"], email_domains: [] },
        },
      },
    });

    const entries = navEntries(renderNav(share));
    expect(entries).toHaveLength(3);
    for (const entry of entries) {
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

  it("marks only an app whose address the backend does not know yet, in a tooltip rather than in words", async () => {
    const share = await publishedPanel();

    const entries = navEntries(renderNav(share));

    expect(entries.map((entry) => attrsOf(entry)["data-share-no-link"])).toEqual([
      undefined,
      undefined,
      "true",
    ]);
    expect(attrsOf(entries[2])["data-tooltip"]).toBe(
      "No link yet: this app gets one when it first starts.",
    );
    expect(attrsOf(entries[1])["data-tooltip"]).toBeUndefined();
    // The rail is too narrow for the words themselves.
    expect(allText(entries[2])).toBe("docs");
  });

  it("selects the target it is pressed on, which the page then draws", async () => {
    const share = await publishedPanel();

    const entries = navEntries(renderNav(share));
    (attrsOf(entries[1]).onclick as () => void)();

    expect(share.currentTarget).toBe("web");
    expect(allText(byId(renderTab(share), "ws-share-title"))).toBe("Share the web app");
  });

});

describe("sharingNav system apps", () => {
  it("lists the system apps after the apps, under a label of their own", async () => {
    const share = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      { systemServices: ["chat", "terminal"] },
    );

    const nav = renderNav(share) as AnyVnode[];

    expect(
      navEntries(nav).map((entry) => attrsOf(entry)["data-share-target"]),
    ).toEqual([WHOLE, "web", "docs", "chat", "terminal"]);
    // The label sits between the apps and the system apps, and is not an entry.
    expect(nav.map((child) => child.tag)).toEqual([
      "button",
      "button",
      "button",
      "div",
      "button",
      "button",
    ]);
    expect(allText(nav[3])).toBe("System apps");
  });

  it("lists every system app whether or not it admits anyone, with nothing to open first", async () => {
    const share = await readyPanel(
      {
        grants: {
          workspace: { emails: [], email_domains: [] },
          services: {
            chat: { emails: ["pair@example.com"], email_domains: [] },
          },
        },
      },
      { systemServices: ["chat", "terminal"] },
    );

    const entries = navEntries(renderDeep(renderNav(share)));

    expect(
      entries.slice(3).map((entry) => [
        attrsOf(entry)["data-share-target"],
        allText(withAttr(entry, "data-share-count")),
      ]),
    ).toEqual([
      ["chat", "1"],
      ["terminal", ""],
    ]);
    expect(
      collectVnodes(renderDeep(renderNav(share))).some(
        (vnode) => attrsOf(vnode)["aria-expanded"] !== undefined,
      ),
    ).toBe(false);
  });

  it("draws no label for a workspace with no system apps", async () => {
    const share = await publishedPanel();

    expect(allText(renderNav(share))).not.toContain("System apps");
  });

  it("opens a system app's page from its entry", async () => {
    const share = await readyPanel({}, { systemServices: ["chat"] });

    (attrsOf(navEntries(renderNav(share))[3]).onclick as () => void)();

    expect(share.currentTarget).toBe("chat");
    expect(allText(byId(renderTab(share), "ws-share-title"))).toBe("Share the chat app");
  });
});

describe("ShareTab link section", () => {
  const LINK = "https://shell-r4nd.m.relay.example/";
  const SECURE_LINE =
    "This is a secure link: only people you've granted access can open it.";

  it("shows the link, a QR code of it and who it opens for while published", async () => {
    const share = await publishedPanel();

    const section = byId(renderTab(share), "ws-share-open-elsewhere");

    expect(allText(section)).toContain("Open “alpha” on another device");
    expect(allText(section)).toContain(
      "Access your workspace from your phone or another computer.",
    );
    const link = byId(section, "ws-share-link");
    expect(
      collectVnodes(link).some((vnode) => attrsOf(vnode).value === LINK),
    ).toBe(true);
    expect(byId(link, "ws-share-copy")).toBeDefined();
    expect(attrsOf(qrCodeOf(section)).value).toBe(LINK);
    expect(allText(section)).toContain(SECURE_LINE);
    expect(byId(section, "ws-share-link-off")).toBeUndefined();
  });

  it("draws the code black on white whatever the scheme, named for a screen reader", async () => {
    const share = await publishedPanel();

    const code = withAttr(renderTabDeep(share), "data-qr-code")[0];

    expect(attrsOf(code)["data-qr-code"]).toBe("ready");
    expect(code.tag).toBe("svg");
    expect(attrsOf(code).role).toBe("img");
    expect(attrsOf(code)["aria-label"]).toBe("QR code for the link to alpha");
    expect(classTokensOf(code)).toEqual(
      expect.arrayContaining(["w-24", "h-24", "bg-white"]),
    );
  });

  it("speaks of the app, and carries the app's link, on an app's page", async () => {
    const share = await publishedPanel();
    share.selectTarget("web");

    const section = byId(renderTab(share), "ws-share-open-elsewhere");

    expect(allText(section)).toContain("Open “web” on another device");
    expect(allText(section)).toContain(
      "Access the app from your phone or another computer.",
    );
    expect(attrsOf(qrCodeOf(section)).value).toBe(
      "https://web-r4nd.m.relay.example/",
    );
  });

  it("waits for the link rather than showing an empty field, over an empty box for the code", async () => {
    const share = await publishedPanel();
    share.isLive = false;
    share.selectTarget("docs");

    const root = renderTab(share);
    const section = byId(root, "ws-share-open-elsewhere");

    // Published, so the row is marked as the link's though there is none to show yet.
    expect(allText(byId(section, "ws-share-link"))).toContain(
      "Preparing the link",
    );
    expect(byId(root, "ws-share-copy")).toBeUndefined();
    expect(attrsOf(qrCodeOf(section)).value).toBeNull();
    expect(
      attrsOf(withAttr(renderDeep(section), "data-qr-code")[0])["data-qr-code"],
    ).toBe("empty");
    expect(allText(section)).not.toContain(SECURE_LINE);
  });

  it("keeps the section while sharing is off, saying where the link will appear", async () => {
    const share = await readyPanel();

    const root = renderTab(share);
    const section = byId(root, "ws-share-open-elsewhere");

    expect(allText(section)).toContain("Open “alpha” on another device");
    expect(allText(byId(section, "ws-share-link-off"))).toBe(
      "The link appears here when sharing is on.",
    );
    expect(attrsOf(qrCodeOf(section)).value).toBeNull();
    expect(allText(section)).not.toContain(SECURE_LINE);
    // Neither id exists while unpublished: what waits on a link arriving or
    // leaving watches for exactly these.
    expect(byId(root, "ws-share-link")).toBeUndefined();
    expect(byId(root, "ws-share-copy")).toBeUndefined();
  });

  it("marks the link's row from the moment sharing is on until it is off again", async () => {
    let isEnabled = false;
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) => {
          if (init?.method === "PUT") isEnabled = true;
          if (init?.method === "DELETE") isEnabled = false;
          return Promise.resolve({
            ok: true,
            status: 200,
            body: {
              enabled: isEnabled,
              url: isEnabled ? "https://m.relay.example/" : null,
              grants: {
                workspace: { emails: [], email_domains: [] },
                services: {},
              },
            },
          });
        },
      },
    );
    expect(byId(renderTab(share), "ws-share-link")).toBeUndefined();

    await share.publish();
    await settle();

    expect(share.isPublished).toBe(true);
    expect(byId(renderTab(share), "ws-share-link")).toBeDefined();
    expect(byId(renderTab(share), "ws-share-link-off")).toBeUndefined();

    await share.unpublish();
    await settle();

    expect(share.isPublished).toBe(false);
    expect(byId(renderTab(share), "ws-share-link")).toBeUndefined();
    expect(byId(renderTab(share), "ws-share-link-off")).toBeDefined();
  });
});

describe("ShareTab while its first read is out", () => {
  it("draws the page's frame at once, with the title and every section in place", () => {
    const share = loadingPanel();

    const root = renderTab(share);

    expect(pageBlocks(root)).toEqual([
      "ws-share-title",
      "ws-share-open-elsewhere",
      "ws-share-invite",
      "ws-share-agents",
      "ws-share-apps",
      "ws-share-publish",
    ]);
    expect(allText(byId(root, "ws-share-title"))).toBe("Share alpha");
  });

  it("says it is checking in the link's place, with an empty box for the code, and calls nothing off", () => {
    const share = loadingPanel();

    const root = renderTabDeep(share);

    const checking = byId(renderTab(share), "ws-share-link-checking") as AnyVnode;
    expect(allText(checking)).toBe("Checking whether sharing is enabled");
    expect(collectVnodes(checking).some((vnode) => vnode.tag === Spinner)).toBe(true);
    expect(byId(root, "ws-share-link")).toBeUndefined();
    expect(byId(root, "ws-share-link-off")).toBeUndefined();
    expect(attrsOf(withAttr(root, "data-qr-code")[0])["data-qr-code"]).toBe("empty");
    expect(allText(root)).not.toContain("Sharing is off");
    expect(allText(root)).not.toContain("The link appears here when sharing is on.");
  });

  it("holds a place for who has access, announced as one region, rather than calling the list empty or unread", () => {
    const share = loadingPanel();

    const drawn = collectVnodes(renderTabDeep(share));
    const announced = drawn.filter(
      (vnode) => attrsOf(vnode)["aria-label"] === "Loading who has access",
    );

    expect(announced).toHaveLength(1);
    expect(attrsOf(announced[0]).role).toBe("status");
    expect(attrsOf(announced[0])["aria-busy"]).toBe("true");
    const blocks = drawn.filter((vnode) => classTokensOf(vnode).includes("animate-pulse"));
    expect(blocks.length).toBeGreaterThan(0);
    for (const block of blocks) expect(attrsOf(block)["aria-hidden"]).toBe("true");
    const text = allText(renderTabDeep(share));
    expect(text).not.toContain("The list of who has access couldn't be read.");
    expect(byId(renderTab(share), "ws-share-access")).toBeUndefined();
  });

  it("draws the add row and the email button disabled, saying the status is still being checked", () => {
    const share = loadingPanel();

    const root = renderTab(share);

    const [, , addButton] = addControls(root);
    expect(attrsOf(addButton)["aria-disabled"]).toBe("true");
    const emailButton = byId(root, "ws-share-mobile-link-btn") as AnyVnode;
    expect(attrsOf(emailButton)["aria-disabled"]).toBe("true");
    expect(attrsOf(emailButton)["data-tooltip"]).toBe("Checking whether sharing is enabled");
  });

  it("checks on an app's page on the same terms, without saying the app has no link", () => {
    const share = loadingPanel();
    share.selectTarget("web");

    const root = renderTab(share);

    expect(allText(byId(root, "ws-share-title"))).toBe("Share the web app");
    expect(byId(root, "ws-share-link-checking")).toBeDefined();
    expect(byId(root, "ws-share-app-no-link")).toBeUndefined();
  });

  /** The agents section's own hooks, as a mount would run them on its first draw and on each redraw. */
  function hooksOf(root: unknown): { oncreate: () => void; onupdate: () => void } {
    return attrsOf(byId(root, "ws-share-agents") as AnyVnode) as unknown as {
      oncreate: () => void;
      onupdate: () => void;
    };
  }

  it("does not ask for the agents until there is a document to draw, then asks once", async () => {
    const urls: string[] = [];
    const held = new Promise<never>(() => undefined);
    let answerStatus: (result: RequestResult) => void = () => undefined;
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: (url) => {
          urls.push(url);
          if (url.endsWith("/agents")) return Promise.resolve({ ok: true, status: 200, body: { agents: [] } });
          if (url.endsWith("/invitation-outcomes")) return held;
          return new Promise((resolve) => {
            answerStatus = resolve;
          });
        },
      }),
    );
    void share.load();

    hooksOf(renderTab(share)).oncreate();
    hooksOf(renderTab(share)).onupdate();
    expect(urls.filter((url) => url.endsWith("/agents"))).toEqual([]);

    answerStatus({
      ok: true,
      status: 200,
      body: { enabled: false, url: null, grants: { workspace: { emails: [], email_domains: [] }, services: {} } },
    });
    await settle();
    hooksOf(renderTab(share)).onupdate();
    hooksOf(renderTab(share)).onupdate();

    expect(urls.filter((url) => url.endsWith("/agents"))).toHaveLength(1);
  });

  it("asks for the agents once the reads have failed with nothing to draw, so their place does not spin forever", async () => {
    const { share, urls } = await failedPanel();
    expect(share.isSharingDocumentKnown).toBe(false);

    hooksOf(renderTab(share)).oncreate();
    await settle();

    expect(urls.filter((url) => url.endsWith("/agents"))).toHaveLength(1);
    expect(byId(renderTab(share), "ws-share-agents-loading")).toBeUndefined();
  });

  function placeholderOf(share: SharePanelModel): AnyVnode | undefined {
    return collectVnodes(renderTab(share)).find((vnode) => vnode.tag === SkeletonRegion);
  }

  it("gives the pane over to the document as soon as one lands", async () => {
    const share = await publishedPanel();

    expect(placeholderOf(share)).toBeUndefined();
    expect(byId(renderTab(share), "ws-share-link")).toBeDefined();
    expect(byId(renderTab(share), "ws-share-title")).toBeDefined();
  });

  it("does not leave a read that failed pulsing forever", async () => {
    const { share } = await failedPanel();

    expect(placeholderOf(share)).toBeUndefined();
    expect(byId(renderTab(share), "ws-share-link-checking")).toBeUndefined();
    expect(errorOf(renderTab(share), "ws-share-load-error")?.details).toContain(
      "relay down",
    );
  });
});

describe("ShareTab while only the local read has answered", () => {
  const GRANTS = { workspace: { emails: ["friend@example.com"], email_domains: [] }, services: {} };

  /** A panel whose local read answers `local` at once, and whose fresh read answers only when released. */
  async function locallyKnownPanel(local: Partial<MachineSharingResponse>): Promise<{
    share: SharePanelModel;
    answerFresh: (document: Partial<MachineSharingResponse>) => Promise<void>;
  }> {
    let release: (result: RequestResult) => void = () => undefined;
    const share = new SharePanelModel(
      sharePanelOptions(
        {
          fetchJson: (url) => {
            if (url.endsWith(SHARING_LOCAL_READ_SUFFIX))
              return Promise.resolve({
                ok: true,
                status: 200,
                body: { enabled: false, url: null, grants: GRANTS, is_connector_pending: true, ...local },
              });
            if (url.endsWith("/invitation-outcomes") || url.endsWith("/agents"))
              return Promise.resolve({ ok: true, status: 200, body: { outcomes: [], agents: [] } });
            return new Promise((resolve) => {
              release = resolve;
            });
          },
        },
        { isLocalReadServed: true },
      ),
    );
    void share.load();
    await settle();
    return {
      share,
      answerFresh: async (document) => {
        release({ ok: true, status: 200, body: { enabled: false, url: null, grants: GRANTS, ...document } });
        await settle();
      },
    };
  }

  it("draws the link and its code from what is known here, while the switch is still being checked", async () => {
    const { share } = await locallyKnownPanel({ enabled: true, url: "https://m.relay.example/" });

    const root = renderTabDeep(share);

    expect(byId(root, "ws-share-link")).toBeDefined();
    expect(byId(root, "ws-share-link-checking")).toBeUndefined();
    expect(attrsOf(withAttr(root, "data-qr-code")[0])["data-qr-code"]).toBe("ready");
    expect(byId(root, "ws-share-publish-switch")).toBeUndefined();
    expect(attrsOf(byId(renderTab(share), "ws-share-publish-unknown") as AnyVnode)["aria-label"]).toBe(
      "Checking whether sharing is enabled",
    );
    expect(byId(root, "ws-share-off-notice")).toBeUndefined();
  });

  it("lists who has access and lets someone be added before Imbue Cloud has answered", async () => {
    const { share } = await locallyKnownPanel({ enabled: true, url: "https://m.relay.example/" });

    const root = renderTab(share);

    expect(allText(accessTrigger(renderTabDeep(share)))).toContain("1 has access");
    const [, , addButton] = addControls(root);
    expect(attrsOf(addButton)["aria-disabled"]).toBeUndefined();
    expect(attrsOf(byId(root, "ws-share-mobile-link-btn") as AnyVnode)["aria-disabled"]).toBeUndefined();
  });

  it("keeps checking in the link's place for a workspace that holds share materials but whose domain is not known here", async () => {
    const { share } = await locallyKnownPanel({ enabled: true, url: null });

    const root = renderTab(share);

    expect(byId(root, "ws-share-link-checking")).toBeDefined();
    expect(allText(root)).not.toContain("Preparing the link");
  });

  it("does not call a workspace off on the workspace's word alone", async () => {
    const { share } = await locallyKnownPanel({ enabled: false, url: null });

    const root = renderTab(share);

    expect(byId(root, "ws-share-off-notice")).toBeUndefined();
    expect(byId(root, "ws-share-link-off")).toBeUndefined();
    expect(byId(root, "ws-share-link-checking")).toBeDefined();
  });

  it("settles on what Imbue Cloud says once the fresh read answers", async () => {
    const { share, answerFresh } = await locallyKnownPanel({ enabled: true, url: "https://m.relay.example/" });

    await answerFresh({ enabled: false, url: null });
    const root = renderTab(share);

    expect(byId(root, "ws-share-off-notice")).toBeDefined();
    expect(byId(root, "ws-share-link-off")).toBeDefined();
    expect(byId(root, "ws-share-link")).toBeUndefined();
    expect(byId(root, "ws-share-publish-switch")).toBeDefined();
    expect(byId(root, "ws-share-publish-unknown")).toBeUndefined();
  });
});

describe("ShareTab rail while its first read is out", () => {
  it("keeps the rail's names but states no count and no missing link while it waits", () => {
    const share = loadingPanel();

    const entries = navEntries(renderNav(share));

    expect(entries).toHaveLength(3);
    expect(allText(entries[0])).toContain("Full workspace");
    for (const entry of entries) {
      expect(attrsOf(entry)["data-share-no-link"]).toBeUndefined();
      expect(attrsOf(entry)["data-tooltip"]).toBeUndefined();
      expect(withAttr(entry, "data-share-count")).toEqual([]);
      expect(collectVnodes(entry).some((vnode) => vnode.tag === Skeleton)).toBe(
        true,
      );
    }
  });

  it("gives the rail its counts once the document lands, in place of the skeletons", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: { web: { emails: ["dev@example.com"], email_domains: [] } },
      },
    });

    const entries = navEntries(renderNav(share));

    expect(
      entries.map((entry) => allText(withAttr(entry, "data-share-count"))),
    ).toEqual(["1", "1", ""]);
    for (const entry of entries)
      expect(collectVnodes(entry).some((vnode) => vnode.tag === Skeleton)).toBe(
        false,
      );
  });
});

describe("ShareTab mobile access link", () => {
  /** A published, live panel whose mobile-link route answers with `answer`. */
  async function linkablePanel(
    answer: ResponseBody = { outcome: "sent", recipient_email: "owner@example.com" },
    answeredAs: { ok: boolean; status: number } = { ok: true, status: 200 },
  ): Promise<SharePanelModel> {
    return publishedPanel(
      {},
      {
        fetchJson: (url: string) =>
          Promise.resolve(
            url.endsWith("/mobile-access-link")
              ? { ...answeredAs, body: answer }
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
  }

  function pressMobileLink(root: unknown): void {
    (
      attrsOf(byId(root, "ws-share-mobile-link-btn") as AnyVnode)
        .onclick as () => void
    )();
  }

  it("offers the control under the whole workspace's link, as a small button saying nothing until it is pressed", async () => {
    const share = await publishedPanel();

    const section = byId(renderTab(share), "ws-share-open-elsewhere");
    const control = byId(section, "ws-share-mobile-link");

    expect(allText(control).trim()).toBe("Email me a Mobile Access Link");
    const button = byId(control, "ws-share-mobile-link-btn") as AnyVnode;
    expect(attrsOf(button).size).toBe("xs");
    expect(attrsOf(button).variant).toBe("secondary");
    // Under the link's own row, not beside it.
    const ids = collectVnodes(section).map((vnode) => attrsOf(vnode).id);
    expect(ids.indexOf("ws-share-mobile-link")).toBeGreaterThan(
      ids.indexOf("ws-share-copy"),
    );
  });

  it("ends the column beside the code with the button, so the button's bottom edge is the code's", async () => {
    const share = await linkablePanel();
    pressMobileLink(renderTab(share));
    await settle();

    const section = byId(renderTab(share), "ws-share-open-elsewhere");
    const column = collectVnodes(section).find((vnode) =>
      classTokensOf(vnode).includes("justify-between"),
    ) as AnyVnode;

    // At least as tall as the code, with the button pushed to its foot.
    expect(classTokensOf(column)).toContain(QR_CODE_ROW_MIN_HEIGHT_CLASS);
    const children = (column.children as AnyVnode[]).filter(Boolean);
    expect(attrsOf(children[children.length - 1]).id).toBe("ws-share-mobile-link");
    // What the button reports sits under the column, so it cannot push the button off the code's edge.
    expect(allText(column)).not.toContain("Sent to");
    expect(allText(section)).toContain("Sent to owner@example.com");
  });

  it("offers the control on an app's page too, and sends that app", async () => {
    const share = await linkablePanel();
    share.selectTarget("web");

    const control = byId(renderTab(share), "ws-share-mobile-link");
    expect(allText(control).trim()).toBe("Email me a Mobile Access Link");
    expect(
      attrsOf(byId(control, "ws-share-mobile-link-btn") as AnyVnode)["aria-disabled"],
    ).toBeUndefined();

    pressMobileLink(renderTab(share));
    await settle();

    expect(allText(byId(renderTab(share), "ws-share-open-elsewhere"))).toContain(
      "Sent to owner@example.com",
    );
    // The confirmation belongs to the app's page, not to the workspace's.
    share.selectTarget("system_interface");
    expect(allText(byId(renderTab(share), "ws-share-open-elsewhere"))).not.toContain("Sent to");
  });

  it("disables the control on an app that has no link of its own yet, saying what brings one", async () => {
    const share = await publishedPanel();
    share.selectTarget("docs");

    const button = byId(renderTab(share), "ws-share-mobile-link-btn") as AnyVnode;

    expect(attrsOf(button)["aria-disabled"]).toBe("true");
    expect(attrsOf(button)["data-tooltip"]).toBe(
      "This app gets its link the first time it starts.",
    );
  });

  it("still draws the control while sharing is off, disabled, naming the switch that enables it", async () => {
    const share = await readyPanel();

    const button = byId(
      renderTab(share),
      "ws-share-mobile-link-btn",
    ) as AnyVnode;

    expect(attrsOf(button)["aria-disabled"]).toBe("true");
    expect(attrsOf(button)["data-tooltip"]).toBe(
      "Enable sharing and web access to access this link on mobile",
    );
  });

  it("leaves the control pressable while the workspace's address is still coming up", async () => {
    const share = await publishedPanel();
    share.isLive = false;

    const button = byId(
      renderTab(share),
      "ws-share-mobile-link-btn",
    ) as AnyVnode;

    expect(attrsOf(button)["aria-disabled"]).toBeUndefined();
  });

  it("names the address a sent message went to, and nothing more", async () => {
    const share = await linkablePanel();

    pressMobileLink(renderTab(share));
    await settle();
    const section = byId(renderTab(share), "ws-share-open-elsewhere");

    expect(allText(section)).toContain("Sent to owner@example.com");
    expect(allText(section)).not.toContain("Sent to owner@example.com.");
  });

  it("shows a failed send where the confirmation would have gone", async () => {
    const share = await linkablePanel({ outcome: "failed" });

    pressMobileLink(renderTab(share));
    await settle();

    expect(
      errorOf(
        byId(renderTab(share), "ws-share-open-elsewhere"),
        "ws-share-mobile-link-error",
      )?.message,
    ).toBe(
      "Couldn't send the email. Try again. If this persists, report a bug.",
    );
  });

  it("offers the verification email beside a refusal for an unverified account, and says how it went", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ sent: true }));
    vi.stubGlobal("fetch", fetchMock);
    const share = await linkablePanel(UNVERIFIED_REFUSAL, {
      ok: false,
      status: 409,
    });

    pressMobileLink(renderTab(share));
    await settle();

    const resend = collectVnodes(renderTab(share)).find(
      (vnode) => attrsOf(vnode)["data-resend-verification"] === "mobile-link",
    ) as AnyVnode;
    expect(allText(resend)).toBe("Send the email again");

    (attrsOf(resend).onclick as () => void)();
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const note = collectVnodes(renderTab(share)).find(
      (vnode) => attrsOf(vnode)["data-mobile-link-refusal"] !== undefined,
    ) as AnyVnode;
    expect(allText(note)).toContain("Sent another email to owner@example.com.");
    expect(errorMarks(note)).toHaveLength(0);
  });
});

describe("ShareTab title", () => {
  function swatches(root: unknown): unknown[] {
    return withAttr(root, "data-workspace-swatch").map(
      (swatch) => attrsOf(swatch)["data-workspace-swatch"],
    );
  }

  function trustedMarkup(root: unknown): unknown[] {
    return collectVnodes(root)
      .filter((vnode) => vnode.tag === "<")
      .map((vnode) => vnode.children);
  }

  it("titles the whole workspace's page Share and its name, beside its colour, over what an invitation covers", async () => {
    const share = await publishedPanel(
      {},
      { appServices: [], serviceLabels: { system_interface: "shell-r4nd" } },
    );

    const root = renderTab(share, { accent: "#3366ff" });

    expect(allText(byId(root, "ws-share-title"))).toBe("Share alpha");
    expect(swatches(root)).toEqual(["#3366ff"]);
    expect(trustedMarkup(root)).toEqual([]);
    expect(allText(byId(root, "ws-share-invite"))).toContain(
      "People you invite here can open every app in this workspace, and its " +
        "files, agent chats and terminal.",
    );
  });

  it("draws the ring of a workspace that has no colour", async () => {
    const share = await publishedPanel();

    expect(swatches(renderTab(share, { accent: null }))).toEqual([""]);
  });

  it("titles an app's page with the app's name and icon, and no colour dot", async () => {
    const share = await publishedPanel();
    share.selectTarget("web");

    const root = renderTab(share, { accent: "#3366ff" });

    expect(allText(byId(root, "ws-share-title"))).toBe("Share the web app");
    expect(swatches(root)).toEqual([]);
    expect(trustedMarkup(root)).toEqual([
      shareTargetIconMarkup(share.targetIcon("web"), "web", 24),
    ]);
    expect(allText(byId(root, "ws-share-invite"))).toContain(
      "People you invite here can open only the web app.",
    );
  });

  it("adds an app's own name to its title when another app is called the same", async () => {
    const share = await readyPanel(
      {},
      {
        appServices: ["hud", "hud-dev"],
        serviceDisplayNames: { hud: "Imbue HUD", "hud-dev": "Imbue HUD" },
      },
    );
    share.selectTarget("hud-dev");

    const root = renderTab(share);

    expect(allText(byId(root, "ws-share-title"))).toBe(
      "Share the Imbue HUD (hud-dev) app",
    );
    expect(allText(byId(root, "ws-share-open-elsewhere"))).toContain(
      "Open “Imbue HUD (hud-dev)” on another device",
    );
  });
});

describe("ShareTab page order", () => {
  it("draws the whole workspace's page from the off banner down to the switch", async () => {
    const share = await readyPanel();

    expect(
      pageBlocks(renderTab(share, { connections: [slackConnection()] })),
    ).toEqual([
      "ws-share-off-notice",
      "ws-share-title",
      "ws-share-open-elsewhere",
      "ws-share-invite",
      "ws-share-agents",
      "ws-share-connections",
      "ws-share-apps",
      "ws-share-publish",
    ]);
  });

  it("draws an app's page the same way, without the apps and the switch", async () => {
    const share = await readyPanel();
    share.selectTarget("web");

    expect(
      pageBlocks(renderTab(share, { connections: [slackConnection()] })),
    ).toEqual([
      "ws-share-off-notice",
      "ws-share-title",
      "ws-share-open-elsewhere",
      "ws-share-invite",
      "ws-share-agents",
      "ws-share-connections",
    ]);
  });

  it("keeps the add row and the list of who has access inside Invite others", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {},
      },
    });

    const invite = byId(renderTab(share), "ws-share-invite");

    expect(allText(invite)).toContain("Invite others");
    const ids = collectVnodes(invite).map((vnode) => attrsOf(vnode).id);
    expect(ids.indexOf("ws-share-add")).toBeGreaterThanOrEqual(0);
    expect(ids.indexOf("ws-share-access")).toBeGreaterThan(
      ids.indexOf("ws-share-add"),
    );
  });

  it("leaves scrolling to Settings: nothing in the page scrolls on its own", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {},
      },
    });
    const draw = mountedTab(share);
    openAccessList(draw);
    const root = renderDeep(draw());

    // A scroller inside the page would be the one block the rest could
    // squeeze, down to no height at all.
    expect(
      collectVnodes(root).filter((vnode) =>
        classTokensOf(vnode).some((token) => token.startsWith("overflow-y-")),
      ),
    ).toEqual([]);
    expect(classTokensOf(byId(root, "ws-share-grants") as AnyVnode)).toContain(
      "shrink-0",
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

  it("offers one person or a domain, never an agent, and reads any other kind as one person", async () => {
    const share = await publishedPanel();

    const options = collectVnodes(addControls(renderTab(share))[0]).filter(
      (vnode) => vnode.tag === "option",
    );
    // An agent is invited from a section of its own, not from this row.
    expect(options.map((option) => attrsOf(option).value)).toEqual([
      "email",
      "email_domain",
    ]);
    expect(options.map((option) => allText(option))).toEqual([
      "One person by email",
      "Everyone at a domain",
    ]);
    expect(attrsOf(addControls(renderTab(share))[0])["aria-label"]).toBe(
      "Who to invite",
    );

    chooseKind(renderTab(share), "agent");

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

    expect(allText(renderDeep(byId(renderTab(share), "ws-share-refusal")))).toBe(
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

  it("draws no list at all, and nothing in its place, when nobody has access", async () => {
    const share = await publishedPanel();

    const root = renderTabDeep(share);
    expect(grantRows(root)).toHaveLength(0);
    expect(byId(root, "ws-share-access")).toBeUndefined();
    expect(byId(root, "ws-share-grants")).toBeUndefined();
    expect(allText(byId(root, "ws-share-invite"))).not.toContain("Nobody has access");
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

  it("says nothing about a person until their row is saved", async () => {
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
    expect(withAttr(row, "data-grant-access")).toEqual([]);
  });

  it("gives every card's first line the same height and never shortens a name", async () => {
    const share = await panelWithGrants();

    const rows = grantRows(renderTab(share));

    expect(rows).toHaveLength(4);
    for (const row of rows) {
      expect(classTokensOf(row)).toContain("flex-none");
      const line = firstLineOf(row);
      expect(classTokensOf(line)).toContain("h-6");
      const name = collectVnodes(line).find(
        (vnode) => attrsOf(vnode)["data-grant-name"] !== undefined,
      );
      expect(name).toBeDefined();
      expect(classTokensOf(name as AnyVnode)).toContain("truncate");
      expect(classTokensOf(name as AnyVnode)).not.toContain(
        "whitespace-nowrap",
      );
    }
  });

  it("lets the note beside a name give way before the name does", async () => {
    const share = await panelWithGrants();
    const line = firstLineOf(rowByText(renderTab(share), "Carol Reyes"));

    const name = withAttr(line, "data-grant-name")[0];
    const note = withAttr(line, "data-grant-note")[0];

    expect(classTokensOf(note)).toEqual(
      expect.arrayContaining(["min-w-0", "truncate"]),
    );
    expect(classTokensOf(note)).not.toContain("shrink-0");
    // The name never shrinks, but is held to the cell so it can truncate.
    expect(classTokensOf(name)).toEqual(
      expect.arrayContaining(["shrink-0", "max-w-full", "truncate"]),
    );
  });

  it("keeps every card's content inside it, with no invitation status where there is none", async () => {
    const share = await panelWithGrants();

    for (const row of grantRows(renderTab(share))) {
      // The card clips its own content, so nothing (a re-invite included) can float outside it.
      expect(classTokensOf(row)).toContain("overflow-hidden");
      expect(withAttr(row, "data-slot")).toEqual([]);
    }
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
    expect(allText(row)).toContain("Couldn't add");
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

describe("ShareTab who has access", () => {
  async function grantedPanel(
    emails: string[],
    services: Record<string, { emails: string[]; email_domains: string[] }> = {},
    isPublished = true,
  ): Promise<SharePanelModel> {
    const grants = { workspace: { emails, email_domains: [] }, services };
    return isPublished ? publishedPanel({ grants }) : readyPanel({ grants });
  }

  function isAccessOpen(draw: () => m.Vnode): boolean {
    return (
      attrsOf(accessTrigger(renderDeep(draw())))["aria-expanded"] === "true"
    );
  }

  it("folds the list behind a line counting the people, and draws no row until it is pressed", async () => {
    const share = await grantedPanel(["one@example.com", "two@example.com"]);
    const draw = mountedTab(share);

    const folded = renderDeep(draw());
    expect(allText(accessTrigger(folded))).toBe("2 have access to “alpha”");
    expect(attrsOf(accessTrigger(folded))["aria-expanded"]).toBe("false");
    expect(grantRows(folded)).toHaveLength(0);

    openAccessList(draw);

    const opened = renderDeep(draw());
    expect(attrsOf(accessTrigger(opened))["aria-expanded"]).toBe("true");
    expect(
      grantRows(opened).map((row) => allText(withAttr(row, "data-grant-name"))),
    ).toEqual(["one@example.com", "two@example.com"]);

    openAccessList(draw);

    expect(grantRows(renderDeep(draw()))).toHaveLength(0);
  });

  it("counts one person in the singular", async () => {
    const share = await grantedPanel(["one@example.com"]);

    expect(allText(accessTrigger(renderTabDeep(share)))).toBe(
      "1 has access to “alpha”",
    );
  });

  it("counts, on an app's page, the people the whole workspace admits along with the app's own", async () => {
    const share = await grantedPanel(["one@example.com", "two@example.com"], {
      web: { emails: ["dev@example.com"], email_domains: [] },
    });
    share.selectTarget("web");

    expect(allText(accessTrigger(renderTabDeep(share)))).toBe(
      "3 have access to “web”",
    );
  });

  it("counts an app whose only people are inherited ones, rather than calling it empty", async () => {
    const share = await grantedPanel(["one@example.com"]);
    share.selectTarget("web");

    const root = renderTabDeep(share);

    expect(allText(accessTrigger(root))).toBe("1 has access to “web”");
    expect(byId(root, "ws-share-empty")).toBeUndefined();
  });

  it("unfolds the list when someone is added with the button, so the new row is in view", async () => {
    const share = await grantedPanel(["one@example.com"]);
    const draw = mountedTab(share);
    expect(isAccessOpen(draw)).toBe(false);

    type(draw(), "two@example.com");
    pressAdd(draw());

    expect(isAccessOpen(draw)).toBe(true);
    const root = renderDeep(draw());
    expect(allText(accessTrigger(root))).toBe("2 have access to “alpha”");
    expect(allText(rowByText(root, "two@example.com"))).toContain(
      "Securely granting access",
    );
  });

  it("unfolds the list when someone is added with Enter in the field, and for no other key", async () => {
    const share = await grantedPanel(["one@example.com"]);
    const draw = mountedTab(share);
    let preventedCount = 0;
    const press = (key: string): void =>
      (
        attrsOf(addControls(draw())[1]).onkeydown as (
          event: KeyboardEvent,
        ) => void
      )({
        key,
        preventDefault: () => {
          preventedCount += 1;
        },
      } as unknown as KeyboardEvent);

    type(draw(), "two@example.com");
    press("a");

    expect(isAccessOpen(draw)).toBe(false);
    expect(share.grantCount(WHOLE)).toBe(1);
    expect(preventedCount).toBe(0);

    press("Enter");

    expect(preventedCount).toBe(1);
    expect(share.grantCount(WHOLE)).toBe(2);
    expect(isAccessOpen(draw)).toBe(true);
    expect(rowByText(renderDeep(draw()), "two@example.com")).toBeDefined();
  });

  it("opens onto the first person added to an empty list", async () => {
    const share = await grantedPanel([]);
    const draw = mountedTab(share);
    expect(byId(renderDeep(draw()), "ws-share-access")).toBeUndefined();

    type(draw(), "first@example.com");
    pressAdd(draw());

    const root = renderDeep(draw());
    expect(byId(root, "ws-share-empty")).toBeUndefined();
    expect(allText(accessTrigger(root))).toBe("1 has access to “alpha”");
    expect(rowByText(root, "first@example.com")).toBeDefined();
  });

  it("leaves every other target's list folded after an add", async () => {
    const share = await grantedPanel(["one@example.com"], {
      web: { emails: ["dev@example.com"], email_domains: [] },
    });
    const draw = mountedTab(share);

    type(draw(), "two@example.com");
    pressAdd(draw());
    share.selectTarget("web");

    expect(isAccessOpen(draw)).toBe(false);
    expect(grantRows(renderDeep(draw()))).toHaveLength(0);

    // The list the add was made on is still open on the way back.
    share.selectTarget(WHOLE);

    expect(isAccessOpen(draw)).toBe(true);
  });

  it("stays folded when the add cannot be made, sharing being off", async () => {
    const share = await grantedPanel(["one@example.com"], {}, false);
    const draw = mountedTab(share);

    type(draw(), "two@example.com");
    pressAdd(draw());

    expect(share.grantCount(WHOLE)).toBe(1);
    expect(isAccessOpen(draw)).toBe(false);
  });
});

describe("ShareTab connections reminder", () => {
  const SLACK = slackConnection();
  const NOTION = slackConnection({
    service_name: "notion",
    display_name: "Notion",
    account: "n@x.com",
    account_label: "n@x.com",
    show_account_label: true,
  });
  const WORKSPACE_HEADING = "Invited users will have access to your connections";
  const APP_HEADING =
    "Invited users will have access to any of these connections that are used in the app:";

  function tiles(section: unknown): AnyVnode[] {
    return withAttr(section, "data-share-connection");
  }

  it("lists every connection on the whole workspace's page, telling two accounts of a service apart", async () => {
    const share = await publishedPanel();

    const section = byId(
      renderTab(share, { connections: [SLACK, NOTION] }),
      "ws-share-connections",
    );

    expect(allText(section)).toContain(WORKSPACE_HEADING);
    expect(allText(section)).not.toContain(APP_HEADING);
    expect(
      tiles(section).map((tile) => attrsOf(tile)["data-share-connection"]),
    ).toEqual(["conn:slack:", "conn:notion:n@x.com"]);
    expect(allText(tiles(section)[0])).toBe("Slack");
    expect(allText(tiles(section)[1])).toBe("Notion n@x.com");
  });

  it("lists the same connections on an app's page, as the ones the app may use", async () => {
    const share = await publishedPanel();
    share.selectTarget("web");

    const section = byId(
      renderTab(share, { connections: [SLACK, NOTION] }),
      "ws-share-connections",
    );

    expect(allText(section)).toContain(APP_HEADING);
    expect(allText(section)).not.toContain(WORKSPACE_HEADING);
    expect(tiles(section)).toHaveLength(2);
  });

  it("opens a connection's own page, by its section, when its tile is pressed", async () => {
    const share = await publishedPanel();
    const opened: string[] = [];

    const section = byId(
      renderTab(share, {
        connections: [SLACK, NOTION],
        onOpenConnection: (sectionId) => opened.push(sectionId),
      }),
      "ws-share-connections",
    );
    (attrsOf(tiles(section)[1]).onclick as () => void)();

    expect(opened).toEqual(["conn:notion:n@x.com"]);
  });

  it.each([
    { state: "are not known yet", connections: null },
    { state: "are none", connections: [] },
  ])("is left out, without holding the page, while the connections $state", async ({ connections }) => {
    const share = await publishedPanel();

    for (const target of [WHOLE, "web"]) {
      share.selectTarget(target);
      const root = renderTab(share, { connections });

      expect(byId(root, "ws-share-connections")).toBeUndefined();
      expect(allText(root)).not.toContain(WORKSPACE_HEADING);
      expect(allText(root)).not.toContain(APP_HEADING);
      expect(byId(root, "ws-share-title")).toBeDefined();
      expect(byId(root, "ws-share-agents")).toBeDefined();
    }
  });
});

describe("ShareTab apps grid", () => {
  function tiles(root: unknown): AnyVnode[] {
    return withAttr(byId(root, "ws-share-apps"), "data-share-app");
  }

  it("lists, on the whole workspace's page, the apps an invitation opens, system apps included", async () => {
    const share = await readyPanel(
      { enabled: true, url: "https://m.relay.example/" },
      { systemServices: ["chat"], serviceDisplayNames: { web: "Web App" } },
    );

    const root = renderTab(share);

    expect(allText(byId(root, "ws-share-apps"))).toContain(
      "Invited users will have access to these apps",
    );
    expect(tiles(root).map((tile) => attrsOf(tile)["data-share-app"])).toEqual([
      "web",
      "docs",
      "chat",
    ]);
    expect(tiles(root).map((tile) => allText(tile))).toEqual([
      "Web App",
      "docs",
      "chat",
    ]);
  });

  it("opens an app's own page when its tile is pressed, which has no grid of its own", async () => {
    const share = await publishedPanel();

    (attrsOf(tiles(renderTab(share))[1]).onclick as () => void)();

    expect(share.currentTarget).toBe("docs");
    const root = renderTab(share);
    expect(allText(byId(root, "ws-share-title"))).toBe("Share the docs app");
    expect(byId(root, "ws-share-apps")).toBeUndefined();
  });

  it("is left out of a workspace with no apps to open", async () => {
    const share = await publishedPanel(
      {},
      { appServices: [], serviceLabels: { system_interface: "shell-r4nd" } },
    );

    const root = renderTab(share);

    expect(byId(root, "ws-share-apps")).toBeUndefined();
    expect(allText(root)).not.toContain(
      "Invited users will have access to these apps",
    );
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

  function pressTurnOn(root: unknown): void {
    (
      attrsOf(byId(root, "ws-share-turn-on") as AnyVnode).onclick as () => void
    )();
  }

  it("asks the same question from the off banner's Turn on sharing, and publishes once it is answered", async () => {
    const { share, methods } = await recordingPanel();
    const draw = mountedTab(share);

    pressTurnOn(draw());

    const asked = draw();
    expect(isDialogOpen(asked)).toBe(true);
    expect(allText(asked)).toContain("Enable sharing?");
    expect(methods).toEqual(["GET"]);

    (
      attrsOf(byId(asked, "ws-share-publish-confirm") as AnyVnode)
        .onclick as () => void
    )();
    await settle();

    expect(isDialogOpen(draw())).toBe(false);
    expect(share.isPublished).toBe(true);
    expect(methods).toContain("PUT");
    expect(byId(draw(), "ws-share-off-notice")).toBeUndefined();
  });

  it("asks it on an app's page too, where there is no switch to press", async () => {
    const { share, methods } = await recordingPanel();
    share.selectTarget("web");
    const draw = mountedTab(share);
    expect(byId(draw(), "ws-share-publish-switch")).toBeUndefined();

    pressTurnOn(draw());
    expect(isDialogOpen(draw())).toBe(true);
    (
      attrsOf(byId(draw(), "ws-share-publish-cancel") as AnyVnode)
        .onclick as () => void
    )();

    expect(isDialogOpen(draw())).toBe(false);
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

  it("says sharing is off, above the title, with the way to turn it on", async () => {
    const share = await offPanel();

    const root = renderTab(share);
    const banner = byId(root, "ws-share-off-notice");

    expect(allText(banner)).toBe(
      "Sharing is off for this workspace. Turn on sharing…",
    );
    expect(allText(byId(banner, "ws-share-turn-on"))).toBe("Turn on sharing…");
    expect(pageBlocks(root).slice(0, 2)).toEqual([
      "ws-share-off-notice",
      "ws-share-title",
    ]);
  });

  it("says the same on an app's page, even one whose own list is empty", async () => {
    const share = await readyPanel({
      grants: {
        workspace: { emails: ["friend@example.com"], email_domains: [] },
        services: {},
      },
    });
    share.selectTarget("web");

    expect(share.grantCount("web")).toBe(0);
    expect(allText(byId(renderTab(share), "ws-share-off-notice"))).toBe(
      "Sharing is off for this workspace. Turn on sharing…",
    );
  });

  it("keeps the sections under the banner, with what cannot be used disabled and saying why", async () => {
    const share = await offPanel();

    const root = renderTab(share);

    expect(pageBlocks(root)).toEqual([
      "ws-share-off-notice",
      "ws-share-title",
      "ws-share-open-elsewhere",
      "ws-share-invite",
      "ws-share-agents",
      "ws-share-apps",
      "ws-share-publish",
    ]);
    for (const control of addControls(root)) {
      expect(attrsOf(control)["aria-disabled"]).toBe("true");
      expect(attrsOf(control)["data-tooltip"]).toBe(
        "Permissions cannot be granted while sharing is off",
      );
    }
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
    const draw = mountedTab(share);
    // Folded with the list it belongs to, the group is not drawn at all.
    expect(byId(renderDeep(draw()), "ws-share-inherited")).toBeUndefined();

    openAccessList(draw);
    const group = byId(renderDeep(draw()), "ws-share-inherited");

    expect(allText(group)).toContain(
      "3 permissions inherited from the whole workspace",
    );
    // Shut, the rows are not drawn at all, so nothing reaches them -- not the
    // keyboard, not a screen reader.
    expect(allText(group)).not.toContain("one@example.com");
  });

  it("counts a lone inherited permission in the singular", async () => {
    const share = await appPanel({ emails: ["one@example.com"] });
    const draw = mountedTab(share);

    openAccessList(draw);

    expect(allText(byId(renderDeep(draw()), "ws-share-inherited"))).toContain(
      "1 permission inherited from the whole workspace",
    );
  });

  it("shows the inherited grants under the header once it is opened", async () => {
    const share = await appPanel({
      emails: ["one@example.com", "two@example.com"],
      email_domains: ["acme.example"],
    });

    const draw = mountedTab(share);
    openAccessList(draw);

    const trigger = byId(
      renderDeep(draw()),
      "ws-share-inherited-trigger",
    ) as AnyVnode;
    expect(attrsOf(trigger)["aria-expanded"]).toBe("false");
    (attrsOf(trigger).onclick as () => void)();

    const opened = byId(renderDeep(draw()), "ws-share-inherited");
    expect(
      attrsOf(
        byId(renderDeep(draw()), "ws-share-inherited-trigger") as AnyVnode,
      )["aria-expanded"],
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
    openAccessList(draw);
    const trigger = byId(
      renderDeep(draw()),
      "ws-share-inherited-trigger",
    ) as AnyVnode;
    (attrsOf(trigger).onclick as () => void)();

    const root = renderDeep(draw());
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
    openAccessList(draw);

    (
      attrsOf(
        byId(renderDeep(draw()), "ws-share-inherited-trigger") as AnyVnode,
      ).onclick as () => void
    )();

    // A rail marks them as the whole workspace's rather than this app's, and
    // the indent beside it still lands a row's contents under the summary.
    const panel = byId(
      renderDeep(draw()),
      "ws-share-inherited-panel",
    ) as AnyVnode;
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
    openAccessList(draw);

    (
      attrsOf(
        byId(renderDeep(draw()), "ws-share-inherited-trigger") as AnyVnode,
      ).onclick as () => void
    )();

    const panel = byId(
      renderDeep(draw()),
      "ws-share-inherited-panel",
    ) as AnyVnode;
    const removes = collectVnodes(panel).filter((vnode) =>
      String(attrsOf(vnode)["aria-label"] ?? "").startsWith("Remove"),
    );
    // The grant is the whole workspace's, so a control here would either do
    // nothing or revoke it somewhere the reader is not looking.
    expect(removes).toEqual([]);
  });

  it("keeps the header above the rows it names while they are in view", async () => {
    const share = await appPanel({ emails: ["one@example.com"] });
    const draw = mountedTab(share);
    openAccessList(draw);

    const trigger = byId(
      renderDeep(draw()),
      "ws-share-inherited-trigger",
    ) as AnyVnode;
    // Sticky against the group rather than the scroller, so the header leaves
    // with the rows it labels instead of outliving them.
    expect(classTokensOf(trigger)).toContain("sticky");
    expect(classTokensOf(trigger)).toContain("top-0");
  });

  it("says nothing about inherited grants on the whole workspace itself", async () => {
    const share = await publishedPanel({
      grants: {
        workspace: { emails: ["one@example.com"], email_domains: [] },
        services: {},
      },
    });
    const draw = mountedTab(share);

    openAccessList(draw);
    const root = renderDeep(draw());

    expect(grantRows(root)).toHaveLength(1);
    expect(byId(root, "ws-share-inherited")).toBeUndefined();
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
      "Connecting this workspace",
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

    expect(allText(failed)).toContain("Couldn't add");
    expect(allText(failed)).toContain("Retry");
    expect(allText(saving)).toContain("Securely granting access");
    expect(allText(saving)).not.toContain("Couldn't add");
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
  it("refuses at the add row, over the list that says what is already inherited", async () => {
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
    const draw = mountedTab(share);

    chooseKind(draw(), "email_domain");
    type(draw(), "gmail.com");
    pressAdd(draw());

    // The press unfolded the list, so what the app already admits is in view
    // under the refusal.
    const root = renderDeep(draw());
    expect(allText(byId(root, "ws-share-inherited"))).toContain(
      "2 permissions inherited from the whole workspace",
    );
    expect(allText(byId(root, "ws-share-refusal"))).toBe(
      "gmail.com cannot be granted permissions because it is a public email provider.",
    );
    expect(share.grantCount("web")).toBe(0);
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

  it("says nobody hears about a domain grant, on the card that made it", async () => {
    const share = await everyKindPanel();

    const row = rowByText(renderTab(share), "Anyone at acme.example");

    expect(allText(detailsOf(row))).toBe(
      "People at acme.example will not hear about this unless you send " +
        "them the link yourself.",
    );
  });

  it("says whether each person has opened the workspace, or that they have not signed up yet", async () => {
    const share = await everyKindPanel();
    const root = renderTab(share);

    expect(allText(accessNoteOf(rowByText(root, "newcomer@example.com")))).toBe(
      "Hasn't signed up for Imbue Studio yet",
    );
    expect(allText(accessNoteOf(rowByText(root, "Carol Reyes")))).toBe(
      "Never accessed your workspace",
    );
    expect(
      withAttr(rowByText(root, "Anyone at acme.example"), "data-grant-access"),
    ).toEqual([]);
    expect(allText(root)).not.toContain("(hasn't signed up yet)");
  });
});

describe("ShareTab provisioning trouble", () => {
  /** A published workspace whose link is not live yet. */
  async function stuckPanel(): Promise<SharePanelModel> {
    const share = await publishedPanel();
    share.isLive = false;
    return share;
  }

  it("says a halted bring-up couldn't finish, with a Try again that turns sharing off and on and a bug report", async () => {
    const share = await stuckPanel();
    share.gatewayState = "halted";
    share.gatewayError = "the certificate order was refused.";
    const republish = vi.spyOn(share, "republish").mockResolvedValue();

    const trouble = byId(
      renderTab(share),
      "ws-share-gateway-trouble",
    ) as AnyVnode;

    expect(attrsOf(trouble).error).toEqual({
      message:
        "This workspace couldn't finish setting up its link. Try again. If this persists, report a bug.",
      details: "the certificate order was refused.",
    });
    expect(attrsOf(trouble).isBugReportOffered).toBe(true);
    const [tryAgain] = attrsOf(trouble).actions as {
      label: string;
      onclick: () => void;
    }[];
    expect(tryAgain.label).toBe("Try again");
    tryAgain.onclick();
    expect(republish).toHaveBeenCalledOnce();
  });

  it("says when a failed attempt will be tried again", async () => {
    const share = await stuckPanel();
    share.gatewayState = "retrying";
    share.gatewayFailedAttemptCount = 2;
    share.gatewayError = "the relay refused the tunnel";

    const trouble = byId(
      renderTab(share),
      "ws-share-gateway-trouble",
    ) as AnyVnode;

    expect(attrsOf(trouble).error).toEqual({
      message:
        "Setting up the link hit a problem. The workspace tries again shortly.",
      details: "the relay refused the tunnel",
    });
    // It clears on its own: nothing to report.
    expect(attrsOf(trouble).isBugReportOffered).toBe(false);
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

describe("ShareTab invitations", () => {
  const INVITED_AT = "2026-09-30T12:00:00+00:00";
  const RESEND_INVITE_LABEL = "Resend invite";

  interface InvitationFixture {
    outcomes?: unknown[];
    isSynced?: boolean;
    respondToInvite?: () => Promise<RequestResult>;
  }

  /** The send a refused row offers beside its sentence, if any. */
  function resendControl(row: AnyVnode): AnyVnode | undefined {
    return collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["data-resend-verification"] !== undefined,
    );
  }

  /** Invite the friend on `share`, and return their grant. */
  async function inviteFriend(share: SharePanelModel): Promise<Grant> {
    const friend = share
      .grantsFor(WHOLE)
      .find((grant) => grant.grantee.value === "friend@example.com") as Grant;
    await share.invite(WHOLE, friend.key);
    await settle();
    return friend;
  }

  /** A panel that has just invited the friend, answered by `answer`. */
  async function invitedFriend(
    answer: ResponseBody,
    answeredAs: { ok: boolean; status: number } = { ok: false, status: 409 },
  ): Promise<{ share: SharePanelModel; friend: Grant }> {
    const { share } = await invitationPanel({
      respondToInvite: () => Promise.resolve({ ...answeredAs, body: answer }),
    });
    return { share, friend: await inviteFriend(share) };
  }

  /** A published, synced panel over one address, one account and one domain,
   * with what the outcomes route says about them. */
  async function invitationPanel(
    fixture: InvitationFixture = {},
  ): Promise<{ share: SharePanelModel; inviteCount: () => number }> {
    let inviteCount = 0;
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: (url: string, init?: RequestInit) => {
          if (url.endsWith("/invitation-outcomes"))
            return Promise.resolve({
              ok: true,
              status: 200,
              body: { outcomes: fixture.outcomes ?? [] },
            });
          if (url.endsWith("/invitations")) {
            inviteCount += 1;
            return (
              fixture.respondToInvite ??
              (() =>
                Promise.resolve({
                  ok: true,
                  status: 200,
                  body: { outcome: "invited", invited_at: INVITED_AT },
                }))
            )();
          }
          expect(init?.method ?? "GET").toBe("GET");
          return Promise.resolve({
            ok: true,
            status: 200,
            body: {
              enabled: true,
              url: "https://m.relay.example/",
              grants: {
                workspace: {
                  users: ["user-2"],
                  emails: ["friend@example.com"],
                  email_domains: ["acme.example"],
                },
                services: {},
              },
              identities: {
                "user-2": {
                  user_id: "user-2",
                  email: "carol@example.com",
                  display_name: "Carol Reyes",
                  profile_picture_url: null,
                },
              },
              grants_synced: fixture.isSynced ?? true,
            },
          });
        },
      }),
    );
    await share.load();
    await settle();
    return { share, inviteCount: () => inviteCount };
  }

  function slotOf(row: AnyVnode, name: string): AnyVnode {
    const slot = collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["data-slot"] === name,
    );
    expect(slot).toBeDefined();
    return slot as AnyVnode;
  }

  function inviteControl(row: AnyVnode): AnyVnode | undefined {
    return collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["data-invite"] !== undefined,
    );
  }

  function refusalOf(row: AnyVnode): AnyVnode | undefined {
    return collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["data-invite-refusal"] !== undefined,
    );
  }

  it("fills the one status indicator with what the granter may learn, and nothing about a domain", async () => {
    const { share } = await invitationPanel({
      outcomes: [
        {
          kind: "email",
          value: "friend@example.com",
          app: WHOLE,
          outcome: "invited",
          invited_at: INVITED_AT,
        },
        {
          kind: "user",
          value: "user-2",
          app: WHOLE,
          outcome: "joined",
          joined_at: "2026-09-28T00:00:00+00:00",
          last_visited_at: "2026-09-29T00:00:00+00:00",
        },
      ],
    });

    const root = renderTab(share);

    expect(
      allText(slotOf(rowByText(root, "friend@example.com"), "invitation")),
    ).toMatch(/^Invited .* ago$|^Invited just now$/);
    const carol = rowByText(root, "Carol Reyes");
    // The access note already says Carol is in, so her card does not say it twice.
    expect(allText(accessNoteOf(carol))).toMatch(/^Accessed your workspace /);
    expect(allText(carol)).not.toContain("Joined");
    expect(withAttr(carol, "data-slot")).toEqual([]);
    // A joined person is in: no re-invite is offered.
    expect(inviteControl(carol)).toBeUndefined();
    expect(
      withAttr(rowByText(root, "Anyone at acme.example"), "data-slot"),
    ).toEqual([]);
  });

  it("puts what is known about a person on the line under their name, with the re-invite", async () => {
    const { share } = await invitationPanel({
      outcomes: [
        {
          kind: "email",
          value: "friend@example.com",
          app: WHOLE,
          outcome: "invited",
          invited_at: INVITED_AT,
        },
      ],
    });

    const row = rowByText(renderTab(share), "friend@example.com");
    const details = detailsOf(row) as AnyVnode;

    expect(allText(details)).toMatch(
      /^Hasn't signed up for Imbue Studio yet · Invited .* · Resend invite$/,
    );
    expect(inviteControl(firstLineOf(row))).toBeUndefined();
    expect(allText(inviteControl(details))).toBe(RESEND_INVITE_LABEL);
  });

  it("says could not invite, and no more than that, plus a re-invite", async () => {
    const { share } = await invitationPanel({
      outcomes: [
        {
          kind: "email",
          value: "friend@example.com",
          app: WHOLE,
          outcome: "could_not_invite",
        },
      ],
    });

    const row = rowByText(renderTab(share), "friend@example.com");
    expect(allText(slotOf(row, "invitation"))).toContain("Could not invite");
    expect(allText(inviteControl(row))).toBe(RESEND_INVITE_LABEL);
  });

  it("offers a re-invite once there is an outcome, and nothing for a domain, an untried grant, or someone who joined", async () => {
    const { share, inviteCount } = await invitationPanel({
      outcomes: [
        {
          kind: "email",
          value: "friend@example.com",
          app: WHOLE,
          outcome: "invited",
          invited_at: INVITED_AT,
        },
      ],
    });
    const root = renderTab(share);

    const friend = inviteControl(rowByText(root, "friend@example.com"));
    expect(friend).toBeDefined();
    expect(allText(friend)).toBe(RESEND_INVITE_LABEL);
    expect(attrsOf(friend as AnyVnode)["aria-label"]).toBe(RESEND_INVITE_LABEL);
    // The first invitation is automatic, so a person with no outcome shows no button.
    expect(inviteControl(rowByText(root, "Carol Reyes"))).toBeUndefined();
    expect(
      inviteControl(rowByText(root, "Anyone at acme.example")),
    ).toBeUndefined();

    (attrsOf(friend as AnyVnode).onclick as () => void)();
    await settle();

    expect(inviteCount()).toBe(1);
  });

  it("offers no re-invite, and says why, while the document has not reached Imbue Cloud", async () => {
    const { share } = await invitationPanel({
      isSynced: false,
      outcomes: [
        {
          kind: "email",
          value: "friend@example.com",
          app: WHOLE,
          outcome: "invited",
          invited_at: INVITED_AT,
        },
      ],
    });

    const root = renderTab(share);

    expect(
      grantRows(root).some((row) => inviteControl(row) !== undefined),
    ).toBe(false);
    expect(allText(byId(root, "ws-share-sync-notice"))).toContain(
      "have not reached Imbue Cloud",
    );
  });

  it("has no sync notice once the document has reached Imbue Cloud", async () => {
    const { share } = await invitationPanel();

    expect(byId(renderTab(share), "ws-share-sync-notice")).toBeUndefined();
  });

  it("shows the row inviting with no button, then the refusal in its words and a re-invite", async () => {
    let answer: (result: RequestResult) => void = () => undefined;
    const { share } = await invitationPanel({
      respondToInvite: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const friend = share
      .grantsFor(WHOLE)
      .find((grant) => grant.grantee.value === "friend@example.com");
    expect(friend).toBeDefined();

    const inviting = share.invite(WHOLE, (friend as Grant).key);
    const pending = slotOf(
      rowByText(renderTab(share), "friend@example.com"),
      "invitation",
    );
    expect(allText(pending)).toContain("Inviting");
    expect(
      inviteControl(rowByText(renderTab(share), "friend@example.com")),
    ).toBeUndefined();

    answer({ ok: true, status: 200, body: { outcome: "too_soon" } });
    await inviting;
    await settle();

    const row = rowByText(renderTab(share), "friend@example.com");
    expect(allText(refusalOf(row))).toBe(
      "You invited this person too recently.",
    );
    expect(allText(inviteControl(row))).toBe(RESEND_INVITE_LABEL);
  });

  it("offers the verification email beside a refusal for an unverified granter, and says how it went", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ sent: true }));
    vi.stubGlobal("fetch", fetchMock);
    const { share } = await invitedFriend(UNVERIFIED_REFUSAL);

    const row = rowByText(renderTab(share), "friend@example.com");
    expect(allText(refusalOf(row))).toContain(
      "Verify your email to invite people",
    );
    const resend = resendControl(row) as AnyVnode;
    expect(allText(resend)).toBe("Send the email again");
    expect(errorMarks(refusalOf(row))).toHaveLength(1);

    (attrsOf(resend).onclick as () => void)();
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const rowAfterSend = rowByText(renderTab(share), "friend@example.com");
    expect(allText(refusalOf(rowAfterSend))).toContain(
      "Sent another email to owner@example.com.",
    );
    expect(errorMarks(refusalOf(rowAfterSend))).toHaveLength(0);

    fetchMock.mockResolvedValueOnce(new Response("", { status: 502 }));
    (attrsOf(resendControl(rowAfterSend) as AnyVnode).onclick as () => void)();
    await settle();

    const refusalAfterFailure = refusalOf(
      rowByText(renderTab(share), "friend@example.com"),
    ) as AnyVnode;
    expect(allText(refusalAfterFailure)).toContain(
      "Could not send the email to owner@example.com.",
    );
    expect(errorMarks(refusalAfterFailure)).toHaveLength(1);
  });

  it("stops offering the verification email once a resend finds the address already verified", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ sent: false, already_verified: true })),
    );
    const { share } = await invitedFriend(UNVERIFIED_REFUSAL);

    const row = rowByText(renderTab(share), "friend@example.com");
    (attrsOf(resendControl(row) as AnyVnode).onclick as () => void)();
    await settle();

    const rowAfterSend = rowByText(renderTab(share), "friend@example.com");
    expect(allText(refusalOf(rowAfterSend))).toContain(
      "owner@example.com is already verified.",
    );
    expect(resendControl(rowAfterSend)).toBeUndefined();
  });

  it("offers the verification email for the granter when a refusal names no address", async () => {
    const { share, friend } = await invitedFriend({
      ...UNVERIFIED_REFUSAL,
      email: null,
    });

    const indicator = share.invitationStatusIndicator(WHOLE, friend);
    expect(indicator).toMatchObject({ unverifiedEmail: share.granterEmail });
  });

  it("offers no verification email beside any other refusal", async () => {
    const { share } = await invitedFriend(
      { outcome: "too_soon" },
      { ok: true, status: 200 },
    );

    const row = rowByText(renderTab(share), "friend@example.com");
    expect(resendControl(row)).toBeUndefined();
  });

  it("leaves a refusal the panel has since cleared alone when a resend lands late", async () => {
    const send = heldFetch();
    vi.stubGlobal("fetch", send.fetchImpl);
    const { share, friend } = await invitedFriend(UNVERIFIED_REFUSAL);

    const sending = share.resendVerification(friend.key, "owner@example.com");
    await share.load();
    send.answer(jsonResponse({ sent: true }));
    await sending;

    expect(share.inviteStateFor(friend.key)).toEqual({ state: "idle" });
  });

  it("leaves a newer refusal alone when a resend lands late", async () => {
    const send = heldFetch();
    vi.stubGlobal("fetch", send.fetchImpl);
    let answerCount = 0;
    const { share } = await invitationPanel({
      respondToInvite: () => {
        answerCount += 1;
        return Promise.resolve(
          answerCount === 1
            ? { ok: false, status: 409, body: UNVERIFIED_REFUSAL }
            : { ok: true, status: 200, body: { outcome: "too_soon" } },
        );
      },
    });
    const friend = await inviteFriend(share);

    const sending = share.resendVerification(friend.key, "owner@example.com");
    await inviteFriend(share);
    send.answer(jsonResponse({ sent: true }));
    await sending;

    const row = rowByText(renderTab(share), "friend@example.com");
    expect(allText(refusalOf(row))).toContain(
      "You invited this person too recently.",
    );
    expect(resendControl(row)).toBeUndefined();
  });

  it("says a refusal whole on a line of its own, under the name and what is known about the person", async () => {
    const { share } = await invitationPanel({
      respondToInvite: () =>
        Promise.resolve({
          ok: true,
          status: 200,
          body: { outcome: "over_allowance" },
        }),
    });
    expect(
      refusalOf(rowByText(renderTab(share), "friend@example.com")),
    ).toBeUndefined();
    const friend = share
      .grantsFor(WHOLE)
      .find((grant) => grant.grantee.value === "friend@example.com");

    await share.invite(WHOLE, (friend as Grant).key);
    await settle();

    const row = rowByText(renderTab(share), "friend@example.com");
    const refusal = refusalOf(row) as AnyVnode;
    expect(allText(refusal)).toBe(
      "Access granted, but you've reached your invitation email limit for now.",
    );
    // Wrapped, never cut short.
    expect(classTokensOf(refusal)).not.toContain("truncate");
    const line = firstLineOf(row);
    const details = detailsOf(row) as AnyVnode;
    expect(collectVnodes(line)).not.toContain(refusal);
    expect(collectVnodes(details)).not.toContain(refusal);
    expect(withAttr(line, "data-grant-name")).toHaveLength(1);
    expect(allText(inviteControl(details))).toBe(RESEND_INVITE_LABEL);
  });

  it("marks the refusal as the error it is, the way the tab's other refusals are marked", async () => {
    const { share } = await invitationPanel({
      respondToInvite: () =>
        Promise.resolve({
          ok: true,
          status: 200,
          body: { outcome: "too_soon" },
        }),
    });
    const friend = share
      .grantsFor(WHOLE)
      .find((grant) => grant.grantee.value === "friend@example.com");

    await share.invite(WHOLE, (friend as Grant).key);
    await settle();

    const refusal = refusalOf(
      rowByText(renderTab(share), "friend@example.com"),
    );
    expect(allText(refusal)).toContain("You invited this person too recently.");
    expect(errorMarks(renderDeep(refusal))).toHaveLength(1);
  });

  it("says when someone last opened the workspace, with that time on hover", async () => {
    const lastVisit = new Date(Date.now() - 3 * 3600_000).toISOString();
    const { share } = await invitationPanel({
      outcomes: [
        {
          kind: "user",
          value: "user-2",
          app: WHOLE,
          outcome: "joined",
          joined_at: "2026-09-28T00:00:00+00:00",
          last_visited_at: lastVisit,
        },
      ],
    });

    const note = accessNoteOf(
      rowByText(renderTab(share), "Carol Reyes"),
    ) as AnyVnode;

    expect(allText(note)).toBe("Accessed your workspace 3 hours ago");
    expect(attrsOf(note)["data-tooltip"]).toBe(
      `Accessed ${new Date(lastVisit).toLocaleString()}`,
    );
  });
});

describe("ShareTab visiting agents", () => {
  const AGENT: VisitingAgentEntry = {
    agent_id: "va-" + "1".repeat(32),
    name: "Josh Claude",
    fingerprint: "SHA256:abc",
    scope: "workspace",
    granter: "user:owner",
    created_at: "2026-10-15T18:00:00+00:00",
    expires_at: null,
    last_seen_at: null,
  };

  /** A published panel whose agent routes answer `agents` and mint `invite`. */
  async function agentsPanel(
    agents: VisitingAgentEntry[],
    invite: Record<string, unknown> = {
      code: "7K3M-QX9A-2PFH",
      expires_at: "2026-10-15T18:15:00+00:00",
      scope: "workspace",
      auth_origin: "https://auth-x.alpha.example.com",
      instructions: "Join with 7K3M-QX9A-2PFH",
    },
  ): Promise<SharePanelModel> {
    return publishedPanel(
      {},
      {
        fetchJson: (url: string) => {
          if (url.endsWith("/agents/invites"))
            return Promise.resolve({ ok: true, status: 200, body: invite });
          if (url.endsWith("/agents"))
            return Promise.resolve({ ok: true, status: 200, body: { agents } });
          if (url.endsWith("/invitation-outcomes"))
            return Promise.resolve({
              ok: true,
              status: 200,
              body: { outcomes: [] },
            });
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
  }

  it("offers Create invite under its own heading and sentence, and lists the agents once read", async () => {
    const share = await agentsPanel([AGENT]);
    await share.ensureVisitingAgentsLoaded();
    const root = renderTab(share);
    const section = byId(root, "ws-share-agents");
    expect(section).toBeDefined();
    const text = allText(section);
    expect(text).toContain("Invite an agent");
    expect(text).toContain("exactly this workspace's access");
    expect(allText(byId(section, "ws-share-agents-invite"))).toBe(
      "Create invite",
    );
    expect(text).toContain("Josh Claude");
    expect(text).toContain("whole workspace");
    expect(text).toContain("invited by a person, never connected");
    expect(text).toContain("Revoke");
    // A section of its own, after the people: not a kind in the invite row.
    expect(byId(byId(root, "ws-share-invite"), "ws-share-agents")).toBeUndefined();
  });

  it("mints an invite for the page's target when Create invite is pressed", async () => {
    const share = await agentsPanel([]);
    await share.ensureVisitingAgentsLoaded();
    const invite = vi.spyOn(share, "inviteVisitingAgent").mockResolvedValue();

    (
      attrsOf(byId(renderTab(share), "ws-share-agents-invite") as AnyVnode)
        .onclick as () => void
    )();
    share.selectTarget("web");
    (
      attrsOf(byId(renderTab(share), "ws-share-agents-invite") as AnyVnode)
        .onclick as () => void
    )();

    expect(invite.mock.calls).toEqual([[WHOLE], ["web"]]);
  });

  it("revokes the agent its Revoke is pressed on", async () => {
    const share = await agentsPanel([AGENT]);
    await share.ensureVisitingAgentsLoaded();
    const revoke = vi.spyOn(share, "revokeVisitingAgent").mockResolvedValue();

    const row = withAttr(renderTab(share), "data-agent-id")[0];
    const button = collectVnodes(row).find(
      (vnode) => attrsOf(vnode)["aria-label"] === "Revoke Josh Claude",
    );
    (attrsOf(button as AnyVnode).onclick as () => void)();

    expect(revoke).toHaveBeenCalledWith(AGENT.agent_id);
  });

  it("leaves the agents out of the count of who has access, which is people only", async () => {
    const share = await agentsPanel([AGENT]);
    await share.ensureVisitingAgentsLoaded();

    const root = renderTabDeep(share);

    expect(allText(byId(root, "ws-share-agents"))).toContain("Josh Claude");
    expect(byId(root, "ws-share-access")).toBeUndefined();
    expect(byId(root, "ws-share-grants")).toBeUndefined();
  });

  it("offers to read again when the read failed", async () => {
    let isBroken = true;
    const share = await publishedPanel(
      {},
      {
        fetchJson: (url: string) => {
          if (url.endsWith("/agents"))
            return Promise.resolve(
              isBroken
                ? { ok: false, status: 502, body: { error: "it is stopped" } }
                : { ok: true, status: 200, body: { agents: [AGENT] } },
            );
          if (url.endsWith("/invitation-outcomes"))
            return Promise.resolve({
              ok: true,
              status: 200,
              body: { outcomes: [] },
            });
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
    await share.ensureVisitingAgentsLoaded();
    const failed = attrsOf(byId(renderTab(share), "ws-share-agents-error") as AnyVnode);
    expect(failed.error).toEqual({
      message:
        "Couldn't load the agents invited to this workspace. Try again. If this persists, report a bug.",
      details: expect.stringContaining("it is stopped"),
    });
    expect(failed.isBugReportOffered).toBe(true);
    const [retry] = failed.actions as { label: string; onclick: () => void }[];
    expect(retry.label).toBe("Try again");

    isBroken = false;
    retry.onclick();
    await settle();

    const root = renderTab(share);
    expect(byId(root, "ws-share-agents-error")).toBeUndefined();
    expect(allText(byId(root, "ws-share-agents"))).toContain("Josh Claude");
  });

  it("offers only a start when the read failed on a stopped workspace", async () => {
    const share = await publishedPanel(
      {},
      {
        workspaceLiveness: () => "STOPPED",
        fetchJson: (url: string) =>
          Promise.resolve(
            url.endsWith("/agents")
              ? { ok: false, status: 502, body: { error: "it is stopped" } }
              : {
                  ok: true,
                  status: 200,
                  body: { enabled: true, url: "https://m.relay.example/" },
                },
          ),
      },
    );
    await share.ensureVisitingAgentsLoaded();

    expect(
      actionsOf(renderTab(share), "ws-share-agents-error").map(
        (action) => action.label,
      ),
    ).toEqual(["Start workspace"]);
  });

  it("says why an invite was refused, with the button that fixes it", async () => {
    const share = await publishedPanel(
      {},
      {
        fetchJson: (url: string) => {
          if (url.endsWith("/agents/invites"))
            return Promise.resolve({
              ok: false,
              status: 409,
              body: {
                error: "This workspace predates visiting agents; update it",
                reason: "AGENTS_UNSUPPORTED",
              },
            });
          if (url.endsWith("/agents"))
            return Promise.resolve({ ok: true, status: 200, body: { agents: [] } });
          if (url.endsWith("/invitation-outcomes"))
            return Promise.resolve({ ok: true, status: 200, body: { outcomes: [] } });
          return Promise.resolve({
            ok: true,
            status: 200,
            body: {
              enabled: true,
              url: "https://m.relay.example/",
              grants: { workspace: { emails: [], email_domains: [] }, services: {} },
            },
          });
        },
      },
    );
    await share.inviteVisitingAgent(share.wholeService);

    const refused = attrsOf(byId(renderTab(share), "ws-share-agents-invite-error") as AnyVnode);
    expect((refused.error as { message: string }).message).toBe(
      "This workspace is too old to invite agents into. *Update it, then try again.*",
    );
    expect(refused.isBugReportOffered).toBe(false);
    expect((refused.actions as { label: string }[]).map((action) => action.label)).toEqual([
      "Update workspace",
    ]);
  });

  it("says no agents have been invited when the list is empty", async () => {
    const share = await agentsPanel([]);
    await share.ensureVisitingAgentsLoaded();
    expect(allText(byId(renderTab(share), "ws-share-agents"))).toContain(
      "No agents have been invited.",
    );
  });

  it("shows the minted instructions with the code and a copy control", async () => {
    const share = await agentsPanel([]);
    await share.ensureVisitingAgentsLoaded();
    await share.inviteVisitingAgent(share.wholeService);
    const root = renderTab(share);
    const instructions = byId(root, "ws-share-agents-instructions");
    expect(instructions).toBeDefined();
    const textarea = collectVnodes(instructions).find(
      (vnode) => vnode.tag === "textarea",
    );
    expect(textarea).toBeDefined();
    expect(attrsOf(textarea as AnyVnode).value).toBe(
      "Join with 7K3M-QX9A-2PFH",
    );
    expect(allText(instructions)).toContain("Copy the instructions");
    expect(allText(instructions)).toContain("7K3M-QX9A-2PFH");
    expect(byId(root, "ws-share-agents-done")).toBeDefined();

    // The code carries the whole workspace's scope, which an app pane does not
    // describe, so it is shown on the pane it was minted on and nowhere else.
    share.selectTarget("web");
    expect(
      byId(renderTab(share), "ws-share-agents-instructions"),
    ).toBeUndefined();
    share.selectTarget(share.wholeService);
    expect(
      byId(renderTab(share), "ws-share-agents-instructions"),
    ).toBeDefined();
  });

  it("scopes the invite to the app pane it is pressed on", async () => {
    const share = await agentsPanel([
      { ...AGENT, scope: ["web"], name: "Web helper" },
      AGENT,
    ]);
    await share.ensureVisitingAgentsLoaded();
    share.selectTarget("web");
    const text = allText(byId(renderTab(share), "ws-share-agents"));
    expect(text).toContain("to use only this app");
    expect(text).toContain("Web helper");
    expect(text).toContain("web");
    expect(text).not.toContain("Josh Claude");
    expect(share.visitingAgentScopeFor("web")).toEqual(["web"]);
  });
});

describe("ShareTab failures it can tell apart", () => {
  it("says an app without a link gets one when it starts, rather than spinning", async () => {
    const share = await publishedPanel();
    share.selectTarget("docs");

    const link = byId(renderTab(share), "ws-share-link");

    expect(allText(link)).toContain(
      "This app gets its link the first time it starts. Open it in this workspace, then come back.",
    );
    expect(collectVnodes(link).some((vnode) => vnode.tag === Spinner)).toBe(
      false,
    );
  });

  it("says once that a stopped workspace must start when turning sharing on fails on it", async () => {
    const share = await readyPanel(
      { grants: null },
      {
        workspaceLiveness: () => "STOPPED",
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? {
                  ok: false,
                  status: 502,
                  body: {
                    error: "Could not check the share state: exec failed",
                    reason: "WORKSPACE_UNREACHABLE",
                  },
                }
              : { ok: true, status: 200, body: { enabled: false, url: null, grants: null } },
          ),
      },
    );
    await share.publish();

    const root = renderTab(share);

    expect(errorOf(root, "ws-share-publish-error")?.message).toBe(
      "This workspace is stopped. *Start it, then try again.*",
    );
    expect(actionsOf(root, "ws-share-publish-error").map((action) => action.label)).toEqual([
      "Start workspace",
    ]);
    expect(byId(root, "ws-share-load-error")).toBeUndefined();
  });

  it("says once that a running workspace couldn't be reached when turning sharing on fails on it", async () => {
    const share = await readyPanel(
      { grants: null },
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? {
                  ok: false,
                  status: 502,
                  body: { error: "exec failed", reason: "WORKSPACE_UNREACHABLE" },
                }
              : { ok: true, status: 200, body: { enabled: false, url: null, grants: null } },
          ),
      },
    );
    await share.publish();

    const root = renderTab(share);

    expect(errorOf(root, "ws-share-publish-error")?.message).toBe(
      "Couldn't reach this workspace to set up sharing. *Make sure it's running, then try again.*",
    );
    expect(actionsOf(root, "ws-share-publish-error")).toEqual([
      expect.objectContaining({ label: "Try again" }),
    ]);
    expect(byId(root, "ws-share-load-error")).toBeUndefined();
  });

  it("shows no link being set up while a failed address move waits on Restart sharing", async () => {
    const share = await readyPanel(
      {},
      {
        fetchJson: () =>
          Promise.resolve({
            ok: false,
            status: 502,
            body: { error: "Failed to write share files", reason: "ADDRESS_MOVE_FAILED" },
          }),
      },
    );

    const root = renderTab(share);

    expect(byId(root, "ws-share-provisioning")).toBeUndefined();
    expect(
      ((attrsOf(byId(root, "ws-share-load-error") as AnyVnode).actions ?? []) as { label: string }[]).map(
        (action) => action.label,
      ),
    ).toEqual(["Restart sharing"]);
  });

  it("offers Restart Imbue Studio when sharing is unavailable, and restarts the app from it", async () => {
    const share = await readyPanel(
      {},
      {
        fetchJson: () =>
          Promise.resolve({
            ok: false,
            status: 503,
            body: { error: "sharing handler not configured", reason: "UNAVAILABLE" },
          }),
      },
    );
    const retry = vi.fn();

    await withMindsNative({ retry }, async () => {
      const [restart] = actionsOf(renderTab(share), "ws-share-load-error");
      expect(restart.label).toBe("Restart Imbue Studio");
      restart.onclick();
    });

    expect(retry).toHaveBeenCalledOnce();
  });

  it("offers to start a stopped workspace for an app's link", async () => {
    const share = await publishedPanel(
      {},
      { workspaceLiveness: () => "STOPPED" },
    );
    share.selectTarget("docs");

    const link = byId(renderTab(share), "ws-share-link");

    expect(allText(link)).toContain(
      "Start this workspace to get this app's link.",
    );
    expect(allText(link)).toContain("Start workspace");
  });

  it("says a link that never answered is not answering, with a Try again", async () => {
    const share = await publishedPanel();
    share.isLive = false;
    share.isLinkDeadlinePassed = true;
    const republish = vi.spyOn(share, "republish").mockResolvedValue();

    const notice = byId(
      renderTab(share),
      "ws-share-link-not-answering",
    ) as AnyVnode;

    expect(attrsOf(notice).error).toEqual({
      message: "This link still isn't answering.",
      details: "",
    });
    expect(attrsOf(notice).isBugReportOffered).toBe(true);
    const [tryAgain, ask] = actionsOf(renderTab(share), "ws-share-link-not-answering");
    expect([tryAgain.label, ask.label]).toEqual(["Try again", "Ask the agent to fix it"]);
    tryAgain.onclick();
    expect(republish).toHaveBeenCalledOnce();
    expect(await assistDescriptionSentBy(ask)).toContain("link hasn't answered");
    const link = byId(renderTab(share), "ws-share-link");
    expect(allText(link)).toContain("The link isn't answering yet");
    expect(allText(link)).not.toContain("Preparing the link");
  });

  it("holds a notice's Ask the agent while its agent starts, and says under that notice why it couldn't", async () => {
    const share = await publishedPanel();
    share.isLive = false;
    share.isLinkDeadlinePassed = true;
    let answer: (response: Response) => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => (answer = resolve))),
    );
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const draw = mountedTab(share);

    actionsOf(draw(), "ws-share-link-not-answering")[1].onclick();
    expect(actionsOf(draw(), "ws-share-link-not-answering")[1]).toEqual(
      expect.objectContaining({ label: "Starting an agent…", isDisabled: true }),
    );

    answer(
      new Response(JSON.stringify({ error: "spawn failed", reason: "SPAWN_FAILED" }), {
        status: 502,
      }),
    );
    await settle();
    const tab = draw();
    const nested = byId(
      renderDeep(tab),
      "ws-share-link-not-answering-ask-agent-error",
    );
    expect(allText(nested)).toContain(
      "Couldn't start an agent in this workspace.",
    );
    // One failure is one box: it is said on the tint already under it.
    expect(
      collectVnodes(nested).some((vnode) =>
        classesOf(vnode).includes("c-important-surface"),
      ),
    ).toBe(false);
    expect(actionsOf(tab, "ws-share-link-not-answering")[1].isDisabled).toBe(false);
  });

  it("offers Remove, not Retry, on an entry the workspace refused", async () => {
    const share = await publishedPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? {
                  ok: false,
                  status: 400,
                  body: {
                    error: "grant_refused",
                    refusals: [
                      {
                        scope: "workspace",
                        kind: "email_domain",
                        value: "gmail.example",
                        message: "gmail.example is not allowed here.",
                      },
                    ],
                  },
                }
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
    share.addGrant("system_interface", "email_domain", "gmail.example");
    await settle();

    const row = rowByText(renderTab(share), "gmail.example");

    expect(allText(row)).toContain("gmail.example is not allowed here.");
    expect(allText(row)).toContain("Remove");
    expect(allText(row)).not.toContain("Retry");
  });

  it("says a failed removal leaves the person's access in place", async () => {
    const share = await publishedPanel(
      {
        grants: {
          workspace: { emails: ["kim@example.org"], email_domains: [] },
          services: {},
        },
      },
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
                      workspace: {
                        emails: ["kim@example.org"],
                        email_domains: [],
                      },
                      services: {},
                    },
                  },
                },
          ),
      },
    );
    share.removeGrant(
      "system_interface",
      share.grantsFor("system_interface")[0].key,
    );
    await settle();

    const row = rowByText(renderTab(share), "kim@example.org");

    expect(allText(row)).toContain("Couldn't remove — still has access");
    expect(allText(row)).toContain("Retry");

    // The card's details line is too small for the whole failure, so See
    // details opens it in the error dialog, with its sentence and the status.
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    errorDialogs.attachHost();
    try {
      const seeDetails = collectVnodes(row).find(
        (vnode) => attrsOf(vnode)["data-failure-details"] !== undefined,
      );
      if (seeDetails === undefined) throw new Error("no See details on the card");
      (attrsOf(seeDetails).onclick as () => void)();
      const dialog = errorDialogs.current;
      expect(dialog?.error.message).toBe(
        "Couldn't save this change. Try again. If this persists, report a bug.",
      );
      expect(dialog?.error.details).toContain("HTTP 500");
      expect(dialog?.isBugReportOffered).toBe(true);
    } finally {
      errorDialogs.dismiss();
      errorDialogs.detachHost();
    }
  });

  it("does not call an unread list empty, nor the workspace unshared, when the status could not be read", async () => {
    const share = await readyPanel({
      grants: {
        workspace: { emails: ["kim@example.org"], email_domains: [] },
        services: {},
      },
      publication_unknown_reason: "UNREADABLE",
    });

    const root = renderTab(share);

    expect(byId(root, "ws-share-off-notice")).toBeUndefined();
    expect(byId(root, "ws-share-publish-switch")).toBeUndefined();
    expect(errorOf(root, "ws-share-load-error")?.message).toBe(
      "Couldn't check whether this workspace is shared. Try again. If this persists, report a bug.",
    );
    expect(actionsOf(root, "ws-share-load-error")).toEqual([
      expect.objectContaining({ label: "Try again" }),
    ]);
    expect(attrsOf(byId(root, "ws-share-load-error") as AnyVnode).isBugReportOffered).toBe(true);
    expect(
      attrsOf(byId(root, "ws-share-add-value") as AnyVnode)["data-tooltip"],
    ).toBe("Sharing status couldn't be checked.");
  });

  it("replaces the empty-list line when no document could be read", async () => {
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: () => Promise.resolve({ ok: false, status: 0, body: null }),
      }),
    );
    await share.load();

    const root = renderTab(share);

    expect(byId(root, "ws-share-empty")).toBeUndefined();
    expect(allText(byId(root, "ws-share-list-unread"))).toBe(
      "The list of who has access couldn't be read.",
    );
    expect(errorOf(root, "ws-share-load-error")?.message).toBe(
      "Imbue Studio didn't respond. Try again in a moment.",
    );
  });

  it("offers to ask the workspace's agent when its sharing list cannot be read", async () => {
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? {
                  ok: false,
                  status: 502,
                  body: {
                    error: "could not decode",
                    reason: "GRANTS_UNREADABLE",
                  },
                }
              : {
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
                },
          ),
      },
    );
    await share.publish();

    const root = renderTab(share);

    expect(errorOf(root, "ws-share-publish-error")?.message).toBe(
      "This workspace's list of who it's shared with can't be read, so sharing can't be changed right now.",
    );
    const [ask] = actionsOf(root, "ws-share-publish-error");
    expect(ask).toEqual(
      expect.objectContaining({ label: "Ask the agent to fix it" }),
    );
    expect(attrsOf(byId(root, "ws-share-publish-error") as AnyVnode).isBugReportOffered).toBe(
      true,
    );
    expect(await assistDescriptionSentBy(ask)).toContain("share_grants.toml");
  });

  it.each([
    {
      failure: "a failed start",
      respond: () =>
        Promise.resolve(
          new Response(
            JSON.stringify({ error: "spawn failed", reason: "SPAWN_FAILED" }),
            { status: 502 },
          ),
        ),
      message: fallbackErrorSentence(
        "Couldn't start an agent in this workspace",
      ),
      details: "spawn failed",
      isBugReportOffered: true,
    },
    {
      failure: "a refused start",
      respond: () =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              error: "This workspace can't start a helper agent yet.",
            }),
            { status: 409 },
          ),
        ),
      message: "This workspace can't start a helper agent yet.",
      details: "HTTP 409",
      isBugReportOffered: false,
    },
    {
      failure: "a start that got no response",
      respond: () => Promise.reject(new TypeError("Failed to fetch")),
      message: NO_RESPONSE_SENTENCE,
      details: "No response.",
      isBugReportOffered: false,
    },
  ])("raises $failure pressed from a card's error dialog in a dialog of its own, and only there", async (failure) => {
    const share = await publishedPanel(
      {
        grants: {
          workspace: { emails: ["kim@example.org"], email_domains: [] },
          services: {},
        },
      },
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? {
                  ok: false,
                  status: 502,
                  body: { error: "could not decode", reason: "GRANTS_UNREADABLE" },
                }
              : {
                  ok: true,
                  status: 200,
                  body: {
                    enabled: true,
                    url: "https://m.relay.example/",
                    grants: {
                      workspace: { emails: ["kim@example.org"], email_domains: [] },
                      services: {},
                    },
                  },
                },
          ),
      },
    );
    share.removeGrant("system_interface", share.grantsFor("system_interface")[0].key);
    await settle();
    const draw = mountedTab(share);
    const row = rowByText(draw(), "kim@example.org");

    vi.stubGlobal("fetch", vi.fn(failure.respond));
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    errorDialogs.attachHost();
    try {
      const seeDetails = collectVnodes(row).find(
        (vnode) => attrsOf(vnode)["data-failure-details"] !== undefined,
      );
      if (seeDetails === undefined) throw new Error("no See details on the card");
      (attrsOf(seeDetails).onclick as () => void)();
      const ask = errorDialogs.current?.actions?.find(
        (action) => action.label === "Ask the agent to fix it",
      );
      if (ask === undefined) throw new Error("no Ask the agent in the dialog");
      // The dialog closes as its button is pressed.
      errorDialogs.dismiss();
      ask.onclick();
      await settle();

      expect(errorDialogs.current?.error.message).toBe(failure.message);
      expect(errorDialogs.current?.error.details).toContain(failure.details);
      expect(errorDialogs.current?.isBugReportOffered).toBe(
        failure.isBugReportOffered,
      );
      // Another notice's Ask the agent is a button of its own: neither the
      // start nor its failure is drawn there.
      share.isLive = false;
      share.isLinkDeadlinePassed = true;
      const tab = draw();
      expect(
        byId(renderDeep(tab), "ws-share-link-not-answering-ask-agent-error"),
      ).toBeUndefined();
      expect(actionsOf(tab, "ws-share-link-not-answering")[1].label).toBe(
        "Ask the agent to fix it",
      );
    } finally {
      errorDialogs.dismiss();
      errorDialogs.detachHost();
    }
  });

  it("opens the workspace's Owner section from a refusal for a workspace linked to no account", async () => {
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === "PUT"
              ? {
                  ok: false,
                  status: 409,
                  body: { error: "no account owns this workspace", reason: "NOT_LINKED" },
                }
              : {
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
                },
          ),
      },
    );
    await share.publish();
    const stores = createEmptyStores();
    const shell = new ShellState(stores);
    registerAppContext({ stores, shell });
    const openSettings = vi.spyOn(shell, "openSettings").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    try {
      const root = renderTab(share);
      expect(errorOf(root, "ws-share-publish-error")?.message).toBe(
        "Link this workspace to your Imbue Cloud account before sharing it.",
      );
      const [open] = actionsOf(root, "ws-share-publish-error");
      expect(open.label).toBe("Open account settings");

      open.onclick();

      expect(openSettings.mock.calls).toEqual([
        [ownerSectionQuery(share.workspaceAgentId)],
      ]);
      // It opens Settings in place, and routes nowhere itself.
      expect(routeSet).not.toHaveBeenCalled();
    } finally {
      clearAppContextForTests();
    }
  });

  it("offers Try again as the main button when turning sharing on fails at Imbue Cloud", async () => {
    let isConnectorDown = true;
    let isPublished = false;
    const share = await readyPanel(
      {},
      {
        fetchJson: (_url: string, init?: RequestInit) => {
          if (init?.method === "PUT" && isConnectorDown)
            return Promise.resolve({
              ok: false,
              status: 502,
              body: { error: "upstream connect error", reason: "CONNECTOR_FAILED" },
            });
          if (init?.method === "PUT") isPublished = true;
          return Promise.resolve({
            ok: true,
            status: 200,
            body: { enabled: isPublished, url: null, grants: null },
          });
        },
      },
    );
    await share.publish();

    const [retry] = actionsOf(renderTab(share), "ws-share-publish-error");
    expect(retry).toEqual(expect.objectContaining({ label: "Try again" }));

    isConnectorDown = false;
    retry.onclick();
    await settle();

    expect(byId(renderTab(share), "ws-share-publish-error")).toBeUndefined();
    expect(share.isPublished).toBe(true);
  });
});

/** The buttons an error display with this id offers. */
function actionsOf(
  root: unknown,
  id: string,
): ErrorAction[] {
  const display = byId(root, id);
  expect(display, `no error display ${id}`).toBeDefined();
  return (attrsOf(display as AnyVnode).actions ?? []) as ErrorAction[];
}

/** What the agent is asked when `action` is pressed: the description its
 * /help/assist request carries. The request is refused, so nothing past it
 * (entering the workspace) runs here. */
async function assistDescriptionSentBy(action: { onclick: () => void }): Promise<string> {
  const assist = vi.fn((_url: string, _init?: RequestInit) =>
    Promise.resolve(new Response("{}", { status: 500 })),
  );
  vi.stubGlobal("fetch", assist);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  action.onclick();
  await settle();
  expect(assist.mock.calls[0]?.[0]).toBe("/help/assist");
  const init = assist.mock.calls[0]?.[1];
  return String((JSON.parse(String(init?.body)) as { description?: string }).description);
}

describe("ShareTab mobile access coach bubble", () => {
  const BUBBLE_ID = "ws-share-mobile-link-coach";

  function bubbleOf(root: unknown): AnyVnode | undefined {
    return byId(root, BUBBLE_ID);
  }

  function pressMobileLink(root: unknown): void {
    (
      attrsOf(byId(root, "ws-share-mobile-link-btn") as AnyVnode)
        .onclick as () => void
    )();
  }

  it("points a bubble at the control when the panel was opened with the mark", async () => {
    const share = await publishedPanel();
    share.showCoachMark("mobile-access");

    const bubble = bubbleOf(renderTab(share)) as AnyVnode;

    expect(bubble.tag).toBe(CoachBubble);
    expect(attrsOf(bubble).text).toBe("Use this to open Studio on your phone.");
    // Inside the control, beside the button it points at.
    expect(
      bubbleOf(byId(renderTab(share), "ws-share-mobile-link")),
    ).toBeDefined();
    const drawn = bubbleOf(renderTabDeep(share));
    expect(allText(drawn)).toBe(
      "Use this to open Studio on your phone. Got it",
    );
  });

  it("draws no bubble for a panel opened without the mark", async () => {
    const share = await publishedPanel();

    expect(share.coachMark).toBeNull();
    expect(bubbleOf(renderTab(share))).toBeUndefined();
  });

  it("takes the bubble down on its button, and redraws", async () => {
    const redraw = vi.fn();
    const share = await publishedPanel({}, { redraw });
    share.showCoachMark("mobile-access");
    redraw.mockClear();

    (attrsOf(bubbleOf(renderTab(share)) as AnyVnode).onDismiss as () => void)();

    expect(bubbleOf(renderTab(share))).toBeUndefined();
    expect(redraw).toHaveBeenCalledTimes(1);
  });

  it("takes the bubble down when the control it points at is pressed", async () => {
    const share = await publishedPanel();
    share.showCoachMark("mobile-access");

    pressMobileLink(renderTab(share));

    expect(bubbleOf(renderTab(share))).toBeUndefined();
    expect(share.mobileLinkState.state).toBe("sending");
  });

  it("points at the control while sharing is off and it is dimmed, and a press there answers the bubble too", async () => {
    const share = await readyPanel();
    share.showCoachMark("mobile-access");

    const root = renderTab(share);

    expect(
      attrsOf(byId(root, "ws-share-mobile-link-btn") as AnyVnode)[
        "aria-disabled"
      ],
    ).toBe("true");
    expect(bubbleOf(root)).toBeDefined();
    pressMobileLink(root);
    expect(bubbleOf(renderTab(share))).toBeUndefined();
    expect(share.mobileLinkState.state).toBe("idle");
  });

  it("points at the control on an app's page too, where it emails the app's link", async () => {
    const share = await publishedPanel();
    share.showCoachMark("mobile-access");
    share.selectTarget("web");

    expect(bubbleOf(byId(renderTab(share), "ws-share-mobile-link"))).toBeDefined();

    share.selectTarget(WHOLE);
    expect(bubbleOf(byId(renderTab(share), "ws-share-mobile-link"))).toBeDefined();
  });

  it("points at the control while the sharing document is still being read", async () => {
    let answer: (result: RequestResult) => void = () => undefined;
    const share = new SharePanelModel(
      sharePanelOptions({
        fetchJson: () =>
          new Promise<RequestResult>((resolve) => {
            answer = resolve;
          }),
      }),
    );
    const loading = share.load();
    share.showCoachMark("mobile-access");

    expect(bubbleOf(byId(renderTab(share), "ws-share-mobile-link"))).toBeDefined();

    answer({
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
    });
    await loading;
    expect(bubbleOf(byId(renderTab(share), "ws-share-mobile-link"))).toBeDefined();
  });

  it("asks for a redraw only when there is a bubble to take down", async () => {
    const redraw = vi.fn();
    const share = await publishedPanel({}, { redraw });
    redraw.mockClear();

    share.dismissCoachMark();

    expect(redraw).not.toHaveBeenCalled();
  });
});
