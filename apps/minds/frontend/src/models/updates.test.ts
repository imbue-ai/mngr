import { afterEach, describe, expect, it, vi } from "vitest";
import type { UiWorkspaceUpdatesMessage } from "../channel/messages";
import {
  UpdatesStore,
  devOverridePrefill,
  isRunInFlight,
  isUpdateDispatchable,
  isUpdateOffered,
  labelVersionNote,
  standingUpdateNotice,
  updateActivityNotice,
  updateBadgeFor,
  updateRunOutcome,
} from "./updates";

vi.mock("mithril", () => ({ default: { redraw: () => undefined } }));

afterEach(() => {
  vi.unstubAllGlobals();
});

function message(
  updates: UiWorkspaceUpdatesMessage["updates"],
): UiWorkspaceUpdatesMessage {
  return {
    type: "workspace_updates",
    updates,
    update_window: "2:00 AM-5:00 AM",
  };
}

const OUT_OF_DATE = {
  availability: "OUT_OF_DATE" as const,
  current_version: "minds-v0.3.9",
  supported_version: "minds-v0.4.1",
  is_version_from_label: false,
  activity: "IDLE" as const,
};

describe("UpdatesStore", () => {
  it("reports a machine it has heard nothing about as unknown, never as up to date", () => {
    const store = new UpdatesStore();
    expect(store.forAgent("agent-never-seen").availability).toBe("UNKNOWN");
  });

  it("locks the row the moment an update is dispatched, before any state is pushed", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));

    const pending = store.updateNow("agent-a");
    expect(store.isUpdating("agent-a")).toBe(true);
    // The backend publishes the in-flight row well before the request answers.
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE, activity: "STARTING" } }));
    await pending;
    expect(store.isUpdating("agent-a")).toBe(true);
  });

  it("does not hold the optimistic lock past the dispatch it was taken for", async () => {
    // Otherwise a run that finished while the socket was down latches the row
    // on "Updating…" for the rest of the session.
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));

    await store.updateNow("agent-a");
    store.reset();
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE, activity: "IDLE" } }));

    expect(store.isUpdating("agent-a")).toBe(false);
  });

  it("unlocks the row when the dispatch is refused, so nothing is left locked with nothing running", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE }));
    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response(JSON.stringify({ error: "nope" }), { status: 409 })),
    );

    const result = await store.updateNow("agent-a");

    expect(result.isOk).toBe(false);
    expect(store.isUpdating("agent-a")).toBe(false);
  });

  it("hands the optimistic lock over to the pushed state once a run is really in flight", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));
    await store.updateNow("agent-a");

    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE, activity: "RUNNING" } }));

    expect(store.isUpdating("agent-a")).toBe(true);
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE, activity: "IDLE" } }));
    expect(store.isUpdating("agent-a")).toBe(false);
  });

  it("asks the no-backup confirmation only on a positive published reading", () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(
      message({
        "agent-unbacked": { ...OUT_OF_DATE, is_backup_configured: false },
        "agent-backed": { ...OUT_OF_DATE, is_backup_configured: true },
      }),
    );

    expect(store.needsNoBackupConfirmation("agent-unbacked")).toBe(true);
    expect(store.needsNoBackupConfirmation("agent-backed")).toBe(false);
    expect(store.needsNoBackupConfirmation("agent-unpublished")).toBe(false);
  });

  it("covers only confirmed out-of-date machines in a bulk action", () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(
      message({
        "agent-stale": OUT_OF_DATE,
        "agent-unknown": { ...OUT_OF_DATE, availability: "UNKNOWN" },
        "agent-current": { ...OUT_OF_DATE, availability: "UP_TO_DATE" },
        "agent-running": { ...OUT_OF_DATE, activity: "RUNNING" },
      }),
    );

    expect(store.updatableAgentIds()).toEqual(["agent-stale"]);
  });

  it("reports a machine mid-apply so the health surfaces can defer to it", () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE, activity: "APPLYING" } }));

    expect(store.isApplying("agent-a")).toBe(true);
    expect(store.isApplying("agent-b")).toBe(false);
  });

  it("leaves a bulk-dispatched machine unlocked until the backend says a run started", async () => {
    // The bulk gate silently skips some machines, so a lock taken on the press
    // could never be released for them.
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-skipped": OUT_OF_DATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));

    await store.updateAllNow(["agent-skipped"]);

    expect(store.isUpdating("agent-skipped")).toBe(false);
    store.applyUpdatesMessage(message({ "agent-skipped": { ...OUT_OF_DATE, activity: "STARTING" } }));
    expect(store.isUpdating("agent-skipped")).toBe(true);
  });

  it("keeps a dispatch this window made across a reconnect", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));
    const dispatch = store.updateNow("agent-a");

    store.reset();

    expect(store.isUpdating("agent-a")).toBe(true);
    expect((await dispatch).isOk).toBe(true);
  });
});

