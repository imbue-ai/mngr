// What the Updates panel actually puts on screen. The model tests next door
// prove the decisions; these prove the panel states them -- and, for a channel
// that serves nothing, that the click never reaches the model at all.

import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PeekedChannel, UpdateState, UpdateStatus } from "../../../electron-bridge";
import { resetNotificationPrefsForTests } from "../../../models/notificationsUi";
import {
  SETTINGS_SECTIONS,
  SettingsModel,
  type SettingsOverview,
} from "../../../models/settings";
import { SettingsSections } from "./SettingsSections";
import { Button } from "../../components/Button";
import { jsonResponse } from "../../../testing";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectText,
  collectVnodes,
  renderRoot,
  renderedText,
  withMindsNative,
} from "../../../testing";

afterEach(() => {
  resetNotificationPrefsForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const ON_STABLE: UpdateState = {
  channel: "stable",
  currentVersion: "0.4.30",
  available: ["stable", "beta", "alpha"],
  status: { type: "up-to-date" },
};

const PARKED: UpdateStatus = {
  type: "parked",
  channel: "stable",
  currentVersion: "0.4.30",
  feedVersion: "0.4.12",
};

/** Running 0.4.30 on stable: stable is behind us, the faster channels are level. */
const PEEKED: Record<string, PeekedChannel> = {
  stable: { version: "0.4.12", wouldPark: true },
  beta: { version: "0.4.30", wouldPark: false },
  alpha: { version: "0.4.30", wouldPark: false },
};

function updatesModel(overrides: Partial<SettingsModel> = {}): SettingsModel {
  const model = new SettingsModel(undefined, () => {});
  model.activeSection = "updates";
  model.updateState = ON_STABLE;
  model.peekedChannels = PEEKED;
  return Object.assign(model, overrides);
}

function panelText(model: SettingsModel): string {
  return renderedText(renderRoot(SettingsSections, { model }));
}

interface ElementVnode {
  tag: unknown;
  attrs: Record<string, unknown> | null;
  children?: unknown;
}

/** The channel radios in the rendered tree, in the order the panel lists them. */
function channelRadios(node: unknown): Record<string, unknown>[] {
  if (node === null || node === undefined || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(channelRadios);
  const vnode = node as ElementVnode;
  const own =
    vnode.tag === "input" && vnode.attrs !== null && vnode.attrs.name === "update-channel"
      ? [vnode.attrs]
      : [];
  return [...own, ...channelRadios(vnode.children)];
}

/** Whether the panel's Check now control is refused. */
function isCheckDisabled(model: SettingsModel): boolean {
  return collectVnodes(renderRoot(SettingsSections, { model })).some((node) => {
    const label = collectText(node).join("").trim();
    if (label !== "Check now" && label !== "Checking...") return false;
    const attrs = (node as { attrs?: Record<string, unknown> | null }).attrs;
    return attrs != null && attrs.disabled === true;
  });
}

describe("the Updates panel", () => {
  it("says being parked plainly, without raising it as a fault", async () => {
    // Being ahead of your channel is temporary and self-correcting, so this is
    // not a warning. But it cannot go unsaid either: with nothing on screen the
    // panel redraws identically to up-to-date, and a switch that just parked the
    // user looks like a click that did nothing.
    await withMindsNative({}, async () => {
      const text = panelText(updatesModel({ updateState: { ...ON_STABLE, status: PARKED } }));

      expect(text).toContain("Stable is at 0.4.12, so you will stay on 0.4.30 until it catches up.");
      expect(text).not.toContain("not receiving updates");
      expect(text).not.toContain("Switch to alpha");
      expect(text).toContain("You are running Minds 0.4.30.");
    });
  });

  it("names the channel the way the panel does, never the bare feed name", async () => {
    // The status carries `stable`; the radio beside it says `Stable`. A sentence
    // interpolating the raw name reads as a different thing from the control it
    // is about.
    await withMindsNative({}, async () => {
      expect(panelText(updatesModel({ updateState: { ...ON_STABLE, status: PARKED } }))).not.toContain(
        "stable is at",
      );
    });
  });

  it("states each outcome that has something to say", async () => {
    // The panel's whole account of an update in progress, and of a channel that
    // has quietly stopped serving. Every one of these reads an optional field
    // off the shared status shape, so each is one rename away from putting
    // "Downloading undefined..." on screen.
    const cases: [string, UpdateStatus, string][] = [
      [
        "a check that could not reach the feed",
        { type: "error", channel: "stable", message: "getaddrinfo ENOTFOUND" },
        "Update check failed: getaddrinfo ENOTFOUND",
      ],
      [
        "an artifact waiting to be installed",
        { type: "update-downloaded", version: "0.5.0" },
        "Minds 0.5.0 is downloaded. Restart to install.",
      ],
      [
        "a transfer in flight",
        { type: "update-available", channel: "stable", feedVersion: "0.5.0" },
        "Downloading 0.5.0",
      ],
      [
        "a dev run, where there is no bundle to swap",
        { type: "disabled", reason: "not-packaged" },
        "Updates are only available in installed builds.",
      ],
    ];
    await withMindsNative({}, async () => {
      for (const [name, status, sentence] of cases) {
        expect(panelText(updatesModel({ updateState: { ...ON_STABLE, status } })), name).toContain(sentence);
      }
    });
  });

  it("reports when a check last ran, which is the only sign most checks give", async () => {
    // The two common outcomes -- up to date, and ahead of this channel -- both
    // redraw the panel to the strings it already showed. Without the time, a
    // check that worked is indistinguishable from a button that does nothing.
    const justNow = { ...ON_STABLE, lastCheckedAt: new Date(Date.now() - 5_000).toISOString() };
    const minutesAgo = { ...ON_STABLE, lastCheckedAt: new Date(Date.now() - 8 * 60_000).toISOString() };
    // Checks run every ten minutes, so this far back means they stopped, and
    // the time it happened beats counting the days since.
    const longAgo = { ...ON_STABLE, lastCheckedAt: new Date(Date.now() - 3 * 86_400_000).toISOString() };
    await withMindsNative({}, async () => {
      expect(panelText(updatesModel({ updateState: justNow }))).toContain("Checked just now.");
      expect(panelText(updatesModel({ updateState: minutesAgo }))).toContain("Checked 8 mins ago.");
      const stale = panelText(updatesModel({ updateState: longAgo }));
      expect(stale).toContain("Checked at");
      expect(stale).not.toContain("days ago");
      // Nothing to report before the first check, and not while one is running.
      expect(panelText(updatesModel({}))).not.toContain("Checked");
      expect(panelText(updatesModel({ updateState: justNow, isUpdateBusy: true }))).not.toContain("Checked");
    });
  });

  it("lists every channel this build serves, slowest first", async () => {
    await withMindsNative({}, async () => {
      const model = updatesModel({});
      const text = panelText(model);

      expect(text.indexOf("Stable")).toBeLessThan(text.indexOf("Beta"));
      expect(text.indexOf("Beta")).toBeLessThan(text.indexOf("Alpha"));
      expect(channelRadios(renderRoot(SettingsSections, { model }))).toHaveLength(3);
    });
  });

  it("hides a channel this build cannot serve", async () => {
    // A build naming no manifest host reaches only ToDesktop's own feed, so
    // offering the faster channels would offer a switch that breaks checking.
    await withMindsNative({}, async () => {
      const stableOnly: UpdateState = { ...ON_STABLE, available: ["stable"] };
      const model = updatesModel({ updateState: stableOnly });
      const text = panelText(model);

      expect(text).toContain("Stable");
      expect(text).not.toContain("Beta");
      expect(text).not.toContain("Alpha");
      expect(text).not.toContain("Internal channels");
      expect(channelRadios(renderRoot(SettingsSections, { model }))).toHaveLength(1);
    });
  });

  it("still shows the channel in effect when this build cannot serve it", async () => {
    // A preference written by a build that had a manifest host survives into
    // one that does not, and readChannel resolves it against every known
    // channel. Leaving it out renders a list with nothing selected and the
    // channel actually in use named nowhere.
    await withMindsNative({}, async () => {
      const stranded: UpdateState = { ...ON_STABLE, channel: "alpha", available: ["stable"] };
      const model = updatesModel({ updateState: stranded });
      const radios = channelRadios(renderRoot(SettingsSections, { model }));

      expect(panelText(model)).toContain("Alpha");
      expect(radios).toHaveLength(2);
      expect(radios[1].checked).toBe(true);
    });
  });

  it("keeps alpha behind a disclosure, so it is never a stray click", async () => {
    await withMindsNative({}, async () => {
      const model = updatesModel({});
      const root = renderRoot(SettingsSections, { model });
      const details = collectVnodes(root).filter((vnode) => vnode.tag === "details");

      expect(panelText(model)).toContain("Internal channels");
      expect(details).toHaveLength(1);
      expect(details[0].attrs?.open).toBe(false);
      expect(channelRadios(root)).toHaveLength(3);
    });
  });

  it("opens the disclosure when alpha is the channel in effect", async () => {
    // What you are running is never behind something you have to open. The row
    // stays put rather than moving into the list: selecting a channel should
    // not make the group it lives in disappear under the cursor.
    await withMindsNative({}, async () => {
      const onAlpha: UpdateState = { ...ON_STABLE, channel: "alpha" };
      const model = updatesModel({ updateState: onAlpha });
      const root = renderRoot(SettingsSections, { model });
      const details = collectVnodes(root).filter((vnode) => vnode.tag === "details");
      const radios = channelRadios(root);

      expect(details).toHaveLength(1);
      expect(details[0].attrs?.open).toBe(true);
      expect(radios).toHaveLength(3);
      expect(radios[2].checked).toBe(true);
    });
  });

  it("marks a channel with no readable manifest unavailable AND unselectable", async () => {
    // The model refuses this switch too, but only if the click gets there.
    // Disabling the radio is what stops a user selecting a channel that would
    // serve them nothing.
    const unpublishedAlpha = { ...PEEKED, alpha: { version: null, wouldPark: false, error: "404" } };
    await withMindsNative({}, async () => {
      const model = updatesModel({ peekedChannels: unpublishedAlpha });
      const text = panelText(model);
      const radios = channelRadios(renderRoot(SettingsSections, { model }));

      expect(text).toContain("Unavailable right now.");
      expect(radios).toHaveLength(3);
      expect(radios[2].disabled).toBe(true);
      expect(radios[0].disabled).toBe(false);
      expect(radios[0].checked).toBe(true);
    });
  });

  it("says the state is being read, not that updates are somebody else's business", async () => {
    // The menu bar's "Check for Updates..." opens this panel from oninit, so it
    // renders before loadUpdateState resolves -- and the panel is desktop-only,
    // so the browser copy would be wrong for every reader who can see it.
    await withMindsNative({}, async () => {
      const loading = updatesModel({ updateState: null });

      expect(panelText(loading)).toContain("Reading the update state...");
      expect(panelText(loading)).not.toContain("Updates are managed by the desktop app.");
    });
  });

  it("tells a desktop user why the update state could not be read", async () => {
    // Falling through to the browser copy would show a desktop user "Updates
    // are managed by the desktop app." with no version, channel, or reason.
    await withMindsNative({}, async () => {
      const broken = updatesModel({ updateState: null, updateError: "MINDS_ROOT_NAME is unreadable" });

      expect(panelText(broken)).toContain("Could not read the update state: MINDS_ROOT_NAME is unreadable");
      expect(panelText(broken)).not.toContain("Updates are managed by the desktop app.");
    });
  });

  it("states the cost of a parking switch before it is committed", async () => {
    await withMindsNative({}, async () => {
      const text = panelText(
        updatesModel({ pendingChannelSwitch: { channel: "stable", targetVersion: "0.4.12" } }),
      );

      expect(text).toContain("Switch to Stable?");
      expect(text).toContain("Stable is at 0.4.12.");
      expect(text).toContain("You will stay on 0.4.30 until it catches up.");
    });
  });

  it("says where the restart lands, since a slower channel does not hold a download back", async () => {
    // The download was handed to the OS installer as it landed, so switching
    // empties the updater's cache but cannot take it back: the restart moves
    // FORWARD to 0.5.0 despite the switch, and the wait for stable gets longer
    // rather than shorter. Saying only "still installs" leaves the user
    // expecting to land on the version they can see.
    const staged = { ...ON_STABLE, channel: "alpha" as const, downloadedVersion: "0.5.0" };
    const pendingChannelSwitch = { channel: "stable" as const, targetVersion: "0.4.12" };
    await withMindsNative({}, async () => {
      const text = panelText(updatesModel({ updateState: staged, pendingChannelSwitch }));

      expect(text).toContain("Minds 0.5.0 is already downloaded and will still install when you restart");
      expect(text).toContain("you will stay on it until Stable passes it");
      // With nothing staged there is nothing to warn about, so the sentence must
      // not be unconditional.
      expect(panelText(updatesModel({ pendingChannelSwitch }))).not.toContain("already downloaded");
    });
  });

  it("keeps the staged sentence through a check that failed after the download", async () => {
    // The status that announced the download is transient and any later check
    // replaces it -- but the artifact is with the installer and still goes in.
    // Reading the status here is what made a network blip retract a true promise
    // at the exact moment it was being weighed.
    const staged = {
      ...ON_STABLE,
      channel: "alpha" as const,
      downloadedVersion: "0.5.0",
      status: { type: "error" as const, message: "ENOTFOUND" },
    };
    await withMindsNative({}, async () => {
      const text = panelText(
        updatesModel({ updateState: staged, pendingChannelSwitch: { channel: "stable", targetVersion: "0.4.12" } }),
      );

      expect(text).toContain("Minds 0.5.0 is already downloaded");
    });
  });

  it("stops offering a check while the download that check started is running", async () => {
    // The check has already answered; what is still running is the transfer it
    // began. A check clicked now is queued behind that transfer in the main
    // process and would answer minutes later, so the button is refused rather
    // than left live to look broken. The status line above it says why.
    await withMindsNative({}, async () => {
      const downloading = updatesModel({
        updateState: {
          ...ON_STABLE,
          status: { type: "update-available", channel: "stable", feedVersion: "0.5.0" },
        },
      });
      expect(renderedText(renderRoot(SettingsSections, { model: downloading }))).toContain("Downloading 0.5.0");
      expect(isCheckDisabled(downloading)).toBe(true);
      // Idle, it is live again.
      expect(isCheckDisabled(updatesModel())).toBe(false);
    });
  });

  it("offers a restart in the panel, because the floating card can be dismissed for good", async () => {
    // installUpdate() is otherwise reachable only from the card, which does not
    // come back for a version already dismissed -- leaving a finished download
    // with no way to install it short of quitting by hand.
    await withMindsNative({}, async () => {
      expect(panelText(updatesModel({ updateState: { ...ON_STABLE, downloadedVersion: "0.5.0" } }))).toContain(
        "Restart now",
      );
      expect(panelText(updatesModel())).not.toContain("Restart now");
    });
  });
});

/** The sections view, rendered from a bare model: every panel guards on a
 * null overview and renders nothing, so the layout is exercised without a
 * payload fixture or any network. */
function renderSections(
  model: SettingsModel = new SettingsModel(),
): AnyVnode[] {
  const instance = SettingsSections() as unknown as m.Component;
  const vnode = m(instance, { model } as unknown as m.Attributes) as m.Vnode;
  const rendered = (
    instance.view as unknown as (v: m.Vnode) => m.Children
  ).call(instance, vnode);
  return rendered as unknown as AnyVnode[];
}

/** The nav column and the panel column of the settings pane, in that order. */
function columns(): AnyVnode[] {
  const row = renderSections()[0];
  return row.children as AnyVnode[];
}

// The nav lists the sections this build can service, which it reads off the
// native bridge, and vitest runs without a `window` -- so every render below
// declares which build it is.
describe("SettingsSections layout", () => {
  it("scrolls the section list and the panel independently of each other", async () => {
    // Regression guard for the bug this pane replaced: the app-overlay card was
    // the only scroller, so reading down a long panel carried the section list
    // off the top of the card with it.
    await withMindsNative({}, async () => {
      const [nav, panel] = columns();
      expect(nav.tag).toBe("nav");
      expect(nav.attrs?.["aria-label"]).toBe("Settings sections");
      for (const column of [nav, panel]) {
        expect(classTokensOf(column)).toEqual(
          expect.arrayContaining(["overflow-y-auto", "min-h-0"]),
        );
      }
    });
  });

  it("takes a bounded height from the card rather than growing to its content", async () => {
    // items-start (the old row) leaves both columns content-height, so their
    // overflow never bites however the card above them is shaped.
    await withMindsNative({}, async () => {
      const row = renderSections()[0];
      expect(classTokensOf(row)).toEqual(expect.arrayContaining(["flex-1", "min-h-0"]));
      expect(classTokensOf(row)).not.toContain("items-start");
    });
  });

  it("keeps the group headings and every section in the nav", async () => {
    await withMindsNative({}, async () => {
      const [nav] = columns();
      const navText = collectText(nav).join(" ");
      for (const heading of ["Permissions", "Other"]) {
        expect(navText, heading).toContain(heading);
      }
      for (const section of SETTINGS_SECTIONS) {
        expect(navText, section.label).toContain(section.label);
      }
    });
  });

  it("leaves Updates out of the nav in the browser build", async () => {
    // There is no binary to update in a browser, so the entry would open a
    // panel whose whole content is that it is not for you.
    await withMindsNative(null, async () => {
      const navText = collectText(columns()[0]).join(" ");
      expect(navText).not.toContain("Updates");
      expect(navText).toContain("Error reporting");
    });
  });

  it("keeps the revoke dialog beside the pane, not inside it as a third column", async () => {
    // The pane's contract is two columns; a fixed-position dialog parked in the
    // row would be a real bug the moment it stopped being fixed-position.
    await withMindsNative({}, async () => {
      const [, ...siblings] = renderSections();
      expect(siblings).toHaveLength(1);
      expect(columns()).toHaveLength(2);
    });
  });

  it("styles its nav entries like every other pane's", async () => {
    // "The main page's settings have a different left menu" -- they no longer
    // do: the entries come from the shared recipe, not a fifth one.
    await withMindsNative({}, async () => {
      const [nav] = columns();
      const entries = collectVnodes(nav).filter((vnode) => vnode.tag === "button");
      expect(entries).toHaveLength(SETTINGS_SECTIONS.length);
      for (const entry of entries) {
        expect(classTokensOf(entry)).toEqual(
          expect.arrayContaining(["type-body", "rounded-md", "text-primary"]),
        );
      }
    });
  });
});

const NOTIFICATIONS_OVERVIEW: SettingsOverview = {
  services_overview: [],
  file_sharing_grants: [],
  workspace_delegation_grants: [],
  permissions_unavailable: false,
  is_master_password_set: false,
  report_unexpected_errors: true,
  notification_prefs: {
    is_enabled: true,
    style: "cards",
    is_os_hint_dismissed: false,
    version: "np-1",
  },
  version: "v-one",
};

/** A model on the Notifications section with the given overview loaded. */
async function notificationsModel(
  onWrite: (body: unknown) => Response = () =>
    jsonResponse({ version: "np-2" }),
): Promise<SettingsModel> {
  const model = new SettingsModel(
    async (input, init) =>
      String(input).endsWith("/settings/notifications")
        ? onWrite(JSON.parse(String(init?.body)))
        : jsonResponse(NOTIFICATIONS_OVERVIEW),
    () => {},
  );
  await model.load();
  model.selectSection("notifications");
  return model;
}

describe("SettingsSections notifications panel", () => {
  it("offers the master toggle and the three-style radio, current style checked", async () => {
    vi.stubGlobal("window", {});
    const panel = renderSections(await notificationsModel());
    const toggle = collectVnodes(panel).find(
      (vnode) => attrsOf(vnode).id === "notifications-enabled-toggle",
    );
    expect(toggle).toBeDefined();
    expect(attrsOf(toggle as AnyVnode).checked).toBe(true);

    const group = collectVnodes(panel).find(
      (vnode) => attrsOf(vnode).role === "radiogroup",
    );
    expect(group).toBeDefined();
    const radios = collectVnodes(group).filter(
      (vnode) => attrsOf(vnode).role === "radio",
    );
    expect(radios.map((radio) => attrsOf(radio).id)).toEqual([
      "notification-style-cards",
      "notification-style-os",
      "notification-style-both",
    ]);
    expect(radios.map((radio) => attrsOf(radio)["aria-checked"])).toEqual([
      "true",
      "false",
      "false",
    ]);
    expect(allText(group)).toContain("In-app cards");
    expect(allText(group)).toContain("System notifications");
    expect(allText(group)).toContain("Both");
  });

  it("hides the style radio while the master toggle is off", async () => {
    vi.stubGlobal("window", {});
    const model = await notificationsModel();
    (model.overview as SettingsOverview).notification_prefs = {
      is_enabled: false,
      style: "both",
      is_os_hint_dismissed: false,
      version: "np-1",
    };
    const panel = renderSections(model);
    expect(
      collectVnodes(panel).some(
        (vnode) => attrsOf(vnode).role === "radiogroup",
      ),
    ).toBe(false);
  });

  it("writes the picked style and asks the browser for permission in browser mode", async () => {
    // Browser mode: no window.mindsNative. Permission undecided.
    vi.stubGlobal("window", {});
    const requestPermission = vi.fn(async () => "granted");
    vi.stubGlobal("Notification", { permission: "default", requestPermission });
    const writtenBodies: unknown[] = [];
    const model = await notificationsModel((body) => {
      writtenBodies.push(body);
      return jsonResponse({ version: "np-2" });
    });
    const panel = renderSections(model);
    const osRadio = collectVnodes(panel).find(
      (vnode) => attrsOf(vnode).id === "notification-style-os",
    );
    (attrsOf(osRadio as AnyVnode).onclick as () => void)();
    expect(writtenBodies).toEqual([
      { is_enabled: true, style: "os", is_os_hint_dismissed: false },
    ]);
    expect(requestPermission).toHaveBeenCalledTimes(1);
  });

  it("offers to open OS notification settings in desktop mode whenever system delivery is selected", async () => {
    const openNotificationSettings = vi.fn(async () => true);
    vi.stubGlobal("window", {
      mindsNative: { platform: "darwin", openNotificationSettings },
    });
    const model = await notificationsModel();
    (model.overview as SettingsOverview).notification_prefs = {
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: false,
      version: "np-1",
    };

    const panel = renderSections(model);

    const notice = collectVnodes(panel).find((vnode) =>
      allText(vnode).includes("Open System Settings"),
    );
    expect(notice).toBeDefined();
    const button = collectVnodes(panel).find((vnode) => vnode.tag === Button);
    expect(button).toBeDefined();
    (attrsOf(button as AnyVnode).onclick as () => void)();
    expect(openNotificationSettings).toHaveBeenCalledTimes(1);
  });

  it("surfaces the failure inline when opening OS settings doesn't work (e.g. an unsupported Linux desktop environment)", async () => {
    vi.stubGlobal("window", {
      mindsNative: {
        platform: "linux",
        openNotificationSettings: vi.fn(async () => false),
      },
    });
    const model = await notificationsModel();
    (model.overview as SettingsOverview).notification_prefs = {
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: false,
      version: "np-1",
    };

    expect(allText(renderSections(model))).not.toContain(
      "Couldn't open System Settings",
    );

    await model.openNotificationOsSettings();

    expect(allText(renderSections(model))).toContain(
      "Couldn't open System Settings",
    );
  });

  it.each([
    ["the style is cards-only", { style: "cards" as const }],
    ["notifications are off", { is_enabled: false, style: "both" as const }],
  ])("does not offer to open OS settings when %s", async (_label, overrides) => {
    vi.stubGlobal("window", {
      mindsNative: { platform: "darwin", openNotificationSettings: vi.fn() },
    });
    const model = await notificationsModel();
    (model.overview as SettingsOverview).notification_prefs = {
      is_enabled: true,
      is_os_hint_dismissed: false,
      version: "np-1",
      ...overrides,
    };

    const panel = renderSections(model);

    expect(allText(panel)).not.toContain("Open System Settings");
  });

  it("does not offer to open OS settings in browser mode", async () => {
    vi.stubGlobal("window", {});
    const model = await notificationsModel();
    (model.overview as SettingsOverview).notification_prefs = {
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: false,
      version: "np-1",
    };

    const panel = renderSections(model);

    expect(allText(panel)).not.toContain("Open System Settings");
  });

  it("surfaces a failed write beside the controls", async () => {
    vi.stubGlobal("window", {});
    const model = await notificationsModel(() =>
      jsonResponse({ error: "nope" }, 500),
    );
    await model.setNotificationPrefs({
      is_enabled: false,
      style: "cards",
      is_os_hint_dismissed: false,
    });
    const panel = renderSections(model);
    const alert = collectVnodes(panel).find(
      (vnode) => attrsOf(vnode).role === "alert",
    );
    expect(alert).toBeDefined();
    expect(allText(alert)).toBe("nope");
  });
});
