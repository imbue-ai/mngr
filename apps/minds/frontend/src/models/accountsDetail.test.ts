import { afterEach, describe, expect, it, vi } from "vitest";
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
  is_sharing_workspaces: false,
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
        expect(model.planStateFor(ACCOUNT.user_id).planView).toEqual(PLAN_VIEW);
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

    model.syncAccounts([ACCOUNT]);
    await settle();
    model.syncAccounts([ACCOUNT, secondAccountEntry()]);
    await settle();

    // The notification preferences ride along with each plan load.
    expect(urls).toEqual([
      "/ui/api/accounts/user-1/plan",
      "/ui/api/accounts/user-1/notification-preferences",
      "/ui/api/accounts/user-2/plan",
      "/ui/api/accounts/user-2/notification-preferences",
    ]);
    expect(model.planStateFor("user-1").isUnavailable).toBe(true);
    expect(model.planStateFor("user-1").privacyPolicyUrl).toBe(
      "https://accounts.example.com/privacy-policy",
    );
  });

  it("reloads the plan of an account logged out and added back", async () => {
    const urls: string[] = [];
    const model = new AccountsDetailModel(
      async (input) => {
        urls.push(String(input));
        return jsonResponse({ plan_view: PLAN_VIEW, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );

    model.syncAccounts([ACCOUNT]);
    await settle();
    expect(model.planStateFor("user-1").planView).toEqual(PLAN_VIEW);

    model.syncAccounts([]);
    model.syncAccounts([ACCOUNT]);
    await settle();

    expect(urls.filter((url) => url.endsWith("/plan"))).toEqual([
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

  it("names the failed action as the page error, with the route's answer behind See details", async () => {
    const model = new AccountsDetailModel(
      async (input) => {
        const url = String(input);
        if (url === "/accounts/user-1/plan")
          return jsonResponse(
            { detail: "account set-plan failed (exit 1); see the desktop client logs" },
            502,
          );
        if (url === "/accounts/user-1/logout")
          return new Response("Not authenticated", { status: 403 });
        return jsonResponse({ plan_view: null, trim_status: null });
      },
      () => {},
      (callback) => callback(),
    );

    await model.switchPlan("user-1", "person@example.com", "free", "Limited");
    const planError = model.actionError;
    await model.logOut("user-1", "person@example.com");

    expect(planError).toEqual({
      message:
        "Couldn't switch person@example.com to the Limited plan. Try again. If this persists, report a bug.",
      details:
        "HTTP 502\naccount set-plan failed (exit 1); see the desktop client logs",
    });
    expect(model.actionError).toEqual({
      message:
        "Couldn't log out person@example.com. Try again. If this persists, report a bug.",
      details: "HTTP 403\nNot authenticated",
    });
  });

  it("shows the route's own sentence for a refusal it words, and names the action when nothing answers", async () => {
    const model = new AccountsDetailModel(
      async (input) => {
        if (String(input) === "/accounts/user-1/trim-backups")
          return jsonResponse(
            { error: "Imbue Studio can't do that on this computer." },
            503,
          );
        throw new TypeError("Failed to fetch");
      },
      () => {},
      (callback) => callback(),
    );

    await model.submitAccountForm(
      "/accounts/user-1/trim-backups",
      {},
      "Couldn't start freeing up backup space",
    );
    const refusal = model.actionError;
    await model.logOut("user-1", "person@example.com");

    expect(refusal?.message).toBe("Imbue Studio can't do that on this computer.");
    expect(refusal?.details).toBe("HTTP 503");
    expect(model.actionError?.message).toBe(
      "Couldn't log out person@example.com. Try again. If this persists, report a bug.",
    );
    expect(model.actionError?.details).toContain("Failed to fetch");
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

    await model.switchPlan("user-1", "person@example.com", "explorer", "Explorer");

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
        if (url === "/accounts/user-1/logout") {
          observedBody = String(init?.body);
          observedContentType = new Headers(init?.headers).get("Content-Type");
          return new Response("", { status: 200 });
        }
        throw new Error(`unexpected fetch: ${url}`);
      },
      () => {},
      (callback) => callback(),
    );

    await model.logOut("user-1", "person@example.com");

    expect(observedBody).toBe("");
    expect(observedContentType).toBe("application/x-www-form-urlencoded");
  });
});

describe("the cached plan", () => {
  const UPDATED_VIEW: AccountPlanView = { ...PLAN_VIEW, plan_display_name: "Explorer (updated)" };

  /** A model whose plan reads wait until the test answers them. The notification
   * preferences a sync also reads are answered at once, so the held read is
   * always the plan's. */
  function modelWithHeldReads(): {
    model: AccountsDetailModel;
    answer: (planView: AccountPlanView | null) => void;
  } {
    let release: (response: Response) => void = () => {};
    const model = new AccountsDetailModel(
      (input) => {
        if (String(input).endsWith("/notification-preferences")) {
          return Promise.resolve(
            jsonResponse({ preferences: { email_enabled: true, in_app_enabled: true } }),
          );
        }
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      },
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
    model.syncAccounts([ACCOUNT]);
    await settle();
    model.dispose();
  }

  it("shows a previous visit's plan at once, then replaces it with the fresh read", async () => {
    await visitOnce(PLAN_VIEW);
    const { model, answer } = modelWithHeldReads();

    model.syncAccounts([ACCOUNT]);
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

    model.syncAccounts([ACCOUNT]);
    answer(null);
    await settle();

    const shown = model.planStateFor(ACCOUNT.user_id);
    expect(shown.planView).toEqual(PLAN_VIEW);
    expect(shown.isUnavailable).toBe(false);
    expect(shown.isRefreshing).toBe(false);
    expect(shown.isRefreshFailed).toBe(true);
  });

  it("drops a logged-out account's plan, so adding it back loads from scratch", async () => {
    await visitOnce(PLAN_VIEW);
    const pruner = new AccountsDetailModel(
      async () => jsonResponse({ plan_view: null, trim_status: null }),
      () => {},
      (callback) => callback(),
    );
    pruner.syncAccounts([]);
    const { model } = modelWithHeldReads();

    model.syncAccounts([ACCOUNT]);

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

    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");

    expect(model.actionError).toBeNull();
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

    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");

    expect(model.verifyEmailPromptFor("user-1")).toEqual({
      email: "alice@example.com",
      wasAutoSent: false,
      isResending: false,
      wasResent: false,
    });
  });

  it("says Imbue Cloud's stated reason for a refused switch, without asking for another try", async () => {
    const model = makeModelWithPlanResponse(() =>
      jsonResponse({ code: "plan_refused", reason: "The 'ally' plan requires partner access" }, 403),
    );

    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");

    expect(model.verifyEmailPromptFor("user-1")).toBeNull();
    expect(model.actionError).toEqual({
      message:
        "Imbue Cloud won't switch alice@example.com to the Ally plan. It said: \"The 'ally' plan requires partner access\"",
      details: "HTTP 403",
    });
    expect(model.actionErrorRetry).toBeNull();
  });

  it("keeps any other 403 as the page error (no prompt), its text behind See details", async () => {
    const model = makeModelWithPlanResponse(
      () =>
        new Response("The 'ally' plan requires partner access", {
          status: 403,
        }),
    );

    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");

    expect(model.verifyEmailPromptFor("user-1")).toBeNull();
    expect(model.actionError?.message).toBe(
      "Couldn't switch alice@example.com to the Ally plan. Try again. If this persists, report a bug.",
    );
    expect(model.actionError?.details).toBe(
      "HTTP 403\nThe 'ally' plan requires partner access",
    );
  });

  it("does not claim a resend the server suppressed (200 with sent: false)", async () => {
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
      () => jsonResponse({ sent: false, email: "alice@example.com" }),
    );
    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");

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
          JSON.stringify({
            code: "email_not_verified",
            email: "alice@example.com",
            sent: true,
          }),
          { status: 403 },
        );
      },
      () => jsonResponse({ sent: true, email: "alice@example.com" }),
    );
    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");

    await model.resendVerification("user-1");
    expect(model.verifyEmailPromptFor("user-1")?.wasResent).toBe(true);

    await model.switchPlan("user-1", "alice@example.com", "ally", "Ally");
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

    const logoutDone = model.logOut("user-1", "alice@example.com");
    expect(model.isLoggingOut("user-1")).toBe(true);
    // A second click while busy is swallowed (no state churn, no extra POST).
    await model.logOut("user-1", "alice@example.com");
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

    const switchDone = model.switchPlan("user-1", "alice@example.com", "explorer", "Explorer");
    expect(model.isSwitchingPlan("user-1")).toBe(true);
    // A second click while busy is swallowed (no extra POST).
    await model.switchPlan("user-1", "alice@example.com", "explorer", "Explorer");
    expect(switchPostCount).toBe(1);

    releaseSwitch(new Response("", { status: 200 }));
    await switchDone;
    expect(model.isSwitchingPlan("user-1")).toBe(false);
  });
});