describe("update state predicates", () => {
  it("counts every live phase of a run as in flight, waiting included", () => {
    for (const activity of ["STARTING", "RUNNING", "WAITING", "APPLYING"] as const) {
      expect(isRunInFlight({ ...OUT_OF_DATE, activity })).toBe(true);
    }
    for (const activity of ["IDLE", "STALLED"] as const) {
      expect(isRunInFlight({ ...OUT_OF_DATE, activity })).toBe(false);
    }
  });

  it("claims a machine has an update available only when it has read that it does", () => {
    expect(isUpdateOffered(OUT_OF_DATE)).toBe(true);
    for (const availability of ["UP_TO_DATE", "UNKNOWN", "APP_BEHIND"] as const) {
      expect(isUpdateOffered({ ...OUT_OF_DATE, availability })).toBe(false);
    }
  });

  it("stops saying a machine is behind once its run is in flight", () => {
    expect(standingUpdateNotice(OUT_OF_DATE, false)).toBe("out-of-date");
    expect(standingUpdateNotice(OUT_OF_DATE, true)).toBe("none");
    expect(standingUpdateNotice({ ...OUT_OF_DATE, availability: "UNKNOWN" }, false)).toBe("none");
  });

  it("keeps saying a machine needs recreating: no run can silence a standing fact about it", () => {
    const tooOld = { ...OUT_OF_DATE, availability: "NEEDS_RECREATION" as const };
    expect(standingUpdateNotice(tooOld, false)).toBe("needs-recreation");
    expect(standingUpdateNotice(tooOld, true)).toBe("needs-recreation");
  });

  it("will still send the update agent into a machine it could not read", () => {
    // From unknown, the offer is the check: the agent reads the machine's own upstream.
    expect(isUpdateDispatchable({ ...OUT_OF_DATE, availability: "UNKNOWN" })).toBe(true);
    expect(isUpdateDispatchable(OUT_OF_DATE)).toBe(true);
    for (const availability of ["UP_TO_DATE", "APP_BEHIND", "NEEDS_RECREATION"] as const) {
      expect(isUpdateDispatchable({ ...OUT_OF_DATE, availability })).toBe(false);
    }
  });

  it("reports every way a run can end short as one outcome: check in with the agent", () => {
    for (const verdict of ["STUCK", "REFUSED", "NEEDS_RECREATION"] as const) {
      expect(updateRunOutcome({ ...OUT_OF_DATE, verdict })).toBe("failed");
    }
    expect(updateRunOutcome({ ...OUT_OF_DATE, activity: "STALLED" })).toBe("failed");
  });
});

