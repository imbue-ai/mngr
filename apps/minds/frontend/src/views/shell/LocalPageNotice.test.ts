import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import { HealthStore } from "../../models/health";
import type { EnvironmentCondition } from "../../models/health";
import { LocalPageNotice } from "./LocalPageNotice";
import { allText, renderRoot } from "../../testing";

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
});

/** Register a context carrying nothing but the health store this notice reads. */
function noticeFor(environment: EnvironmentCondition): string {
  const health = new HealthStore();
  health.applyEnvironmentMessage({ type: "environment", state: environment });
  registerAppContext({ stores: { health }, shell: {} } as unknown as AppContext);
  // The bridge resolves window.mindsNative on every call, and vitest's node
  // environment has no window at all.
  vi.stubGlobal("window", {});
  return allText(renderRoot(LocalPageNotice, {}));
}

describe("LocalPageNotice", () => {
  it("names the device's condition on a page that has no band to carry it", () => {
    // A hub page is where a user who opened Minds on a dead network actually
    // is, and it has no machine behind it -- so the app-level reading is the
    // only thing that can speak, and this component's one line is what hands it
    // over. The copy selection itself is notice-band.ts's own business.
    expect(noticeFor("OFFLINE")).toContain("No network connection.");
    expect(noticeFor("SSH_BLOCKED")).toContain("This network blocks the connection to your machines.");
  });

  it("says nothing about a device with no trouble to report, or none measured yet", () => {
    expect(noticeFor("NONE")).toBe("");
    expect(noticeFor("UNKNOWN")).toBe("");
  });
});
