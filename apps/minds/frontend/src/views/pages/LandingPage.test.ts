import { describe, expect, it } from "vitest";
import { shouldRedirectToStartFlow } from "./LandingPage";

describe("shouldRedirectToStartFlow", () => {
  const settled = { is_discovery_complete: true, has_restorable_workspaces: false, destroying_status_by_agent_id: {}, orphaned_failed_destroys: [], locked_account_emails: [] };

  it("sends an install that never finished onboarding to the start flow once discovery finds nothing", () => {
    expect(shouldRedirectToStartFlow(false, false, settled)).toBe(true);
  });

  it("never redirects a completed install, a listed workspace, or an unfinished discovery", () => {
    expect(shouldRedirectToStartFlow(true, false, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, true, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, null)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, { ...settled, is_discovery_complete: false })).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, { ...settled, has_restorable_workspaces: true })).toBe(false);
  });
});
