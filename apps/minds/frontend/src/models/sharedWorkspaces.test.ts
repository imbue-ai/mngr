import { describe, expect, it } from "vitest";
import { accountEntry, secondAccountEntry, settle, sharedWorkspaceRow as row } from "../testing";
import {
  accountSharesUrl,
  SharedWorkspacesModel,
  type SharedWorkspacesDeps,
} from "./sharedWorkspaces";

class FakeDeps implements SharedWorkspacesDeps {
  responseByUrl = new Map<string, unknown | null>();
  getUrls: string[] = [];
  deleteUrls: string[] = [];
  deleteResult: { status: number; json: unknown | null } = { status: 200, json: { outcome: "removed" } };
  redrawCount = 0;

  async getJson(url: string): Promise<unknown | null> {
    this.getUrls.push(url);
    return this.responseByUrl.get(url) ?? null;
  }

  async deleteJson(url: string): Promise<{ status: number; json: unknown | null }> {
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
    expect(model.sections[1].isUnavailable).toBe(true);
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
    expect(model.errorMessage).toBeNull();
    expect(model.sections[0].rows?.map((entry) => entry.host_id)).toEqual([row().host_id]);
  });

  it("keeps the row and reports the server's reason when a removal is refused", async () => {
    const account = accountEntry({ email: "person@example.com" });
    const deps = new FakeDeps();
    deps.responseByUrl.set(accountSharesUrl(account.user_id), {
      account_id: account.user_id,
      email: account.email,
      rows: [row()],
    });
    deps.deleteResult = { status: 502, json: { error: "Could not remove this share: the connector is down." } };
    const model = new SharedWorkspacesModel(deps);
    await model.load([account]);

    await model.remove(model.sections[0], row().host_id);

    expect(model.errorMessage).toBe("Could not remove this share: the connector is down.");
    expect(model.sections[0].rows).toHaveLength(1);
  });
});
