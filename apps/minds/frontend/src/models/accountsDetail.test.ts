import { describe, expect, it } from "vitest";
import { jsonResponse, withReceiverGuardedGlobalFetch } from "../testing";
import { AccountsDetailModel, type AccountEntry } from "./accountsDetail";

const ACCOUNT: AccountEntry = {
  user_id: "user-1",
  email: "alice@example.com",
  workspace_count: 2,
  is_default: true,
  is_enabled: true,
};

describe("AccountsDetailModel", () => {
  it("invokes the default fetch as a plain call (Illegal-invocation regression guard)", async () => {
    // Browsers reject the global fetch when it is invoked with any other
    // receiver (as `this.fetchImpl(...)` would if the default were the bare
    // global), so the default must wrap it in a plain call.
    await withReceiverGuardedGlobalFetch({ accounts: [ACCOUNT] }, async () => {
      const model = new AccountsDetailModel(
        undefined,
        () => {},
        (callback) => callback(),
      );
      await model.load();
      expect(model.isLoadFailed).toBe(false);
      expect(model.accounts).toHaveLength(1);
    });
  });

  it("loads the account list and then each account's plan section", async () => {
    const urls: string[] = [];
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        urls.push(url);
        if (url === "/ui/api/accounts")
          return jsonResponse({ accounts: [ACCOUNT] });
        return jsonResponse({
          plan_view: null,
          trim_status: null,
          privacy_policy_url: "https://accounts.example.com/privacy-policy",
        });
      },
      () => {},
      (callback) => callback(),
    );

    await model.load();
    // The plan load is fired without awaiting; flush the microtask queue.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(model.isListLoaded).toBe(true);
    expect(model.accounts).toEqual([ACCOUNT]);
    expect(urls).toContain("/ui/api/accounts/user-1/plan");
    expect(model.planStateFor("user-1").isUnavailable).toBe(true);
    expect(model.planStateFor("user-1").privacyPolicyUrl).toBe(
      "https://accounts.example.com/privacy-policy",
    );
  });

  it("re-polls the plan section while a trim is running", async () => {
    let planFetchCount = 0;
    const scheduled: (() => void)[] = [];
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        if (url === "/ui/api/accounts")
          return jsonResponse({ accounts: [ACCOUNT] });
        planFetchCount += 1;
        const isStillRunning = planFetchCount === 1;
        return jsonResponse({
          plan_view: null,
          trim_status: {
            is_running: isStillRunning,
            detail: isStillRunning ? "trimming" : "done",
          },
        });
      },
      () => {},
      (callback) => scheduled.push(callback),
    );

    await model.load();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(planFetchCount).toBe(1);
    expect(scheduled.length).toBe(1);

    scheduled[0]();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(planFetchCount).toBe(2);
    expect(scheduled.length).toBe(1);
    expect(model.planStateFor("user-1").trimStatus?.detail).toBe("done");
    // A payload without privacy_policy_url (older backends) falls back to "".
    expect(model.planStateFor("user-1").privacyPolicyUrl).toBe("");
  });

  it("surfaces a failed form action's body as the page error", async () => {
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        if (url === "/ui/api/accounts")
          return jsonResponse({ accounts: [ACCOUNT] });
        if (url === "/accounts/user-1/plan")
          return new Response("No plan selected.", { status: 422 });
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );
    await model.load();

    await model.switchPlan("user-1", "");

    expect(model.actionError).toBe("No plan selected.");
  });

  it("sends form-encoded bodies to the legacy account routes", async () => {
    let observedBody = "";
    let observedContentType: string | null = null;
    const model = new AccountsDetailModel(
      async (input, init) => {
        const url = String(input);
        if (url === "/accounts/set-default") {
          observedBody = String(init?.body);
          observedContentType = new Headers(init?.headers).get("Content-Type");
          return new Response("", { status: 200 });
        }
        return jsonResponse({ accounts: [] });
      },
      () => {},
      (callback) => callback(),
    );

    await model.setDefault("user-1");

    expect(observedBody).toBe("user_id=user-1");
    expect(observedContentType).toBe("application/x-www-form-urlencoded");
  });
});

