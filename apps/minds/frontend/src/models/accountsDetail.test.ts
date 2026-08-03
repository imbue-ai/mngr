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
        return jsonResponse({ plan_view: null, trim_status: null });
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
