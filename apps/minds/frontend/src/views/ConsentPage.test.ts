import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mountConsent } from "./ConsentPage";

function mountFixture(reportUnexpectedErrors = false, includeLogs = false): HTMLElement {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({
    consent: { report_unexpected_errors: reportUnexpectedErrors, include_logs: includeLogs },
  });
  document.body.appendChild(island);
  const container = document.createElement("div");
  container.id = "consent-root";
  document.body.appendChild(container);
  mountConsent(container);
  return container;
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
}

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("mountConsent", () => {
  it("reveals Include logs only while reporting is enabled, clearing it on disable", () => {
    const container = mountFixture();
    expect(container.querySelector("#consent-logs-row")).toBeNull();

    const report = container.querySelector("#consent-report") as HTMLInputElement;
    report.checked = true;
    report.dispatchEvent(new Event("change"));
    m.redraw.sync();
    const logs = container.querySelector("#consent-logs") as HTMLInputElement;
    expect(logs).not.toBeNull();
    logs.checked = true;
    logs.dispatchEvent(new Event("change"));
    m.redraw.sync();

    // Turning reporting back off hides AND clears the logs choice, so a later
    // re-enable starts unchecked.
    report.checked = false;
    report.dispatchEvent(new Event("change"));
    m.redraw.sync();
    expect(container.querySelector("#consent-logs-row")).toBeNull();
    report.checked = true;
    report.dispatchEvent(new Event("change"));
    m.redraw.sync();
    expect((container.querySelector("#consent-logs") as HTMLInputElement).checked).toBe(false);
  });

  it("seeds both toggles from the island", () => {
    const container = mountFixture(true, true);
    expect((container.querySelector("#consent-report") as HTMLInputElement).checked).toBe(true);
    expect((container.querySelector("#consent-logs") as HTMLInputElement).checked).toBe(true);
  });

  it("POSTs the choices on Continue and navigates home even when persisting fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    const setHref = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        set href(value: string) {
          setHref(value);
        },
      },
    });
    try {
      const container = mountFixture(true, false);
      (container.querySelector("#consent-continue") as HTMLButtonElement).click();
      await flushAsync();
      expect(fetchMock).toHaveBeenCalledWith("/consent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ report_unexpected_errors: true, include_logs: false }),
      });
      // The consent flag stays unset server-side, so the screen simply
      // reappears next launch; the page still moves on.
      expect(setHref).toHaveBeenCalledWith("/");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });
});