describe("verify-email prompt", () => {
  function makeModelWithPlanResponse(
    planResponse: () => Response,
    onResend?: () => Response,
  ): AccountsDetailModel {
    return new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        if (url === "/ui/api/accounts")
          return jsonResponse({ accounts: [ACCOUNT] });
        if (url === "/accounts/user-1/plan") return planResponse();
        if (url === "/accounts/user-1/resend-verification" && onResend)
          return onResend();
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );
  }

  it("shows the prompt on a structured email_not_verified 403 instead of the page error", async () => {
    const model = makeModelWithPlanResponse(
      () =>
        new Response(
          JSON.stringify({
            code: "email_not_verified",
            email: "alice@example.com",
            sent: true,
          }),
          { status: 403 },
        ),
    );
    await model.load();

    await model.switchPlan("user-1", "ally");

    expect(model.actionError).toBe("");
    expect(model.verifyEmailPromptFor("user-1")).toEqual({
      email: "alice@example.com",
      wasAutoSent: true,
      isResending: false,
      wasResent: false,
    });
  });

  it("records a suppressed auto-send so the prompt does not claim a link was sent", async () => {
    const model = makeModelWithPlanResponse(
      () =>
        new Response(
          JSON.stringify({
            code: "email_not_verified",
            email: "alice@example.com",
            sent: false,
          }),
          { status: 403 },
        ),
    );
    await model.load();

    await model.switchPlan("user-1", "ally");

    expect(model.verifyEmailPromptFor("user-1")).toEqual({
      email: "alice@example.com",
      wasAutoSent: false,
      isResending: false,
      wasResent: false,
    });
  });

  it("keeps a plain 403 as the page error (no prompt)", async () => {
    const model = makeModelWithPlanResponse(
      () => new Response("The 'ally' plan requires partner access", { status: 403 }),
    );
    await model.load();

    await model.switchPlan("user-1", "ally");

    expect(model.verifyEmailPromptFor("user-1")).toBeNull();
    expect(model.actionError).toBe("The 'ally' plan requires partner access");
  });

  it("does not claim a resend the server suppressed (200 with sent: false)", async () => {
    const model = makeModelWithPlanResponse(
      () =>
        new Response(
          JSON.stringify({ code: "email_not_verified", email: "alice@example.com", sent: true }),
          { status: 403 },
        ),
      () => jsonResponse({ sent: false, email: "alice@example.com" }),
    );
    await model.load();
    await model.switchPlan("user-1", "ally");

    await model.resendVerification("user-1");

    expect(model.verifyEmailPromptFor("user-1")?.wasResent).toBe(false);
    expect(model.verifyEmailPromptFor("user-1")?.isResending).toBe(false);
  });

  it("clears the prompt on the next switch attempt and marks a resend", async () => {
    let planCalls = 0;
    const model = makeModelWithPlanResponse(
      () => {
        planCalls += 1;
        return new Response(
          JSON.stringify({ code: "email_not_verified", email: "alice@example.com", sent: true }),
          { status: 403 },
        );
      },
      () => jsonResponse({ sent: true, email: "alice@example.com" }),
    );
    await model.load();
    await model.switchPlan("user-1", "ally");

    await model.resendVerification("user-1");
    expect(model.verifyEmailPromptFor("user-1")?.wasResent).toBe(true);

    await model.switchPlan("user-1", "ally");
    expect(planCalls).toBe(2);
    expect(model.verifyEmailPromptFor("user-1")?.wasResent).toBe(false);
  });
});

describe("log-out busy state", () => {
  it("marks the account busy during the POST and clears it after", async () => {
    let releaseLogout: (response: Response) => void = () => {};
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        if (url === "/ui/api/accounts")
          return jsonResponse({ accounts: [ACCOUNT] });
        if (url === "/accounts/user-1/logout")
          return new Promise<Response>((resolve) => {
            releaseLogout = resolve;
          });
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );
    await model.load();

    const logoutDone = model.logOut("user-1");
    expect(model.isLoggingOut("user-1")).toBe(true);
    // A second click while busy is swallowed (no state churn, no extra POST).
    await model.logOut("user-1");
    expect(model.isLoggingOut("user-1")).toBe(true);

    releaseLogout(new Response("", { status: 200 }));
    await logoutDone;
    expect(model.isLoggingOut("user-1")).toBe(false);
  });
});

describe("plan-switch busy state", () => {
  it("marks the account busy during the POST and swallows a second click", async () => {
    let releaseSwitch: (response: Response) => void = () => {};
    let switchPostCount = 0;
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        if (url === "/ui/api/accounts")
          return jsonResponse({ accounts: [ACCOUNT] });
        if (url === "/accounts/user-1/plan") {
          switchPostCount += 1;
          return new Promise<Response>((resolve) => {
            releaseSwitch = resolve;
          });
        }
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );
    await model.load();

    const switchDone = model.switchPlan("user-1", "explorer");
    expect(model.isSwitchingPlan("user-1")).toBe(true);
    // A second click while busy is swallowed (no extra POST).
    await model.switchPlan("user-1", "explorer");
    expect(switchPostCount).toBe(1);

    releaseSwitch(new Response("", { status: 200 }));
    await switchDone;
    expect(model.isSwitchingPlan("user-1")).toBe(false);
  });
});
