import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { acknowledgeErrorReportingConsent, skipAccountSetup } from "./onboarding";

describe("onboarding transitions", () => {
  it("posts the consent acknowledgement and reports success", async () => {
    const calls: Array<{ url: string; method?: string }> = [];
    const ok = await acknowledgeErrorReportingConsent((url, init) => {
      calls.push({ url, method: init?.method });
      return Promise.resolve(jsonResponse({}));
    });
    expect(ok).toBe(true);
    expect(calls).toEqual([{ url: "/ui/api/onboarding/consent", method: "POST" }]);
  });

  it("reports failure (without throwing) when the consent post fails", async () => {
    const ok = await acknowledgeErrorReportingConsent(() => Promise.reject(new Error("offline")));
    expect(ok).toBe(false);
  });

  it("reports a non-ok consent response as unsuccessful", async () => {
    const ok = await acknowledgeErrorReportingConsent(() => Promise.resolve(new Response("", { status: 403 })));
    expect(ok).toBe(false);
  });

  it("posts the skip-account-setup choice", async () => {
    const calls: string[] = [];
    const ok = await skipAccountSetup((url) => {
      calls.push(url);
      return Promise.resolve(jsonResponse({}));
    });
    expect(ok).toBe(true);
    expect(calls).toEqual(["/ui/api/onboarding/skip-account-setup"]);
  });

  it("reports failure (without throwing) when the skip post fails", async () => {
    const ok = await skipAccountSetup(() => Promise.reject(new Error("offline")));
    expect(ok).toBe(false);
  });
});
