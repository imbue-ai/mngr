import { describe, expect, it } from "vitest";
import { localPageNoticeFor, noticeBandFor } from "./notice-band";

describe("noticeBandFor", () => {
  it("shows nothing while the machine and the app are both healthy", () => {
    expect(noticeBandFor("healthy", "healthy", true)).toBeNull();
  });

  it("bands a machine that stops answering, and keeps one payload across the recovery states", () => {
    const stuck = noticeBandFor("stuck", "healthy", true);
    const restarting = noticeBandFor("restarting", "healthy", true);
    expect(stuck?.key).toBe("workspace-recovering");
    // Recovery steps between stuck and restarting on its own; sharing the
    // payload is what keeps the strip from rewriting itself mid-read.
    expect(restarting).toEqual(stuck);
    expect(stuck?.action?.kind).toBe("open-recovery");
  });

  it("separates a spent restart from one still in progress", () => {
    const failed = noticeBandFor("restart_failed", "healthy", true);
    expect(failed?.key).toBe("workspace-restart-failed");
    expect(failed?.variant).toBe("error");
    expect(failed?.message).not.toBe(noticeBandFor("stuck", "healthy", true)?.message);
  });

  it("states the condition without recounting a restart the user never made", () => {
    // The app restarts a wedged machine unasked, so an account of a failed
    // restart usually describes an event the user never caused and never saw.
    // The remedy and its cost live on the card behind the action.
    expect(noticeBandFor("restart_failed", "healthy", true)?.message).toBe("This machine stopped responding.");
  });

  it("names the backend it cannot reach instead of the machine that reads stuck because of it", () => {
    // The machine is unreachable because its provider is, and a restart routes
    // through that same provider -- so the band explains the condition rather
    // than repeating the symptom. It keeps the recovering key: this is still
    // "lost contact, still trying", only better explained, and rewriting the
    // strip as a provider error lands and clears would only interrupt a read.
    const band = noticeBandFor("stuck", "healthy", true, true, "Imbue Cloud");
    expect(band?.key).toBe("workspace-recovering");
    // The cause, and nothing else: the band is one line, and the card behind
    // the action is where what it means for this machine belongs.
    expect(band?.message).toBe("Can't connect to Imbue Cloud");
    expect(band?.action?.kind).toBe("open-recovery");
    // The card behind it carries the provider's own error verbatim.
    expect(noticeBandFor("restart_failed", "healthy", true, true, "Imbue Cloud")?.message).toBe(band?.message);
  });

  it("leaves a healthy machine unbanded even while its provider is erroring", () => {
    // A stale row is not a broken machine: the workspace keeps answering
    // through the forward whatever discovery last managed to poll.
    expect(noticeBandFor("healthy", "healthy", true, true, "Imbue Cloud")).toBeNull();
  });

  it("names the dead consumer instead of the stuck machine it produces", () => {
    // Every machine reads stuck while the consumer is dead, and restarting
    // one would not help -- only the app restart does.
    const band = noticeBandFor("stuck", "blocked", true);
    expect(band?.key).toBe("discovery-blocked");
    expect(band?.action?.kind).toBe("restart-app");
  });

  it("leaves a reconnecting consumer to the shell's own indicator", () => {
    expect(noticeBandFor("healthy", "reconnecting", true)).toBeNull();
  });

  it("withholds the band from hub pages, which have no machine behind it", () => {
    expect(noticeBandFor("stuck", "healthy", false)).toBeNull();
    expect(noticeBandFor("restart_failed", "blocked", false)).toBeNull();
  });
});

describe("localPageNoticeFor", () => {
  it("carries the consumer-death condition into hub pages", () => {
    expect(localPageNoticeFor("blocked")?.key).toBe("discovery-blocked");
    expect(localPageNoticeFor("healthy")).toBeNull();
    expect(localPageNoticeFor("reconnecting")).toBeNull();
  });

  it("says the same thing as the band, from the same source", () => {
    // The two surfaces drifted apart when each wrote its own copy.
    expect(localPageNoticeFor("blocked")).toEqual(noticeBandFor("healthy", "blocked", true));
  });
});

describe("restart-app availability", () => {
  it("drops the action where there is no app to restart", () => {
    // Browser mode has no main process: the button would be inert, which is
    // worse than stating the condition and offering nothing.
    const band = noticeBandFor("healthy", "blocked", true, false);
    expect(band?.message).toBe(noticeBandFor("healthy", "blocked", true, true)?.message);
    expect(band?.action).toBeNull();
    expect(localPageNoticeFor("blocked", false)?.action).toBeNull();
  });
});
