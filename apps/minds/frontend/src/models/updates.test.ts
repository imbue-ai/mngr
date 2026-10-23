import { afterEach, describe, expect, it, vi } from "vitest";
import type { UiWorkspaceUpdate, UiWorkspaceUpdatesMessage } from "../channel/messages";
import {
  UpdatesStore,
  devOverridePrefill,
  displayVersion,
  isFailureVerdict,
  isRunInFlight,
  isUpdateDispatchable,
  isUpdateOffered,
  scheduleSkipLine,
  plainScheduleTargetRef,
  scheduledLine,
  standingUpdateNotice,
  updateActivityNotice,
  updateBadgeFor,
  updateRunOutcome,
} from "./updates";
import { OUT_OF_DATE_UPDATE } from "../testing";

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

describe("UpdatesStore", () => {
  it("reports a machine it has heard nothing about as unknown, never as up to date", () => {
    const store = new UpdatesStore();
    expect(store.forAgent("agent-never-seen").availability).toBe("UNKNOWN");
  });

  it("locks the row the moment an update is dispatched, before any state is pushed", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));

    const pending = store.updateNow("agent-a");
    expect(store.isUpdating("agent-a")).toBe(true);
    // The backend publishes the in-flight row well before the request answers.
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "STARTING" } }));
    await pending;
    expect(store.isUpdating("agent-a")).toBe(true);
  });

  it("does not hold the optimistic lock past the dispatch it was taken for", async () => {
    // Otherwise a run that finished while the socket was down latches the row
    // on "Updating…" for the rest of the session.
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));

    await store.updateNow("agent-a");
    store.reset();
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "IDLE" } }));

    expect(store.isUpdating("agent-a")).toBe(false);
  });

  it("unlocks the row when the dispatch is refused, so nothing is left locked with nothing running", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response(JSON.stringify({ error: "nope" }), { status: 409 })),
    );

    const result = await store.updateNow("agent-a");

    expect(result.isOk).toBe(false);
    expect(store.isUpdating("agent-a")).toBe(false);
    // A refusal with nothing to quote reports no detail rather than undefined,
    // so nothing renders an empty block under it.
    expect(result.detail).toBe("");
  });

  it("carries the refusing machine's own words back to the caller", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            error: "Couldn't start the update agent in this workspace.",
            detail: "Error: Unknown fields in agent_types.opencode",
          }),
          { status: 502 },
        ),
      ),
    );

    const result = await store.updateNow("agent-a");

    expect(result.isOk).toBe(false);
    expect(result.detail).toBe("Error: Unknown fields in agent_types.opencode");
  });

  it("hands the optimistic lock over to the pushed state once a run is really in flight", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));
    await store.updateNow("agent-a");

    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "RUNNING" } }));

    expect(store.isUpdating("agent-a")).toBe(true);
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "IDLE" } }));
    expect(store.isUpdating("agent-a")).toBe(false);
  });

  it("asks the no-backup confirmation only on a positive published reading", () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(
      message({
        "agent-unbacked": { ...OUT_OF_DATE_UPDATE, is_backup_configured: false },
        "agent-backed": { ...OUT_OF_DATE_UPDATE, is_backup_configured: true },
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
        "agent-stale": OUT_OF_DATE_UPDATE,
        "agent-unknown": { ...OUT_OF_DATE_UPDATE, availability: "UNKNOWN" },
        "agent-current": { ...OUT_OF_DATE_UPDATE, availability: "UP_TO_DATE" },
        "agent-patch": { ...OUT_OF_DATE_UPDATE, availability: "PATCH_AVAILABLE" },
        "agent-running": { ...OUT_OF_DATE_UPDATE, activity: "RUNNING" },
        "agent-deciding": { ...OUT_OF_DATE_UPDATE, verdict: "NEEDS_DECISION", is_decision_pending: true },
        "agent-landed": { ...OUT_OF_DATE_UPDATE, verdict: "UPDATED", success_note_version: "minds-v0.4.1" },
      }),
    );

    expect(store.updatableAgentIds()).toEqual(["agent-stale"]);
  });

  it("reports a machine mid-apply so the health surfaces can defer to it", () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "APPLYING" } }));

    expect(store.isApplying("agent-a")).toBe(true);
    expect(store.isApplying("agent-b")).toBe(false);
  });

  it("leaves a bulk-dispatched machine unlocked until the backend says a run started", async () => {
    // The bulk gate silently skips some machines, so a lock taken on the press
    // could never be released for them.
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-skipped": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));

    await store.updateAllNow(["agent-skipped"]);

    expect(store.isUpdating("agent-skipped")).toBe(false);
    store.applyUpdatesMessage(message({ "agent-skipped": { ...OUT_OF_DATE_UPDATE, activity: "STARTING" } }));
    expect(store.isUpdating("agent-skipped")).toBe(true);
  });

  it("keeps a dispatch this window made across a reconnect", async () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 200 })));
    const dispatch = store.updateNow("agent-a");

    store.reset();

    expect(store.isUpdating("agent-a")).toBe(true);
    expect((await dispatch).isOk).toBe(true);
  });
});