describe("updateBadgeFor", () => {
  const UNKNOWN = {
    ...OUT_OF_DATE,
    availability: "UNKNOWN" as const,
    current_version: "",
    unknown_reason: "NO_MACHINE_VERSION" as const,
  };

  it("says the machine is unreadable only when the machine is the side with no version", () => {
    const devBuild = updateBadgeFor(
      { ...UNKNOWN, current_version: "minds-v0.3.9", unknown_reason: "NO_APP_VERSION" },
      false,
    );
    expect(devBuild?.label).toBe("Version unknown");
    expect(devBuild?.tooltip).not.toContain("this machine");

    const unreadable = updateBadgeFor(UNKNOWN, false);
    expect(unreadable?.tooltip).toContain("this machine");
  });

  it("shows the run in flight ahead of everything the detection sweep says", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE, activity: "APPLYING" }, true);
    expect(badge).toMatchObject({ state: "updating", label: "Updating…", isSpinnerShown: true });
  });

  it("tells the prepare phase from the apply, which are the two things a run does", () => {
    const preparing = updateBadgeFor({ ...OUT_OF_DATE, activity: "RUNNING" }, true);
    const applying = updateBadgeFor({ ...OUT_OF_DATE, activity: "APPLYING" }, true);
    expect(preparing?.label).toBe("Preparing update…");
    expect(applying?.label).toBe("Updating…");
  });

  it("says a recorded hold is waiting on a decision, and leads with the run's own line", () => {
    const held = {
      ...OUT_OF_DATE,
      activity: "WAITING" as const,
      chat_agent_name: "update-1",
      is_hold_recorded: true,
      hold_detail: "Your dashboard widget and the new layout both changed the same file.",
    };
    const badge = updateBadgeFor(held, true);
    expect(badge?.state).toBe("waiting");
    expect(badge?.tooltip).toContain("waiting for your decision");
    const notice = updateActivityNotice(held);
    expect(notice.message.startsWith("Your dashboard widget and the new layout both changed the same file.")).toBe(true);
    expect(notice.message).toContain("waiting for your decision");
    expect(notice.message).toContain("in the update-1 tab");
    expect(notice.isWaiting).toBe(false);
  });

  it("says a waiting run is the reader's move, not a spinner", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE, activity: "WAITING" }, true);
    expect(badge).toMatchObject({ state: "waiting", tone: "warn", label: "Waiting for you", isSpinnerShown: false });
  });

  it("keeps a failed run visible over a machine that is still out of date", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE, verdict: "STUCK" }, false);
    expect(badge).toMatchObject({ state: "failed", tone: "error", label: "Update failed" });
  });

  it("badges a machine below the in-place cutoff as needing recreation, not as an update offer", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE, availability: "NEEDS_RECREATION", is_scheduled: true }, false);
    expect(badge).toMatchObject({ state: "needs-recreation", tone: "warn", label: "Recreate to update" });
  });

  it("distinguishes an armed schedule from a standing offer", () => {
    expect(updateBadgeFor(OUT_OF_DATE, false)?.state).toBe("out-of-date");
    expect(updateBadgeFor({ ...OUT_OF_DATE, is_scheduled: true }, false)?.state).toBe("scheduled");
  });

  it("shows nothing for a machine that is simply up to date", () => {
    expect(updateBadgeFor({ ...OUT_OF_DATE, availability: "UP_TO_DATE" }, false)).toBeNull();
  });

  it("still badges an up-to-date machine that has an update armed at a version the user named", () => {
    // The schedule survives the up-to-date reading, and the badge is the row's
    // only way into the modal that can cancel it.
    const badge = updateBadgeFor({ ...OUT_OF_DATE, availability: "UP_TO_DATE", is_scheduled: true }, false);
    expect(badge?.state).toBe("scheduled");
    expect(badge?.tone).toBe("neutral");
  });
});

describe("an update that landed with a note for the reader", () => {
  it("keeps a neutral badge of its own, since detection puts the machine back at up to date", () => {
    // Not a failure, and detection reads the machine as current again, so
    // nothing else would open the modal carrying the note.
    const badge = updateBadgeFor(
      { ...OUT_OF_DATE, availability: "UP_TO_DATE", verdict: "UPDATED_WITH_REBUILD_ITEMS" },
      false,
    );
    expect(badge).not.toBeNull();
    expect(badge?.tone).toBe("neutral");
    expect(badge?.state).toBe("needs-attention");
  });
});

