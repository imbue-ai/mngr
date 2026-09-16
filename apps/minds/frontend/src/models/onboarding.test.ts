import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { OnboardingProgress, acknowledgeErrorReportingConsent, markOnboardingComplete } from "./onboarding";

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

  it("posts the onboarding-complete fact and flips the local copy", async () => {
    const progress = new OnboardingProgress();
    progress.seed(false);
    const calls: Array<{ url: string; method?: string }> = [];
    const ok = await markOnboardingComplete((url, init) => {
      calls.push({ url, method: init?.method });
      return Promise.resolve(jsonResponse({}));
    }, progress);
    expect(ok).toBe(true);
    expect(calls).toEqual([{ url: "/ui/api/onboarding/complete", method: "POST" }]);
    expect(progress.isComplete).toBe(true);
  });

  it("flips the local copy even when the post fails, and reports the failure", async () => {
    // The user did complete the flow; a lost write only means the next launch
    // may ask again. The window itself must not bounce back to /start.
    const progress = new OnboardingProgress();
    progress.seed(false);
    const ok = await markOnboardingComplete(() => Promise.reject(new Error("offline")), progress);
    expect(ok).toBe(false);
    expect(progress.isComplete).toBe(true);
  });
});