describe("a stopped machine's update state", () => {
  function storeWith(liveness: Record<string, string>, updates: UiWorkspaceUpdatesMessage["updates"]): UpdatesStore {
    const store = new UpdatesStore((agentId) => liveness[agentId] ?? "");
    store.applyUpdatesMessage(message(updates));
    return store;
  }

  it.each(["STOPPED", "STOPPING"])(
    "shows nothing while %s that needs the machine to answer, and all of it again once it runs",
    (stoppedLiveness) => {
      const liveness: Record<string, string> = { "agent-a": stoppedLiveness };
      const store = storeWith(liveness, {
        "agent-a": {
          ...OUT_OF_DATE_UPDATE,
          verdict: "NEEDS_DECISION",
          is_decision_pending: true,
          is_scheduled: true,
          needs_sign_in: true,
          chat_agent_id: "agent-chat",
          success_note_version: "minds-v0.4.0",
        },
      });

      expect(store.publishedFor("agent-a")).toBeNull();
      expect(updateBadgeFor(store.publishedFor("agent-a"), store.isUpdating("agent-a"))).toBeNull();
      expect(standingUpdateNotice(store.forAgent("agent-a"), false)).toBe("none");
      expect(store.updatableAgentIds()).toEqual([]);

      liveness["agent-a"] = "RUNNING";
      expect(updateBadgeFor(store.publishedFor("agent-a"), false)?.state).toBe("needs-decision");
    },
  );

  it("hides a run that reads as in progress, since nothing can be running in a stopped machine", () => {
    const store = storeWith({ "agent-a": "STOPPED" }, { "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "RUNNING" } });
    expect(store.isUpdating("agent-a")).toBe(false);
  });

  it("keeps a landing apply whole, whose restart can read as a stop", () => {
    const applying = { ...OUT_OF_DATE_UPDATE, activity: "APPLYING" as const, chat_agent_id: "agent-chat" };
    const store = storeWith({ "agent-a": "STOPPING" }, { "agent-a": applying });
    expect(store.publishedFor("agent-a")).toEqual(applying);
    expect(store.isApplying("agent-a")).toBe(true);
  });

  it("keeps the facts that need nothing from the machine: an update that never went out, and recreation", () => {
    const store = storeWith(
      { "agent-a": "STOPPED", "agent-b": "STOPPED" },
      {
        "agent-a": { ...OUT_OF_DATE_UPDATE, dispatch_failure: "Couldn't start this workspace.", verdict: "STUCK" },
        "agent-b": { ...OUT_OF_DATE_UPDATE, availability: "NEEDS_RECREATION", is_scheduled: true },
      },
    );

    expect(updateBadgeFor(store.publishedFor("agent-a"), false)).toMatchObject({
      state: "failed",
      tooltip: "Couldn't start this workspace.",
    });
    expect(store.publishedFor("agent-a")?.verdict).toBeUndefined();
    expect(updateBadgeFor(store.publishedFor("agent-b"), false)?.state).toBe("needs-recreation");
    expect(store.publishedFor("agent-b")?.is_scheduled).toBeUndefined();
  });

  it("shows a press on a stopped machine as updating while the press starts it, past the claim's push", () => {
    const store = storeWith({ "agent-a": "STOPPED" }, { "agent-a": OUT_OF_DATE_UPDATE });
    vi.stubGlobal("fetch", () => new Promise<Response>(() => undefined));
    void store.updateNow("agent-a");
    expect(updateBadgeFor(store.publishedFor("agent-a"), store.isUpdating("agent-a"))?.state).toBe("updating");

    // The claim's push releases the press's optimistic hold before the machine reads as started.
    store.applyUpdatesMessage(message({ "agent-a": { ...OUT_OF_DATE_UPDATE, activity: "STARTING" } }));
    expect(store.isQuietWhileStopped("agent-a")).toBe(false);
    expect(updateBadgeFor(store.publishedFor("agent-a"), store.isUpdating("agent-a"))?.state).toBe("updating");
  });
});

describe("update state predicates", () => {
  it("counts every live phase of a run as in flight, waiting included", () => {
    for (const activity of ["STARTING", "RUNNING", "WAITING", "APPLYING"] as const) {
      expect(isRunInFlight({ ...OUT_OF_DATE_UPDATE, activity })).toBe(true);
    }
    for (const activity of ["IDLE", "STALLED"] as const) {
      expect(isRunInFlight({ ...OUT_OF_DATE_UPDATE, activity })).toBe(false);
    }
  });

  it("claims a machine has an update available only when it has read that it does", () => {
    expect(isUpdateOffered(OUT_OF_DATE_UPDATE)).toBe(true);
    for (const availability of ["UP_TO_DATE", "UNKNOWN", "APP_BEHIND"] as const) {
      expect(isUpdateOffered({ ...OUT_OF_DATE_UPDATE, availability })).toBe(false);
    }
  });

  it("stops saying a machine is behind once its run is in flight", () => {
    expect(standingUpdateNotice(OUT_OF_DATE_UPDATE, false)).toBe("out-of-date");
    expect(standingUpdateNotice(OUT_OF_DATE_UPDATE, true)).toBe("none");
    expect(standingUpdateNotice({ ...OUT_OF_DATE_UPDATE, availability: "UNKNOWN" }, false)).toBe("none");
  });

  it("keeps saying a machine needs recreating: no run can silence a standing fact about it", () => {
    const tooOld = { ...OUT_OF_DATE_UPDATE, availability: "NEEDS_RECREATION" as const };
    expect(standingUpdateNotice(tooOld, false)).toBe("needs-recreation");
    expect(standingUpdateNotice(tooOld, true)).toBe("needs-recreation");
  });

  it("never prompts for a patch, but still lets the user take it", () => {
    const patch = { ...OUT_OF_DATE_UPDATE, availability: "PATCH_AVAILABLE" as const };
    expect(isUpdateOffered(patch)).toBe(false);
    expect(standingUpdateNotice(patch, false)).toBe("none");
    expect(isUpdateDispatchable(patch)).toBe(true);
  });

  it("will still send the update agent into a machine it could not read", () => {
    // From unknown, the offer is the check: the agent reads the machine's own upstream.
    expect(isUpdateDispatchable({ ...OUT_OF_DATE_UPDATE, availability: "UNKNOWN" })).toBe(true);
    expect(isUpdateDispatchable(OUT_OF_DATE_UPDATE)).toBe(true);
    for (const availability of ["UP_TO_DATE", "APP_BEHIND", "NEEDS_RECREATION"] as const) {
      expect(isUpdateDispatchable({ ...OUT_OF_DATE_UPDATE, availability })).toBe(false);
    }
  });

  it("reports every way a run can end short as one outcome: check in with the agent", () => {
    for (const verdict of ["STUCK", "REFUSED", "NEEDS_RECREATION"] as const) {
      expect(updateRunOutcome({ ...OUT_OF_DATE_UPDATE, verdict })).toBe("failed");
    }
    expect(updateRunOutcome({ ...OUT_OF_DATE_UPDATE, activity: "STALLED" })).toBe("failed");
  });

  it("reports an update that never went out ahead of an earlier run's outcome", () => {
    const update = { ...OUT_OF_DATE_UPDATE, verdict: "STUCK" as const, dispatch_failure: "Couldn't start this workspace." };
    expect(updateRunOutcome(update)).toBe("not-started");
  });
});

describe("updateBadgeFor", () => {
  const UNKNOWN = {
    ...OUT_OF_DATE_UPDATE,
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
    expect(devBuild?.tooltip).not.toContain("this workspace");

    const unreadable = updateBadgeFor(UNKNOWN, false);
    expect(unreadable?.tooltip).toContain("this workspace");
  });

  it("shows the run in flight ahead of everything the detection sweep says", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, activity: "APPLYING" }, true);
    expect(badge).toMatchObject({ state: "updating", label: "Updating…", isSpinnerShown: true });
  });

  it("tells the prepare phase from the apply, which are the two things a run does", () => {
    const preparing = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, activity: "RUNNING" }, true);
    const applying = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, activity: "APPLYING" }, true);
    expect(preparing?.label).toBe("Preparing update…");
    expect(applying?.label).toBe("Updating…");
  });

  it("says a recorded hold is waiting on a decision, and leads with the run's own line", () => {
    const held = {
      ...OUT_OF_DATE_UPDATE,
      activity: "WAITING" as const,
      is_hold_recorded: true,
      hold_detail: "Waiting for you to confirm minds-v0.9.1, which is newer than Imbue Studio.",
    };
    const badge = updateBadgeFor(held, true);
    expect(badge?.state).toBe("waiting");
    expect(badge?.tooltip).toContain("waiting for your go-ahead");
    const notice = updateActivityNotice(held);
    expect(notice.message.startsWith("Waiting for you to confirm minds-v0.9.1, which is newer than Imbue Studio.")).toBe(true);
    expect(notice.message).toContain("waiting for your go-ahead");
    expect(notice.isWaiting).toBe(false);
  });

  it("says a waiting run is the reader's move, not a spinner", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, activity: "WAITING" }, true);
    expect(badge).toMatchObject({ state: "waiting", tone: "warn", label: "Needs your answer", isSpinnerShown: false });
  });

  it("keeps a failed run visible over a machine that is still out of date", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, verdict: "STUCK" }, false);
    expect(badge).toMatchObject({ state: "failed", tone: "error", label: "Update failed" });
  });

  it("says an update that never went out didn't start, ahead of an earlier run's outcome", () => {
    const badge = updateBadgeFor(
      { ...OUT_OF_DATE_UPDATE, verdict: "STUCK", dispatch_failure: "Couldn't reach this workspace to start the update." },
      false,
    );
    expect(badge).toMatchObject({
      state: "failed",
      tone: "error",
      label: "Update didn't start",
      tooltip: "Couldn't reach this workspace to start the update.",
    });
  });

  it("badges a machine below the in-place cutoff as needing recreation, not as an update offer", () => {
    const badge = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, availability: "NEEDS_RECREATION", is_scheduled: true }, false);
    expect(badge).toMatchObject({ state: "needs-recreation", tone: "warn", label: "Recreate to update" });
  });

  it("distinguishes an armed schedule from a standing offer", () => {
    expect(updateBadgeFor(OUT_OF_DATE_UPDATE, false)?.state).toBe("out-of-date");
    expect(updateBadgeFor({ ...OUT_OF_DATE_UPDATE, is_scheduled: true }, false)?.state).toBe("scheduled");
  });

  it("shows nothing for a machine that is simply up to date", () => {
    expect(updateBadgeFor({ ...OUT_OF_DATE_UPDATE, availability: "UP_TO_DATE" }, false)).toBeNull();
  });

  it("shows nothing for a machine behind only by a patch, unless the user scheduled it", () => {
    const patch = { ...OUT_OF_DATE_UPDATE, availability: "PATCH_AVAILABLE" as const };
    expect(updateBadgeFor(patch, false)).toBeNull();
    const scheduled = updateBadgeFor({ ...patch, is_scheduled: true }, false);
    expect(scheduled?.state).toBe("scheduled");
    expect(scheduled?.tone).toBe("neutral");
  });

  it("still badges an up-to-date machine that has an update armed at a version the user named", () => {
    // The schedule survives the up-to-date reading, and the badge is the row's
    // only way into the modal that can cancel it.
    const badge = updateBadgeFor({ ...OUT_OF_DATE_UPDATE, availability: "UP_TO_DATE", is_scheduled: true }, false);
    expect(badge?.state).toBe("scheduled");
    expect(badge?.tone).toBe("neutral");
  });
});

