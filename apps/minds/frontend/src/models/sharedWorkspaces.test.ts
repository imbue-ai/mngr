import { describe, expect, it } from "vitest";
import type { RequestResult } from "./errors";
import { accountEntry, secondAccountEntry, settle, sharedWorkspaceRow as row } from "../testing";
import {
  accountSharesUrl,
  SharedWorkspacesModel,
  type SharedWorkspacesDeps,
} from "./sharedWorkspaces";

/** Answers each listed URL with its body; any other read is Imbue Cloud failing. */
class FakeDeps implements SharedWorkspacesDeps {
  responseByUrl = new Map<string, unknown>();
  failedRead: RequestResult = {
    ok: false,
    status: 502,
    body: { error: "Could not list this account's shares: connector down", reason: "CONNECTOR_FAILED" },
  };
  getUrls: string[] = [];
  deleteUrls: string[] = [];
  deleteResult: RequestResult = { ok: true, status: 200, body: { outcome: "removed" } };
  redrawCount = 0;

  async getJson(url: string): Promise<RequestResult> {
    this.getUrls.push(url);
    const body = this.responseByUrl.get(url);
    return body === undefined ? this.failedRead : { ok: true, status: 200, body };
  }

  async deleteJson(url: string): Promise<RequestResult> {
    this.deleteUrls.push(url);
    return this.deleteResult;
  }

  redraw(): void {
    this.redrawCount += 1;
  }
}

describe("SharedWorkspacesModel", () => {
  it("loads one section per signed-in account and counts the leaked rows", async () => {
    const first = accountEntry({ email: "person@example.com" });
    const second = secondAccountEntry();
    const deps = new FakeDeps();
    const leaked = row({ host_id: "host-" + "b".repeat(32), record_state: "destroyed", is_likely_leaked: true });
    deps.responseByUrl.set(accountSharesUrl(first.user_id), {
      account_id: first.user_id,
      email: first.email,
      rows: [row(), leaked],
    });
    const model = new SharedWorkspacesModel(deps);

    await model.load([first, second]);

    expect(model.isLoaded).toBe(true);
    expect(model.sections.map((section) => section.email)).toEqual([first.email, second.email]);
    expect(model.sections[0].rows).toHaveLength(2);
    // The second account's read failed: its section says so without failing the page.
    expect(model.sections[1].rows).toBeNull();
    expect(model.sections[1].failure?.error).toEqual({
      message:
        "Couldn't read this account's shared workspaces from Imbue Cloud. Try again. If this persists, report a bug.",
      details: expect.stringContaining("connector down"),
    });
    expect(model.sections[1].failure?.remedies).toEqual(["retry"]);
    expect(model.rowCount).toBe(2);
    expect(model.leakedCount).toBe(1);
  });

  it("re-reads the shares only when the set of signed-in accounts changes", async () => {
    const first = accountEntry({ email: "person@example.com" });
    const second = secondAccountEntry();
    const deps = new FakeDeps();
    deps.responseByUrl.set(accountSharesUrl(first.user_id), {
      account_id: first.user_id,
      email: first.email,
      rows: [row()],
    });
    const model = new SharedWorkspacesModel(deps);

    model.syncAccounts([first]);
    model.syncAccounts([first]);
    await settle();
    expect(deps.getUrls).toEqual([accountSharesUrl(first.user_id)]);

    model.syncAccounts([first, second]);
    await settle();
    expect(deps.getUrls).toEqual([
      accountSharesUrl(first.user_id),
      accountSharesUrl(first.user_id),
      accountSharesUrl(second.user_id),
    ]);
    expect(model.sections.map((section) => section.email)).toEqual([first.email, second.email]);
  });

  it("removes a row through the account's share route and re-reads that section", async () => {
    const account = accountEntry({ email: "person@example.com" });
    const deps = new FakeDeps();
    const gone = row({ host_id: "host-" + "b".repeat(32), record_state: "missing", is_likely_leaked: true });
    deps.responseByUrl.set(accountSharesUrl(account.user_id), {
      account_id: account.user_id,
      email: account.email,
      rows: [row(), gone],
    });
    const model = new SharedWorkspacesModel(deps);
    await model.load([account]);
    model.armedHostId = gone.host_id;
    deps.responseByUrl.set(accountSharesUrl(account.user_id), {
      account_id: account.user_id,
      email: account.email,
      rows: [row()],
    });

    await model.remove(model.sections[0], gone.host_id);

    expect(deps.deleteUrls).toEqual([`${accountSharesUrl(account.user_id)}/${gone.host_id}`]);
    expect(model.armedHostId).toBeNull();
    expect(model.removingHostIds.size).toBe(0);
    expect(model.removeFailure).toBeNull();
    expect(model.sections[0].rows?.map((entry) => entry.host_id)).toEqual([row().host_id]);
  });

  it("keeps the row and says the removal failed in our words, with the server's behind the details", async () => {
    const account = accountEntry({ email: "person@example.com" });
    const deps = new FakeDeps();
    deps.responseByUrl.set(accountSharesUrl(account.user_id), {
      account_id: account.user_id,
      email: account.email,
      rows: [row()],
    });
    deps.deleteResult = {
      ok: false,
      status: 502,
      body: { error: "Could not remove this share: the connector is down.", reason: "CONNECTOR_FAILED" },
    };
    const model = new SharedWorkspacesModel(deps);
    await model.load([account]);

    await model.remove(model.sections[0], row().host_id);

    expect(model.removeFailure?.error).toEqual({
      message: "Couldn't remove this share. Try again. If this persists, report a bug.",
      details: expect.stringContaining("the connector is down"),
    });
    expect(model.removeFailure?.isBugReportOffered).toBe(true);
    expect(model.sections[0].rows).toHaveLength(1);
  });

  it("asks an account whose Imbue Cloud sign-in expired to sign in again", async () => {
    const account = accountEntry({ email: "person@example.com" });
    const deps = new FakeDeps();
    deps.failedRead = {
      ok: false,
      status: 502,
      body: { error: "Could not list this account's shares: session expired", reason: "SIGN_IN_EXPIRED" },
    };
    const model = new SharedWorkspacesModel(deps);

    await model.load([account]);

    expect(model.sections[0].failure?.error.message).toBe(
      "Your Imbue Cloud sign-in has expired. *Sign in again, then try again.*",
    );
    expect(model.sections[0].failure?.remedies).toEqual(["sign_in"]);
    expect(model.sections[0].failure?.isBugReportOffered).toBe(false);
  });

  it("reads a failed section again on Try again", async () => {
    const account = accountEntry({ email: "person@example.com" });
    const deps = new FakeDeps();
    const model = new SharedWorkspacesModel(deps);
    await model.load([account]);
    expect(model.sections[0].failure).not.toBeNull();
    deps.responseByUrl.set(accountSharesUrl(account.user_id), {
      account_id: account.user_id,
      email: account.email,
      rows: [row()],
    });

    await model.reloadSection(model.sections[0]);

    expect(model.sections[0].failure).toBeNull();
    expect(model.sections[0].rows).toHaveLength(1);
  });
});
