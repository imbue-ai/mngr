import { afterEach, describe, expect, it, vi } from "vitest";
import type { UiWorkspaceEntry } from "../channel/messages";
import type { UiSharedWorkspace } from "../generated/ui";
import {
  SHARED_WORKSPACE_DOMAIN,
  sharedWorkspace,
  sharedWorkspaceEntry,
  workspacesMessage,
} from "../testing";
import {
  SharedWithMeError,
  homeSharedRows,
  isDeclinedInvitation,
  isHostOfShare,
  isInvitation,
  isRetryOffered,
  isShellLabel,
  isWorkspaceShare,
  openTargetApp,
  pastedLinkAppName,
  pastedLinkLabel,
  primaryTarget,
  relativeTimeLabel,
  sharedAppsOf,
  sharedActionErrorMessage,
  isShareLive,
  isSharedEntry,
  ownedWorkspaceEntries,
  ownerLabel,
  primaryLink,
  shareKeyOfRouteId,
  sharedEntryForHost,
  sharedRouteId,
  sharedWithMeApi,
  statusLabel,
  verdictLabel,
} from "./sharedWithMe";

const DOMAIN = SHARED_WORKSPACE_DOMAIN;

/** An entry of the account's own: the fields a row needs, keyed as given. */
function entry(overrides: Partial<UiWorkspaceEntry> = {}): UiWorkspaceEntry {
  return sharedWorkspaceEntry(
    {},
    { id: "agent-" + "1".repeat(32), name: "Alpha", is_owned: true, share: null, ...overrides },
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("shared row helpers", () => {
  it("recognizes a shared row only when it is not owned and carries a share", () => {
    expect(isSharedEntry(sharedWorkspaceEntry())).toBe(true);
    expect(isSharedEntry(entry())).toBe(false);
    expect(isSharedEntry(entry({ is_owned: false }))).toBe(false);
  });

  it("keeps only the account's own rows for the pickers that act as an owner", () => {
    const owned = entry({ id: "agent-" + "2".repeat(32) });
    const shared = sharedWorkspaceEntry();
    expect(ownedWorkspaceEntries([shared, owned])).toEqual([owned]);
    expect(ownedWorkspaceEntries([])).toEqual([]);
  });

  it("routes a coordinate-shaped key as itself and a digest key through the shared alias", () => {
    const key = "agent-" + "a".repeat(32);
    expect(sharedRouteId(key)).toBe(key);
    expect(sharedRouteId("host-" + "b".repeat(32))).toBe(
      "host-" + "b".repeat(32),
    );
    expect(sharedRouteId("0f3a9c")).toBe("shared-0f3a9c");
    expect(shareKeyOfRouteId("shared-0f3a9c")).toBe("0f3a9c");
    expect(shareKeyOfRouteId(key)).toBe(key);
  });

  it("names the owner by display name, then email, then a placeholder", () => {
    expect(ownerLabel(sharedWorkspace())).toBe("owner@acme.example");
    expect(
      ownerLabel(
        sharedWorkspace({
          owner: {
            user_id: "o",
            email: "e@x",
            display_name: "Owen",
            profile_picture_url: null,
          },
        }),
      ),
    ).toBe("Owen");
    expect(
      ownerLabel(
        sharedWorkspace({
          owner: {
            user_id: "o",
            email: null,
            display_name: null,
            profile_picture_url: null,
          },
        }),
      ),
    ).toBe("someone");
  });

  it("reads the status, the verdict, and the liveness for the tile", () => {
    expect(statusLabel(sharedWorkspace())).toBe("Invitation");
    expect(statusLabel(sharedWorkspace({ status: "accepted" }))).toBe("Accepted");
    expect(statusLabel(sharedWorkspace({ status: "declined" }))).toBe("Declined");
    expect(statusLabel(sharedWorkspace({ status: "something-new" }))).toBe("Shared");
    expect(statusLabel(sharedWorkspace({ is_manual: true }))).toBe("Pasted link");
    expect(verdictLabel(sharedWorkspace())).toBeNull();
    expect(verdictLabel(sharedWorkspace({ verdict: "admitted" }))).toBeNull();
    expect(verdictLabel(sharedWorkspace({ verdict: "refused" }))).toBe(
      "No longer shared with you",
    );
    expect(verdictLabel(sharedWorkspace({ verdict: "misconfigured" }))).toBe(
      "The owner's sharing is misconfigured",
    );
    expect(verdictLabel(sharedWorkspace({ verdict: "offline" }))).toBe(
      "Could not reach the workspace",
    );
    const now = Date.parse("2026-11-04T10:00:00+00:00");
    expect(isShareLive(sharedWorkspace(), now)).toBeNull();
    expect(
      isShareLive(sharedWorkspace({ last_seen_at: "2026-11-04T09:50:00+00:00" }), now),
    ).toBe(true);
    expect(
      isShareLive(sharedWorkspace({ last_seen_at: "2026-11-04T08:00:00+00:00" }), now),
    ).toBe(false);
    expect(isShareLive(sharedWorkspace({ last_seen_at: "not a time" }), now)).toBeNull();
  });

  it("opens the shell first, else the first target with a link, else nothing", () => {
    expect(primaryLink(sharedWorkspace())).toBe(`https://shell-1.${DOMAIN}/`);
    const appOnly = sharedWorkspace({
      targets: [
        {
          app: "notes",
          display_name: "Notes",
          link: `https://notes-1.${DOMAIN}/`,
          granted_via: "user",
        },
      ],
    });
    expect(primaryLink(appOnly)).toBe(`https://notes-1.${DOMAIN}/`);
    expect(
      primaryLink(
        sharedWorkspace({
          targets: [
            {
              app: "system_interface",
              display_name: null,
              link: null,
              granted_via: "user",
            },
          ],
        }),
      ),
    ).toBeNull();
  });

  it("matches a frame host to the share whose domain it is under", () => {
    const shared = sharedWorkspaceEntry();
    const owned = entry({ id: "agent-" + "2".repeat(32) });
    expect(isHostOfShare(`shell-1.${DOMAIN}`, sharedWorkspace())).toBe(true);
    expect(isHostOfShare(DOMAIN.toUpperCase(), sharedWorkspace())).toBe(true);
    expect(isHostOfShare(`evil-${DOMAIN}`, sharedWorkspace())).toBe(false);
    expect(sharedEntryForHost([owned, shared], `notes-1.${DOMAIN}`)).toBe(
      shared,
    );
    expect(
      sharedEntryForHost([owned, shared], "agent-abc.localhost"),
    ).toBeNull();
  });
});

describe("sharedWithMeApi", () => {
  it("sends the account on every call and parses the open plan", async () => {
    const calls: { url: string; init: RequestInit | undefined }[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response(
        JSON.stringify({
          share_key: "k",
          host_id: "h",
          link: "https://x/",
          redeem_url: null,
          status: "accepted",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    const plan = await sharedWithMeApi.open("u", "k", "notes");

    expect(plan.link).toBe("https://x/");
    expect(calls[0].url).toBe("/ui/api/shared-with-me/k/open?account=u");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.body).toBe(JSON.stringify({ app: "notes" }));
  });

  it("raises a typed error carrying the desktop's code on a refusal", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({ error: "frozen", code: "already_reported" }),
          { status: 409 },
        ),
    );

    await expect(sharedWithMeApi.decline("u", "k")).rejects.toMatchObject({
      code: "already_reported",
      status: 409,
    });
    await expect(sharedWithMeApi.decline("u", "k")).rejects.toBeInstanceOf(
      SharedWithMeError,
    );
  });

  it("names the refusals a code stands for, and otherwise shows the error's own message", () => {
    expect(sharedActionErrorMessage(new SharedWithMeError("x", "already_reported", 409))).toBe(
      "You reported this share; a report cannot be undone.",
    );
    expect(sharedActionErrorMessage(new SharedWithMeError("x", "rate_limited", 429))).toBe(
      "Too many reports today; try again tomorrow.",
    );
    expect(sharedActionErrorMessage(new SharedWithMeError("no address yet", "no_link", 409))).toBe(
      "no address yet",
    );
    expect(sharedActionErrorMessage(new Error("offline"))).toBe("offline");
    expect(sharedActionErrorMessage("plain")).toBe("plain");
  });
});

describe("the home page's reading of a shared row", () => {
  const appTarget = { app: "notes", display_name: "Notes", link: "https://notes-1.ws.example/", granted_via: "user" };

  it("reads a share with a shell target as a workspace and one without as apps", () => {
    expect(isWorkspaceShare(sharedWorkspace())).toBe(true);
    expect(isWorkspaceShare(sharedWorkspace({ targets: [appTarget] }))).toBe(false);
  });

  it("reads a pasted link by its label: the shell's label is a workspace, any other an app named without its suffix", () => {
    expect(pastedLinkLabel("https://system-interface-k3j9x2ab.ws.example/x")).toBe("system-interface-k3j9x2ab");
    expect(pastedLinkLabel("not a link")).toBeNull();
    expect(isShellLabel("system-interface-k3j9x2ab")).toBe(true);
    expect(isShellLabel("system_interface-k3j9x2ab")).toBe(true);
    expect(isShellLabel("reading-list-k3j9x2ab")).toBe(false);
    expect(pastedLinkAppName("reading-list-k3j9x2ab")).toBe("reading-list");
    expect(pastedLinkAppName("short")).toBe("short");
    const shellLink = sharedWorkspace({
      is_manual: true,
      targets: [{ ...appTarget, app: "system_interface", link: "https://system_interface-aaaaaaaa.ws.example/" }],
    });
    const appLink = sharedWorkspace({
      is_manual: true,
      targets: [{ ...appTarget, app: "system_interface", link: "https://reading-list-k3j9x2ab.ws.example/" }],
    });
    expect(isWorkspaceShare(shellLink)).toBe(true);
    expect(isWorkspaceShare(appLink)).toBe(false);
    const [app] = sharedAppsOf(sharedWorkspaceEntry({ ...appLink }));
    expect(app.name).toBe("reading-list");
  });

  it("sorts a listed share by its status into invitations, declined ones, and the rest; a pasted link is never one", () => {
    expect(isInvitation(sharedWorkspace())).toBe(true);
    expect(isDeclinedInvitation(sharedWorkspace({ status: "declined" }))).toBe(true);
    expect(isInvitation(sharedWorkspace({ status: "accepted" }))).toBe(false);
    expect(isInvitation(sharedWorkspace({ is_manual: true }))).toBe(false);
    expect(isDeclinedInvitation(sharedWorkspace({ is_manual: true, status: "declined" }))).toBe(false);
  });

  it("lists an app share's targets as apps named by their display name, else their app name", () => {
    const entry = sharedWorkspaceEntry({ targets: [appTarget, { ...appTarget, app: "crm", display_name: null, link: null }] });
    expect(sharedAppsOf(entry).map((app) => [app.name, app.target.app])).toEqual([
      ["Notes", "notes"],
      ["crm", "crm"],
    ]);
  });

  it("lists, on a workspace share, only the apps a grant names in their own right, never the shell", () => {
    const shell = sharedWorkspace().targets[0];
    const covered = { ...appTarget, app: "crm", display_name: "CRM", is_granted_as_app: false };
    const granted = { ...appTarget, is_granted_as_app: true };
    expect(sharedAppsOf(sharedWorkspaceEntry({ targets: [shell, covered] }))).toEqual([]);
    const apps = sharedAppsOf(sharedWorkspaceEntry({ targets: [shell, covered, granted] }));
    expect(apps.map((app) => [app.name, app.target.app])).toEqual([["Notes", "notes"]]);
    expect(isWorkspaceShare(sharedWorkspaceEntry({ targets: [shell, granted] }).share!)).toBe(true);
  });

  it("opens the shell target as the whole workspace and any other by its app, and offers Retry offline or after a verdict", () => {
    expect(openTargetApp({ ...appTarget, app: "system_interface" })).toBeNull();
    expect(openTargetApp(appTarget)).toBe("notes");
    expect(isRetryOffered(sharedWorkspace(), false)).toBe(false);
    expect(isRetryOffered(sharedWorkspace(), true)).toBe(true);
    expect(isRetryOffered(sharedWorkspace({ verdict: "admitted" }), false)).toBe(false);
    expect(isRetryOffered(sharedWorkspace({ verdict: "refused" }), false)).toBe(true);
  });

  it("opens a share by its shell when linked, else its first linked target, else its first target", () => {
    expect(primaryTarget(sharedWorkspace())?.app).toBe("system_interface");
    const unlinkedShell = sharedWorkspace({
      targets: [{ ...appTarget, app: "system_interface", link: null }, appTarget],
    });
    expect(primaryTarget(unlinkedShell)?.app).toBe("notes");
    expect(primaryTarget(sharedWorkspace({ targets: [{ ...appTarget, link: null }] }))?.app).toBe("notes");
    expect(primaryTarget(sharedWorkspace({ targets: [] }))).toBeNull();
  });

  it("sorts the shared rows onto the home page's tabs, invitations newest first, owned rows left out", () => {
    const keyOf = (digit: string) => "agent-" + digit.repeat(32);
    const share = (digit: string, overrides: Partial<UiSharedWorkspace>) =>
      sharedWorkspaceEntry({ share_key: keyOf(digit), ...overrides });
    const rows = homeSharedRows(
      [
        workspacesMessage().workspaces[0],
        share("1", { latest_grant_at: "2026-11-01T09:00:00+00:00" }),
        share("3", { status: "declined" }),
        share("5", { status: "accepted" }),
        share("2", { latest_grant_at: "2026-11-05T09:00:00+00:00" }),
        share("4", { status: "declined" }),
        share("6", { status: "accepted", targets: [appTarget] }),
      ],
      new Set([keyOf("3")]),
    );
    const keysOf = (entries: readonly { share: { share_key: string } }[]) => entries.map((entry) => entry.share.share_key);

    expect(keysOf(rows.invitations)).toEqual([keyOf("2"), keyOf("1")]);
    expect(keysOf(rows.pendingDeclined)).toEqual([keyOf("3")]);
    expect(keysOf(rows.settledDeclined)).toEqual([keyOf("4")]);
    expect(keysOf(rows.workspaceShares)).toEqual([keyOf("5")]);
    expect(rows.sharedApps.map((app) => [app.share.share_key, app.name])).toEqual([[keyOf("6"), "Notes"]]);
  });

  it("labels a time relative to now, down to the day, and nothing for a missing one", () => {
    const now = Date.parse("2026-11-11T12:00:00Z");
    expect(relativeTimeLabel("2026-11-11T11:59:40Z", now)).toBe("just now");
    expect(relativeTimeLabel("2026-11-11T11:35:00Z", now)).toBe("25m ago");
    expect(relativeTimeLabel("2026-11-11T09:00:00Z", now)).toBe("3h ago");
    expect(relativeTimeLabel("2026-11-08T12:00:00Z", now)).toBe("3d ago");
    expect(relativeTimeLabel("2026-10-01T12:00:00Z", now)).toBe(new Date(Date.parse("2026-10-01T12:00:00Z")).toLocaleDateString());
    expect(relativeTimeLabel("", now)).toBe("");
    expect(relativeTimeLabel("nonsense", now)).toBe("");
  });
});