describe("an update that landed with a note for the reader", () => {
  it("keeps a neutral badge of its own, since detection puts the machine back at up to date", () => {
    // Not a failure, and detection reads the machine as current again, so
    // nothing else would open the modal carrying the note.
    const badge = updateBadgeFor(
      { ...OUT_OF_DATE_UPDATE, availability: "UP_TO_DATE", verdict: "UPDATED_WITH_REBUILD_ITEMS" },
      false,
    );
    expect(badge).not.toBeNull();
    expect(badge?.tone).toBe("neutral");
    expect(badge?.state).toBe("needs-attention");
  });
});

describe("updateActivityNotice", () => {
  // STARTING can last minutes for a stopped machine, and its chat does not
  // exist yet.
  it("waits while the agent is still being created", () => {
    expect(updateActivityNotice({ ...OUT_OF_DATE_UPDATE, activity: "STARTING" }).isWaiting).toBe(true);
  });

  it.each(["RUNNING", "WAITING", "STALLED"] as const)(
    "never names a %s run's chat by its agent's internal name, which the chat list does not show",
    (activity) => {
      const notice = updateActivityNotice({ ...OUT_OF_DATE_UPDATE, activity, chat_agent_name: "Update-to-v041" });
      expect(notice.isWaiting).toBe(false);
      expect(notice.message).not.toContain("Update-to-v041");
    },
  );

  it("names the version a run was given an explicit target for", () => {
    const running = updateActivityNotice({
      ...OUT_OF_DATE_UPDATE,
      activity: "RUNNING",
      target_override: "main",
    });
    expect(running.message).toContain("Preparing the update to main");
    const applying = updateActivityNotice({ ...OUT_OF_DATE_UPDATE, activity: "APPLYING", target_override: "main" });
    expect(applying.message).toContain("to main");
  });

  it("says nothing at all for a machine with no run in flight", () => {
    expect(updateActivityNotice(OUT_OF_DATE_UPDATE).message).toBe("");
  });

  it("waits from the press, not from the first frame that agrees", () => {
    // The modal drops its buttons the moment the row locks, so the press itself
    // must already show something.
    expect(updateActivityNotice(OUT_OF_DATE_UPDATE, true)).toEqual(updateActivityNotice({ ...OUT_OF_DATE_UPDATE, activity: "STARTING" }));
  });

  it("lets the pushed state win once it has something more specific to say", () => {
    const applying = updateActivityNotice({ ...OUT_OF_DATE_UPDATE, activity: "APPLYING" }, true);
    expect(applying.message).toContain("services restart");
    expect(applying.isWaiting).toBe(false);
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
    store.applyUpdatesMessage(message({ "agent-a": OUT_OF_DATE_UPDATE }));
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

describe("displayVersion", () => {
  it("drops the tag prefix from a release and leaves every other ref as it is", () => {
    expect(displayVersion("minds-v0.8.2")).toBe("v0.8.2");
    expect(displayVersion("minds-v0.4.0-rc1")).toBe("v0.4.0-rc1");
    expect(displayVersion("main")).toBe("main");
    expect(displayVersion("upstream/gabriel/some-branch")).toBe("upstream/gabriel/some-branch");
    expect(displayVersion("minds-feature")).toBe("minds-feature");
    expect(displayVersion("")).toBe("");
  });
});

describe("auto-update's quiet surfaces", () => {
  it("says nothing about a behind machine auto-update will arm itself, on the row or the band", () => {
    // Behind and not armed yet: the next sweep arms it, and saying "Update
    // available" meanwhile would offer the user a job the app has taken on.
    expect(updateBadgeFor(OUT_OF_DATE_UPDATE, false, true)).toBeNull();
    expect(standingUpdateNotice(OUT_OF_DATE_UPDATE, false, true)).toBe("none");
    // The same machine without auto-update is an offer.
    expect(updateBadgeFor(OUT_OF_DATE_UPDATE, false, false)?.state).toBe("out-of-date");
    expect(standingUpdateNotice(OUT_OF_DATE_UPDATE, false, false)).toBe("out-of-date");
  });

  it("shows a machine it armed as scheduled, asking nothing of the reader, and one the user armed as without it", () => {
    const armed = { ...OUT_OF_DATE_UPDATE, is_scheduled: true };
    const badge = updateBadgeFor({ ...armed, is_schedule_auto_armed: true }, false, true);
    expect(badge).toMatchObject({ state: "scheduled", tone: "neutral", label: "Update scheduled" });
    expect(updateBadgeFor(armed, false, true)).toEqual(updateBadgeFor(armed, false, false));
    expect(updateBadgeFor(armed, false, true)?.tone).toBe("warn");
  });

  it.each([true, false])(
    "keeps a skipped release quiet, with a neutral badge into the modal (auto-update %s)",
    (isAutoUpdateEnabled) => {
      const skipped = { ...OUT_OF_DATE_UPDATE, is_update_skipped: true, needs_sign_in: true };
      expect(updateBadgeFor(skipped, false, isAutoUpdateEnabled)).toMatchObject({
        state: "skipped",
        tone: "neutral",
        label: "Skipped v0.4.1",
      });
      expect(standingUpdateNotice(skipped, false, isAutoUpdateEnabled)).toBe("none");
    },
  );

  it("still says a machine read behind earlier and stopped since is behind, since the sweep cannot arm it", () => {
    const stopped = { ...OUT_OF_DATE_UPDATE, is_workspace_readable: false };
    expect(updateBadgeFor(stopped, false, true)?.state).toBe("out-of-date");
    expect(standingUpdateNotice(stopped, false, true)).toBe("out-of-date");
  });

  it("says a machine that cannot be armed needs a sign-in", () => {
    const refused = { ...OUT_OF_DATE_UPDATE, needs_sign_in: true };
    const badge = updateBadgeFor(refused, false, true);
    expect(badge).toMatchObject({ state: "needs-sign-in", label: "Needs sign-in to auto-update" });
    expect(standingUpdateNotice(refused, false, true)).toBe("needs-sign-in");
    // A run in flight is the newer news.
    expect(standingUpdateNotice(refused, true, true)).toBe("none");
  });

  it("keeps the standing facts auto-update cannot fix: recreation and a landed update's note", () => {
    const tooOld = { ...OUT_OF_DATE_UPDATE, availability: "NEEDS_RECREATION" as const };
    expect(updateBadgeFor(tooOld, false, true)?.state).toBe("needs-recreation");
    expect(standingUpdateNotice(tooOld, false, true)).toBe("needs-recreation");
    const withNote = { ...OUT_OF_DATE_UPDATE, availability: "UP_TO_DATE" as const, verdict: "UPDATED_WITH_REBUILD_ITEMS" as const };
    expect(updateBadgeFor(withNote, false, true)?.state).toBe("needs-attention");
  });

  it("keeps a failed run visible under auto-update", () => {
    const failed = { ...OUT_OF_DATE_UPDATE, verdict: "STUCK" as const, chat_agent_id: "agent-chat" };
    expect(updateBadgeFor(failed, false, true)?.state).toBe("failed");
  });

  it("says why a machine the workspace would not let auto-update arm is still behind", () => {
    const refused = { ...OUT_OF_DATE_UPDATE, dispatch_failure: "This workspace can't run scheduled updates. Update it now instead." };
    expect(updateBadgeFor(refused, false, true)?.state).toBe("failed");
    expect(standingUpdateNotice(refused, false, true)).toBe("out-of-date");
  });
});

describe("a run that ended needing the user's decision", () => {
  const PENDING = {
    ...OUT_OF_DATE_UPDATE,
    verdict: "NEEDS_DECISION" as const,
    is_decision_pending: true,
    chat_agent_id: "agent-lead",
  };

  it("is its own state, not a failure", () => {
    expect(isFailureVerdict("NEEDS_DECISION")).toBe(false);
    expect(updateRunOutcome(PENDING)).toBe("needs-decision");
    expect(updateBadgeFor(PENDING, false)).toMatchObject({ state: "needs-decision", tone: "warn" });
  });

  it("outranks the machine still reading behind", () => {
    expect(updateBadgeFor(PENDING, false, true)?.state).toBe("needs-decision");
  });
});

describe("the schedule's lines", () => {
  /** A reader's clock at a fixed offset (minutes east of UTC). */
  function readerAt(utcOffsetMinutes: number): { now: Date; utcOffsetMinutesAt: () => number } {
    return { now: new Date("2026-10-20T12:00:00Z"), utcOffsetMinutesAt: () => utcOffsetMinutes };
  }
  // 1:00 AM-4:00 AM in Los Angeles under daylight saving (UTC-7).
  const PACIFIC_DAYLIGHT = readerAt(-7 * 60);
  const ARMED = {
    ...OUT_OF_DATE_UPDATE,
    is_scheduled: true,
    schedule_window_start_minute_utc: 8 * 60,
    schedule_window_length_minutes: 3 * 60,
  };

  it("names the window written into the workspace in the reader's own local time", () => {
    expect(scheduledLine(ARMED, "2:00 AM-5:00 AM", false, PACIFIC_DAYLIGHT)).toBe(
      "Scheduled to update in the next update window (1:00 AM-4:00 AM).",
    );
    // A half-hour zone east of UTC reads the same window in the afternoon.
    expect(scheduledLine(ARMED, "2:00 AM-5:00 AM", false, readerAt(5 * 60 + 30))).toContain("(1:30 PM-4:30 PM)");
    // A window that runs past the reader's midnight.
    expect(scheduledLine({ ...ARMED, schedule_window_start_minute_utc: 6 * 60 }, "", false, PACIFIC_DAYLIGHT)).toContain(
      "(11:00 PM-2:00 AM)",
    );
    // A schedule read with no window falls back to the app's.
    expect(
      scheduledLine({ ...ARMED, schedule_window_start_minute_utc: null }, "2:00 AM-5:00 AM", false, PACIFIC_DAYLIGHT),
    ).toContain("(2:00 AM-5:00 AM)");
  });

  it("reads the window at the offset of its next opening, as the app wrote it, across a daylight-saving change", () => {
    // Noon on the day before Los Angeles falls back: the next 2:00 AM is 10:00 UTC, past the change.
    const fallBack = new Date("2026-11-01T09:00:00Z");
    const reader = {
      now: new Date("2026-10-31T19:00:00Z"),
      utcOffsetMinutesAt: (instant: Date) => (instant < fallBack ? -7 * 60 : -8 * 60),
    };
    expect(scheduledLine({ ...ARMED, schedule_window_start_minute_utc: 10 * 60 }, "", false, reader)).toContain(
      "(2:00 AM-5:00 AM)",
    );
  });

  it("names a version the user typed, and says of auto-update's own schedule that nothing is asked", () => {
    const autoArmed = { ...ARMED, is_schedule_auto_armed: true };
    const line = scheduledLine({ ...autoArmed, override_ref: "minds-v0.9.1" }, "", true, PACIFIC_DAYLIGHT);
    expect(line).toContain("to v0.9.1");
    expect(line).toContain("No action is needed.");
    // A sign-in is asked of the reader, beside this line.
    expect(scheduledLine({ ...autoArmed, needs_sign_in: true }, "", true, PACIFIC_DAYLIGHT)).not.toContain(
      "No action is needed.",
    );
    // One the user armed is theirs, under auto-update too.
    expect(scheduledLine(ARMED, "", true, PACIFIC_DAYLIGHT)).toBe(
      "Scheduled to update in the next update window (1:00 AM-4:00 AM).",
    );
  });

  it.each([
    ["nothing is armed", { ...OUT_OF_DATE_UPDATE }, ""],
    ["the supported version is armed", { ...ARMED }, null],
    ["the supported version is armed by name", { ...ARMED, override_ref: "minds-v0.4.1" }, null],
    ["another version is armed", { ...ARMED, override_ref: "upstream/some-branch" }, "minds-v0.4.1"],
    [
      "another version is armed on a dev build",
      { ...ARMED, supported_version: "gabe/some-branch", override_ref: "main" },
      "upstream/gabe/some-branch",
    ],
    [
      "another version is armed on a dev build, by its branch",
      { ...ARMED, supported_version: "gabe/some-branch", override_ref: "upstream/gabe/some-branch" },
      null,
    ],
  ] as const)("names what the plain schedule sends when %s", (_case, update, expected) => {
    expect(plainScheduleTargetRef(update as UiWorkspaceUpdate)).toBe(expected);
  });

  it("adds the skip line only after a window passed without a start", () => {
    expect(scheduleSkipLine(ARMED)).toBe("");
    const skipped = scheduleSkipLine({
      ...ARMED,
      last_skip_reason: "No AI account was signed in to this workspace during the last update window.",
    });
    expect(skipped.startsWith("No AI account was signed in")).toBe(true);
    expect(skipped).toContain("try again in the next window");
  });
});

describe("UpdatesStore's auto-update and sign-in", () => {
  it("reads whether auto-update is on from every frame", () => {
    const store = new UpdatesStore();
    store.applyUpdatesMessage({ ...message({}), is_auto_update_enabled: true });
    expect(store.isAutoUpdateEnabled).toBe(true);
    store.applyUpdatesMessage(message({}));
    expect(store.isAutoUpdateEnabled).toBe(false);
  });

  it("keeps a refused sign-in for the next surface to say, until the next ask", async () => {
    const store = new UpdatesStore();
    let status = 502;
    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response(JSON.stringify({ error: "Couldn't open a chat in this workspace." }), { status })),
    );

    await store.signIn("agent-a");
    expect(store.signInFailureFor("agent-a")?.error).toBe("Couldn't open a chat in this workspace.");
    expect(store.signInFailureFor("agent-b")).toBeNull();

    status = 200;
    await store.signIn("agent-a");
    expect(store.signInFailureFor("agent-a")).toBeNull();
  });
});