describe("AccountsDetailModel notification preferences", () => {
  const PREFERENCES_URL = `/ui/api/accounts/${ACCOUNT.user_id}/notification-preferences`;

  it("loads each listed account's channels beside its plan, and writes the email switch", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    let isEmailEnabled = true;
    const model = new AccountsDetailModel(
      async (input, init) => {
        const url = String(input);
        const body =
          typeof init?.body === "string"
            ? (JSON.parse(init.body) as unknown)
            : null;
        calls.push({ url, method: init?.method ?? "GET", body });
        if (!url.endsWith("/notification-preferences"))
          return jsonResponse({ plan_view: null, trim_status: null });
        if (init?.method === "POST")
          isEmailEnabled = (body as { email_enabled: boolean }).email_enabled;
        return jsonResponse({
          preferences: { email_enabled: isEmailEnabled, in_app_enabled: true },
        });
      },
      () => {},
      (callback) => callback(),
    );

    model.syncAccounts([ACCOUNT]);
    await settle();

    expect(model.notificationPreferencesFor(ACCOUNT.user_id)).toEqual({
      isLoaded: true,
      channels: { email_enabled: true, in_app_enabled: true },
      readFailure: null,
      isWriting: false,
    });

    await model.setEmailNotifications(ACCOUNT.user_id, false);

    const write = calls.find((call) => call.method === "POST");
    expect(write?.url).toBe(PREFERENCES_URL);
    expect(write?.body).toEqual({ email_enabled: false });
    expect(model.notificationPreferencesFor(ACCOUNT.user_id).channels).toEqual({
      email_enabled: false,
      in_app_enabled: true,
    });
    expect(model.actionError).toBeNull();
  });

  it("keeps why the channels could not be read, and writes nothing then", async () => {
    const methods: string[] = [];
    const model = new AccountsDetailModel(
      async (_input, init) => {
        methods.push(init?.method ?? "GET");
        return jsonResponse({
          preferences: null,
          failure: {
            kind: "SESSION_REJECTED",
            detail: "Refresh rejected by connector",
          },
        });
      },
      () => {},
      (callback) => callback(),
    );

    await model.loadNotificationPreferences(ACCOUNT.user_id);
    await model.setEmailNotifications(ACCOUNT.user_id, false);

    expect(model.notificationPreferencesFor(ACCOUNT.user_id)).toEqual({
      isLoaded: true,
      channels: null,
      readFailure: {
        kind: "SESSION_REJECTED",
        details: "Refresh rejected by connector",
      },
      isWriting: false,
    });
    expect(methods).toEqual(["GET"]);
  });

  it("reads a section answered with text instead of JSON as failed, the text behind See details", async () => {
    const model = new AccountsDetailModel(
      async () => new Response("<html>Bad gateway</html>", { status: 200 }),
      () => {},
      (callback) => callback(),
    );

    await model.loadPlan(ACCOUNT.user_id);

    expect(model.planStateFor(ACCOUNT.user_id).isUnavailable).toBe(true);
    expect(model.planStateFor(ACCOUNT.user_id).readFailure).toEqual({
      kind: "FAILED",
      details: "HTTP 200\n<html>Bad gateway</html>",
    });
  });

  it("tells a refusal of this window's own login apart from a failed read", async () => {
    const model = new AccountsDetailModel(
      async (input) =>
        String(input).endsWith("/plan")
          ? jsonResponse({ error: "Not authenticated" }, 401)
          : jsonResponse({ error: "boom" }, 500),
      () => {},
      (callback) => callback(),
    );

    await model.loadPlan(ACCOUNT.user_id);
    await model.loadNotificationPreferences(ACCOUNT.user_id);

    expect(model.planStateFor(ACCOUNT.user_id).isUnavailable).toBe(true);
    expect(model.planStateFor(ACCOUNT.user_id).readFailure).toEqual({
      kind: "WINDOW_LOGIN_LAPSED",
      details: "HTTP 401\nNot authenticated",
    });
    expect(
      model.notificationPreferencesFor(ACCOUNT.user_id).readFailure,
    ).toEqual({ kind: "FAILED", details: "HTTP 500\nboom" });
  });

  it.each([
    {
      name: "a refused write",
      answer: () => jsonResponse({ error: "boom" }, 502),
      details: "HTTP 502\nboom",
    },
    {
      name: "a write Imbue Cloud did not take",
      answer: () =>
        jsonResponse({
          preferences: null,
          failure: { kind: "FAILED", detail: "connector unreachable" },
        }),
      details: "connector unreachable",
    },
    {
      name: "a write that got no answer",
      answer: () => Promise.reject(new TypeError("Failed to fetch")),
      details: "No response. Failed to fetch",
    },
  ])(
    "leaves the switch where it was and names the failed save for $name, with why behind See details",
    async ({ answer, details }) => {
      const model = new AccountsDetailModel(
        async (_input, init) =>
          init?.method === "POST"
            ? answer()
            : jsonResponse({
                preferences: { email_enabled: true, in_app_enabled: true },
              }),
        () => {},
        (callback) => callback(),
      );
      await model.loadNotificationPreferences(ACCOUNT.user_id);

      await model.setEmailNotifications(ACCOUNT.user_id, false);

      expect(
        model.notificationPreferencesFor(ACCOUNT.user_id).channels
          ?.email_enabled,
      ).toBe(true);
      expect(model.notificationPreferencesFor(ACCOUNT.user_id).isWriting).toBe(
        false,
      );
      expect(model.actionError).toEqual({
        message:
          "Couldn't save your notification setting. Try again. If this persists, report a bug.",
        details,
      });
    },
  );

  it("sends the same setting again from the failed save's Try again", async () => {
    const posted: unknown[] = [];
    let isPostFailing = true;
    const model = new AccountsDetailModel(
      async (_input, init) => {
        if (init?.method !== "POST") {
          return jsonResponse({ preferences: { email_enabled: true, in_app_enabled: true } });
        }
        posted.push(JSON.parse(String(init.body)));
        return isPostFailing
          ? jsonResponse({ error: "boom" }, 502)
          : jsonResponse({ preferences: { email_enabled: false, in_app_enabled: true } });
      },
      () => {},
      (callback) => callback(),
    );
    await model.loadNotificationPreferences(ACCOUNT.user_id);
    await model.setEmailNotifications(ACCOUNT.user_id, false);
    isPostFailing = false;

    model.actionErrorRetry?.();
    await vi.waitFor(() =>
      expect(model.notificationPreferencesFor(ACCOUNT.user_id).channels?.email_enabled).toBe(false),
    );

    expect(posted).toEqual([{ email_enabled: false }, { email_enabled: false }]);
    expect(model.actionError).toBeNull();
    expect(model.actionErrorRetry).toBeNull();
  });

  it.each([
    {
      name: "an expired Imbue Cloud sign-in",
      answer: () =>
        jsonResponse({
          preferences: null,
          failure: {
            kind: "SESSION_REJECTED",
            detail: "Refresh rejected by connector",
          },
        }),
      readFailure: {
        kind: "SESSION_REJECTED",
        details: "Refresh rejected by connector",
      },
    },
    {
      name: "this window's lapsed login",
      answer: () => jsonResponse({ error: "Not authenticated" }, 401),
      readFailure: {
        kind: "WINDOW_LOGIN_LAPSED",
        details: "HTTP 401\nNot authenticated",
      },
    },
  ])(
    "shows the section's read notice, not a failed save, for a write refused for $name",
    async ({ answer, readFailure }) => {
      const model = new AccountsDetailModel(
        async (_input, init) =>
          init?.method === "POST"
            ? answer()
            : jsonResponse({
                preferences: { email_enabled: true, in_app_enabled: true },
              }),
        () => {},
        (callback) => callback(),
      );
      await model.loadNotificationPreferences(ACCOUNT.user_id);

      await model.setEmailNotifications(ACCOUNT.user_id, false);

      expect(model.notificationPreferencesFor(ACCOUNT.user_id)).toEqual({
        isLoaded: true,
        channels: null,
        readFailure,
        isWriting: false,
      });
      expect(model.actionError).toBeNull();
    },
  );
});
