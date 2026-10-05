import { afterEach, describe, expect, it } from "vitest";
import {
  accountEntry,
  jsonResponse,
  secondAccountEntry,
  settle,
  withReceiverGuardedGlobalFetch,
} from "../testing";
import {
  AccountsDetailModel,
  clearAccountPlanCacheForTests,
  type AccountPlanView,
} from "./accountsDetail";

const ACCOUNT = accountEntry();

const PLAN_VIEW: AccountPlanView = {
  plan_name: "explorer",
  plan_display_name: "Explorer",
  available_plans: ["explorer"],
  usage_rows: [],
  is_over_storage_quota: false,
  is_at_bucket_quota: false,
};

afterEach(() => {
  clearAccountPlanCacheForTests();
});

describe("AccountsDetailModel", () => {
  it("invokes the default fetch as a plain call (Illegal-invocation regression guard)", async () => {
    // Browsers reject the global fetch when it is invoked with any other
    // receiver (as `this.fetchImpl(...)` would if the default were the bare
    // global), so the default must wrap it in a plain call.
    await withReceiverGuardedGlobalFetch(
      { plan_view: PLAN_VIEW, trim_status: null },
      async () => {
        const model = new AccountsDetailModel(
          undefined,
          () => {},
          (callback) => callback(),
        );
        await model.loadPlan(ACCOUNT.user_id);
        expect(model.planStateFor(ACCOUNT.user_id).planView).toEqual(
          PLAN_VIEW,
        );
      },
    );
  });

  it("loads each listed account's plan section once, and a newly listed account's too", async () => {
    const urls: string[] = [];
    const model = new AccountsDetailModel(
      async (input) => {
        urls.push(String(input));
        return jsonResponse({
          plan_view: null,
          trim_status: null,
          privacy_policy_url: "https://accounts.example.com/privacy-policy",
        });
      },
      () => {},
      (callback) => callback(),
    );

    model.syncPlans([ACCOUNT]);
    await settle();
    model.syncPlans([ACCOUNT, secondAccountEntry()]);
    await settle();

    expect(urls).toEqual([
      "/ui/api/accounts/user-1/plan",
      "/ui/api/accounts/user-2/plan",
    ]);
    expect(model.planStateFor("user-1").isUnavailable).toBe(true);
    expect(model.planStateFor("user-1").privacyPolicyUrl).toBe(
      "https://accounts.example.com/privacy-policy",
    );
  });

  it("reloads the plan of an account signed back in, or logged out and added back", async () => {
    // A plan loaded while signed out reads as unavailable; the sign-in that
    // follows must replace it rather than leave the card saying so.
    let planView: AccountPlanView | null = null;
    const urls: string[] = [];
    const model = new AccountsDetailModel(
      async (input) => {
        urls.push(String(input));
        return jsonResponse({ plan_view: planView, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );
    const signedOut = accountEntry({ is_enabled: false });

    model.syncPlans([signedOut]);
    await settle();
    expect(model.planStateFor("user-1").isUnavailable).toBe(true);

    planView = PLAN_VIEW;
    model.syncPlans([ACCOUNT]);
    await settle();
    expect(model.planStateFor("user-1").planView).toEqual(PLAN_VIEW);

    model.syncPlans([]);
    model.syncPlans([ACCOUNT]);
    await settle();

    expect(urls).toEqual([
      "/ui/api/accounts/user-1/plan",
      "/ui/api/accounts/user-1/plan",
      "/ui/api/accounts/user-1/plan",
    ]);
  });

  it("re-polls the plan section while a trim is running", async () => {
    let planFetchCount = 0;
    const scheduled: (() => void)[] = [];
    const model = new AccountsDetailModel(
      async () => {
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

    await model.loadPlan(ACCOUNT.user_id);
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
        if (url === "/accounts/user-1/plan")
          return new Response("No plan selected.", { status: 422 });
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );

    await model.switchPlan("user-1", "");

    expect(model.actionError).toBe("No plan selected.");
  });

  it("reloads the account's plan section after a successful switch", async () => {
    const urls: string[] = [];
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        urls.push(url);
        if (url === "/accounts/user-1/plan")
          return new Response("", { status: 200 });
        return jsonResponse({ plan_view: PLAN_VIEW, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );

    await model.switchPlan("user-1", "explorer");

    expect(urls).toEqual([
      "/accounts/user-1/plan",
      "/ui/api/accounts/user-1/plan",
    ]);
    expect(model.planStateFor("user-1").planView).toEqual(PLAN_VIEW);
    expect(model.isSwitchingPlan("user-1")).toBe(false);
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
        throw new Error(`unexpected fetch: ${url}`);
      },
      () => {},
      (callback) => callback(),
    );

    await model.setDefault("user-1");

    expect(observedBody).toBe("user_id=user-1");
    expect(observedContentType).toBe("application/x-www-form-urlencoded");
  });
});

describe("the cached plan", () => {
  const UPDATED_VIEW: AccountPlanView = { ...PLAN_VIEW, plan_display_name: "Explorer (updated)" };

  /** A model whose plan reads wait until the test answers them. */
  function modelWithHeldReads(): {
    model: AccountsDetailModel;
    answer: (planView: AccountPlanView | null) => void;
  } {
    let release: (response: Response) => void = () => {};
    const model = new AccountsDetailModel(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
      () => {},
      (callback) => callback(),
    );
    return {
      model,
      answer: (planView) => release(jsonResponse({ plan_view: planView, trim_status: null })),
    };
  }

  async function visitOnce(planView: AccountPlanView): Promise<void> {
    const model = new AccountsDetailModel(
      async () => jsonResponse({ plan_view: planView, trim_status: null }),
      () => {},
      (callback) => callback(),
    );
    model.syncPlans([ACCOUNT]);
    await settle();
    model.dispose();
  }

  it("shows a previous visit's plan at once, then replaces it with the fresh read", async () => {
    await visitOnce(PLAN_VIEW);
    const { model, answer } = modelWithHeldReads();

    model.syncPlans([ACCOUNT]);
    const shown = model.planStateFor(ACCOUNT.user_id);
    expect(shown.isLoaded).toBe(true);
    expect(shown.planView).toEqual(PLAN_VIEW);
    expect(shown.isRefreshing).toBe(true);

    answer(UPDATED_VIEW);
    await settle();

    expect(shown.planView).toEqual(UPDATED_VIEW);
    expect(shown.isRefreshing).toBe(false);
    expect(shown.isRefreshFailed).toBe(false);
  });

  it("keeps the earlier plan on screen, marked, when the refresh cannot reach the cloud", async () => {
    await visitOnce(PLAN_VIEW);
    const { model, answer } = modelWithHeldReads();

    model.syncPlans([ACCOUNT]);
    answer(null);
    await settle();

    const shown = model.planStateFor(ACCOUNT.user_id);
    expect(shown.planView).toEqual(PLAN_VIEW);
    expect(shown.isUnavailable).toBe(false);
    expect(shown.isRefreshing).toBe(false);
    expect(shown.isRefreshFailed).toBe(true);
  });

  it("does not show a signed-out account's earlier plan, which reads as unavailable", async () => {
    await visitOnce(PLAN_VIEW);
    const { model, answer } = modelWithHeldReads();

    model.syncPlans([{ ...ACCOUNT, is_enabled: false }]);
    const shown = model.planStateFor(ACCOUNT.user_id);
    expect(shown.isLoaded).toBe(false);
    expect(shown.planView).toBeNull();

    answer(null);
    await settle();

    expect(shown.isUnavailable).toBe(true);
    expect(shown.isRefreshFailed).toBe(false);
  });

  it("drops a logged-out account's plan, so adding it back loads from scratch", async () => {
    await visitOnce(PLAN_VIEW);
    const pruner = new AccountsDetailModel(
      async () => jsonResponse({ plan_view: null, trim_status: null }),
      () => {},
      (callback) => callback(),
    );
    pruner.syncPlans([]);
    const { model } = modelWithHeldReads();

    model.syncPlans([ACCOUNT]);

    const shown = model.planStateFor(ACCOUNT.user_id);
    expect(shown.isLoaded).toBe(false);
    expect(shown.planView).toBeNull();
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
        if (url === "/accounts/user-1/logout")
          return new Promise<Response>((resolve) => {
            releaseLogout = resolve;
          });
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );

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