describe("updateActivityNotice", () => {
  // STARTING can last minutes for a stopped machine, and its chat tab does
  // not exist yet.
  it("waits, without naming a tab, while the agent is still being created", () => {
    const notice = updateActivityNotice({ ...OUT_OF_DATE, activity: "STARTING", chat_agent_name: "update-fox" });
    expect(notice.isWaiting).toBe(true);
    expect(notice.message).not.toContain("update-fox");
  });

  it("names the tab to open once the agent is actually there", () => {
    const notice = updateActivityNotice({ ...OUT_OF_DATE, activity: "RUNNING", chat_agent_name: "update-fox" });
    expect(notice.isWaiting).toBe(false);
    expect(notice.message).toContain("Open the update-fox tab");
  });

  it("still says where to look for a run whose chat it cannot name", () => {
    const notice = updateActivityNotice({ ...OUT_OF_DATE, activity: "RUNNING" });
    expect(notice.message).toContain("chat tab");
  });

  it("names the version a run was given an explicit target for", () => {
    const running = updateActivityNotice({
      ...OUT_OF_DATE,
      activity: "RUNNING",
      chat_agent_name: "update-fox",
      target_override: "main",
    });
    expect(running.message).toContain("Preparing the update to main");
    const applying = updateActivityNotice({ ...OUT_OF_DATE, activity: "APPLYING", target_override: "main" });
    expect(applying.message).toContain("to main");
  });

  it("says nothing at all for a machine with no run in flight", () => {
    expect(updateActivityNotice(OUT_OF_DATE).message).toBe("");
  });

  it("waits from the press, not from the first frame that agrees", () => {
    // The modal drops its buttons the moment the row locks, so the press itself
    // must already show something.
    expect(updateActivityNotice(OUT_OF_DATE, true)).toEqual(updateActivityNotice({ ...OUT_OF_DATE, activity: "STARTING" }));
  });

  it("lets the pushed state win once it has something more specific to say", () => {
    const applying = updateActivityNotice({ ...OUT_OF_DATE, activity: "APPLYING" }, true);
    expect(applying.message).toContain("services restart");
    expect(applying.isWaiting).toBe(false);
  });
});

describe("labelVersionNote", () => {
  // A running machine that has never updated itself also falls back to the
  // label, and there the label is exactly its version.
  it("offers to start only a machine that is actually stopped", () => {
    expect(labelVersionNote("STOPPED")).toContain("start it");
  });

  it("says nothing over a running machine, whose label is the answer", () => {
    for (const liveness of ["RUNNING", "STARTING", "STOPPING", "UNKNOWN", "", undefined]) {
      expect(labelVersionNote(liveness)).toBeNull();
    }
  });
});

describe("updateBadgeFor before the backend has read a machine", () => {
  // The map is empty for the first seconds after launch; badging "Version
  // unknown" there states a verdict nobody reached.
  it("says nothing about a machine the backend has not published a reading for", () => {
    const store = new UpdatesStore();
    expect(store.publishedFor("agent-never-seen")).toBeNull();
    expect(updateBadgeFor(store.publishedFor("agent-never-seen"), false)).toBeNull();
  });

  it("still locks the row on a dispatch this window just made", () => {
    expect(updateBadgeFor(null, true)).toMatchObject({ state: "updating", label: "Preparing update…" });
  });
});

describe("choosing a specific version", () => {
  it("sends the chosen ref with the dispatch", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE }));
    const bodies: unknown[] = [];
    vi.stubGlobal("fetch", (_url: string, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)));
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    await store.updateNow("agent-a", "minds-v0.5.0");

    expect(bodies[0]).toMatchObject({ target_ref: "minds-v0.5.0" });
  });

  it("prefills only for a build pinned to a branch, addressed the way the agent's fetch sees it", () => {
    // A dev build's branch only resolves in the workspace as upstream/<branch>
    // (main is understood bare).
    expect(devOverridePrefill("minds-v0.4.1")).toBe("");
    expect(devOverridePrefill("")).toBe("");
    expect(devOverridePrefill("main")).toBe("main");
    expect(devOverridePrefill("gabriel/some-branch")).toBe("upstream/gabriel/some-branch");
  });
});
