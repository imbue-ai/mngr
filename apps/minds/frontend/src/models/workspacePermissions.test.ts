import { describe, expect, it } from "vitest";
import { settle } from "../testing";
import {
  BROWSER_SIGN_IN,
  awsAvailable,
  awsConnection,
  credentialsSignIn,
  permissionsView,
  slackConnection,
} from "./workspacePermissions.testing";
import {
  ADD_CONNECTION_SECTION,
  LOCAL_FILES_SECTION,
  PermissionsModel,
  connectActionFor,
  connectServiceRowKey,
  connectionSectionId,
  connectorToggleRowKey,
  disconnectRowKey,
  isCredentialFormComplete,
  resolvePermissionsSection,
  revokeAllRowKey,
  selfToggleRowKey,
} from "./workspacePermissions";

const AGENT_ID = "agent-" + "a".repeat(8);
const PERMISSIONS_URL = `/ui/api/workspaces/${AGENT_ID}/permissions`;

interface RecordedRequest {
  url: string;
  method: string;
  body: unknown;
}

interface StubResponse {
  ok: boolean;
  status: number;
  body: unknown;
}

function makeModel(responder: (url: string, init?: RequestInit) => StubResponse | Promise<StubResponse>): {
  model: PermissionsModel;
  requests: RecordedRequest[];
  redrawCount: () => number;
} {
  const requests: RecordedRequest[] = [];
  let redraws = 0;
  const model = new PermissionsModel(AGENT_ID, {
    fetchJson: (url: string, init?: RequestInit) => {
      requests.push({
        url,
        method: init?.method ?? "GET",
        body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null,
      });
      return Promise.resolve(responder(url, init));
    },
    redraw: () => {
      redraws += 1;
    },
  });
  return { model, requests, redrawCount: () => redraws };
}

function okWith(body: unknown): StubResponse {
  return { ok: true, status: 200, body };
}

describe("PermissionsModel loading", () => {
  it("reads the workspace's permissions and holds the view", async () => {
    const view = permissionsView();
    const { model, requests, redrawCount } = makeModel(() => okWith(view));

    await model.load();

    expect(requests).toEqual([{ url: PERMISSIONS_URL, method: "GET", body: null }]);
    expect(model.status).toBe("ready");
    expect(model.data).toEqual(view);
    expect(model.errorMessage).toBe("");
    expect(redrawCount()).toBeGreaterThan(0);
  });

  it("surfaces the server's message and keeps no data when the read fails", async () => {
    const { model } = makeModel(() => ({ ok: false, status: 503, body: { error: "gateway is down" } }));

    await model.load();

    expect(model.status).toBe("load_failed");
    expect(model.data).toBeNull();
    expect(model.errorMessage).toBe("gateway is down");
  });

  it("reads once however often the tab is reopened", async () => {
    const { model, requests } = makeModel(() => okWith(permissionsView()));

    model.ensureLoaded();
    await settle();
    model.ensureLoaded();
    model.ensureLoaded();
    await settle();

    expect(requests).toHaveLength(1);
  });

  it("keeps the unavailable flag rather than degrading it to an empty tree", async () => {
    const { model } = makeModel(() =>
      okWith(permissionsView({ connections: [], available_connections: [], permissions_unavailable: true })),
    );

    await model.load();

    expect(model.status).toBe("ready");
    expect(model.data?.permissions_unavailable).toBe(true);
  });
});

describe("PermissionsModel writes", () => {
  it("posts only the flipped connector permission and adopts the returned view", async () => {
    const refreshed = permissionsView({ connections: [slackConnection({ granted_count: 2 })] });
    const { model, requests } = makeModel((url) =>
      okWith(url.endsWith("/connector-toggle") ? refreshed : permissionsView()),
    );
    await model.load();

    await model.toggleConnector("slack-api", "", "slack-chat-read", true);

    expect(requests[1]).toEqual({
      url: `${PERMISSIONS_URL}/connector-toggle`,
      method: "POST",
      body: { scope: "slack-api", account: "", permission: "slack-chat-read", enabled: true },
    });
    expect(model.data).toEqual(refreshed);
    expect(model.errorMessage).toBe("");
  });

  it("posts a self toggle without any connector fields", async () => {
    const { model, requests } = makeModel(() => okWith(permissionsView()));
    await model.load();

    await model.toggleSelf("shared-path-1", false);

    expect(requests[1]).toEqual({
      url: `${PERMISSIONS_URL}/self-toggle`,
      method: "POST",
      body: { permission: "shared-path-1", enabled: false },
    });
  });

  it("posts the service and account on revoke all", async () => {
    const { model, requests } = makeModel(() => okWith(permissionsView({ connections: [] })));
    await model.load();

    await model.revokeAll("slack", "work@example.com", "Slack");

    expect(requests[1]).toEqual({
      url: `${PERMISSIONS_URL}/connector-revoke-all`,
      method: "POST",
      body: { service_name: "slack", account: "work@example.com" },
    });
    expect(model.data?.connections).toEqual([]);
  });

  it("posts the service and account on disconnect and lands on what is left", async () => {
    // Disconnecting is not machine-scoped: the server clears the credential and
    // strips the account everywhere, so the connection leaves the view and the
    // pane has to be told where to go.
    const { model, requests } = makeModel((url) =>
      okWith(url.endsWith("/connector-disconnect") ? permissionsView({ connections: [] }) : permissionsView()),
    );
    await model.load();

    const section = await model.disconnect(slackConnection({ account: "work@example.com" }));

    expect(requests[1]).toEqual({
      url: `${PERMISSIONS_URL}/connector-disconnect`,
      method: "POST",
      body: { service_name: "slack", account: "work@example.com" },
    });
    expect(section).toBe(ADD_CONNECTION_SECTION);
    expect(model.data?.connections).toEqual([]);
  });

  it("lands on the connection left standing when the service had another account", async () => {
    const other = slackConnection({ service_name: "notion", display_name: "Notion" });
    const { model } = makeModel((url) =>
      okWith(
        url.endsWith("/connector-disconnect")
          ? permissionsView({ connections: [other] })
          : permissionsView({ connections: [slackConnection(), other] }),
      ),
    );
    await model.load();

    expect(await model.disconnect(slackConnection())).toBe("conn:notion:");
  });

  it("keeps the pane where it is when the disconnect is refused", async () => {
    const loaded = permissionsView();
    const { model } = makeModel((url) =>
      url.endsWith("/connector-disconnect")
        ? { ok: false, status: 502, body: { error: "keychain is locked" } }
        : okWith(loaded),
    );
    await model.load();

    const section = await model.disconnect(slackConnection({ account_label: "work@example.com" }));

    expect(section).toBeNull();
    expect(model.errorMessage).toBe("Could not disconnect work@example.com from Slack: keychain is locked");
    expect(model.data).toEqual(loaded);
  });

  it("leaves the last good view on screen when a write is refused", async () => {
    const loaded = permissionsView();
    const { model } = makeModel((url) =>
      url.endsWith("/self-toggle") ? { ok: false, status: 502, body: { error: "gateway refused" } } : okWith(loaded),
    );
    await model.load();

    await model.toggleSelf("shared-path-1", true);

    expect(model.data).toEqual(loaded);
    expect(model.status).toBe("ready");
    expect(model.errorMessage).toBe("Could not save the change: gateway refused");
  });

  it("clears a stale error on request", async () => {
    const { model } = makeModel((url) =>
      url.endsWith("/self-toggle") ? { ok: false, status: 502, body: { error: "nope" } } : okWith(permissionsView()),
    );
    await model.load();
    await model.toggleSelf("shared-path-1", true);
    expect(model.errorMessage).not.toBe("");

    model.clearErrorMessage();

    expect(model.errorMessage).toBe("");
  });
});

describe("PermissionsModel write serialization", () => {
  it("sends overlapping flips in click order and leaves the last response on screen", async () => {
    const order: string[] = [];
    const resolvers: (() => void)[] = [];
    const { model, requests } = makeModel((url, init) => {
      if (!url.endsWith("/self-toggle")) return okWith(permissionsView());
      const permission = (JSON.parse(String(init?.body)) as { permission: string }).permission;
      order.push(permission);
      return new Promise<StubResponse>((resolve) => {
        resolvers.push(() => resolve(okWith(permissionsView({ host_id: permission }))));
      });
    });
    await model.load();

    const first = model.toggleSelf("first", true);
    const second = model.toggleSelf("second", true);
    await settle();

    // The second write has not reached the server yet: it waits its turn.
    expect(order).toEqual(["first"]);
    resolvers[0]();
    await settle();
    expect(order).toEqual(["first", "second"]);
    resolvers[1]();
    await Promise.all([first, second]);

    expect(requests.map((request) => request.url)).toEqual([
      PERMISSIONS_URL,
      `${PERMISSIONS_URL}/self-toggle`,
      `${PERMISSIONS_URL}/self-toggle`,
    ]);
    expect(model.data?.host_id).toBe("second");
  });

  it("drops a second click on a row whose write is still in flight", async () => {
    const releases: (() => void)[] = [];
    const { model, requests } = makeModel((url) => {
      if (!url.endsWith("/self-toggle")) return okWith(permissionsView());
      return new Promise<StubResponse>((resolve) => {
        releases.push(() => resolve(okWith(permissionsView())));
      });
    });
    await model.load();

    const first = model.toggleSelf("shared-path-1", true);
    await settle();
    expect(model.isRowBusy(selfToggleRowKey("shared-path-1"))).toBe(true);
    await model.toggleSelf("shared-path-1", false);

    expect(requests).toHaveLength(2);
    expect(releases).toHaveLength(1);
    releases[0]();
    await first;
    expect(model.isRowBusy(selfToggleRowKey("shared-path-1"))).toBe(false);
  });

  it("keeps a busy row from blocking a different row", async () => {
    const { model } = makeModel((url) => {
      if (!url.endsWith("/self-toggle")) return okWith(permissionsView());
      return new Promise<StubResponse>(() => undefined);
    });
    await model.load();

    void model.toggleSelf("first", true);
    await settle();

    expect(model.isRowBusy(selfToggleRowKey("first"))).toBe(true);
    expect(model.isRowBusy(selfToggleRowKey("second"))).toBe(false);
  });
});

describe("PermissionsModel pending-request reconciliation", () => {
  const waiting = (id: string) => ({ id, title: "Slack", reason: "", service_name: "slack" });

  it("re-reads when a request is resolved, so the strip and the toggles follow", async () => {
    const resolved = permissionsView({
      waiting_requests: [],
      connections: [slackConnection({ granted_count: 2 })],
    });
    let isResolved = false;
    const { model, requests } = makeModel(() =>
      okWith(isResolved ? resolved : permissionsView({ waiting_requests: [waiting("req-1")] })),
    );
    await model.load();
    // The first reconciliation only records what the load already answered with.
    await model.refreshIfPendingChanged(["req-1"]);
    expect(requests).toHaveLength(1);

    isResolved = true;
    await model.refreshIfPendingChanged([]);

    expect(requests.map((request) => request.url)).toEqual([PERMISSIONS_URL, PERMISSIONS_URL]);
    expect(model.data?.waiting_requests).toEqual([]);
    expect(model.data?.connections[0].granted_count).toBe(2);
  });

  it("stays put while the pending set is unchanged", async () => {
    const { model, requests } = makeModel(() => okWith(permissionsView()));
    await model.load();

    await model.refreshIfPendingChanged(["req-1"]);
    await model.refreshIfPendingChanged(["req-1"]);
    await model.refreshIfPendingChanged(["req-1"]);

    expect(requests).toHaveLength(1);
  });

  it("keeps the pane up rather than blanking it while the refresh runs", async () => {
    const releases: (() => void)[] = [];
    const { model } = makeModel((url, init) => {
      if (init === undefined && model.status === "ready")
        return new Promise<StubResponse>((resolve) => {
          releases.push(() => resolve(okWith(permissionsView({ host_id: "refreshed" }))));
        });
      return okWith(permissionsView());
    });
    await model.load();
    await model.refreshIfPendingChanged(["req-1"]);

    const refresh = model.refreshIfPendingChanged([]);
    await settle();

    expect(model.status).toBe("ready");
    expect(model.data?.host_id).not.toBe("refreshed");
    releases[0]();
    await refresh;
    expect(model.data?.host_id).toBe("refreshed");
  });

  it("waits its turn behind an in-flight flip rather than racing it", async () => {
    const releases: (() => void)[] = [];
    const { model, requests } = makeModel((url) => {
      if (url.endsWith("/self-toggle"))
        return new Promise<StubResponse>((resolve) => {
          releases.push(() => resolve(okWith(permissionsView({ host_id: "flipped" }))));
        });
      return okWith(permissionsView({ host_id: "refreshed" }));
    });
    await model.load();
    await model.refreshIfPendingChanged(["req-1"]);

    const flip = model.toggleSelf("shared-path-1", true);
    await settle();
    const refresh = model.refreshIfPendingChanged([]);
    await settle();

    // The refresh has not been issued yet: the flip owns the chain.
    expect(requests).toHaveLength(2);
    releases[0]();
    await Promise.all([flip, refresh]);
    expect(requests).toHaveLength(3);
    // The refresh ran last, so its view is the one on screen.
    expect(model.data?.host_id).toBe("refreshed");
  });

  it("says so inline when the refresh fails, leaving the last good view up", async () => {
    let isFailing = false;
    const { model } = makeModel(() =>
      isFailing
        ? { ok: false, status: 502, body: { error: "gateway is down" } }
        : okWith(permissionsView({ host_id: "good" })),
    );
    await model.load();
    await model.refreshIfPendingChanged(["req-1"]);

    isFailing = true;
    await model.refreshIfPendingChanged([]);

    expect(model.status).toBe("ready");
    expect(model.data?.host_id).toBe("good");
    expect(model.errorMessage).toBe("Could not refresh permissions: gateway is down");
  });
});

describe("PermissionsModel connect", () => {
  it("runs the sign-in then re-reads the pane so the new connection arrives", async () => {
    const withNotion = permissionsView({ connections: [slackConnection(), slackConnection({ service_name: "notion" })] });
    let isSignedIn = false;
    const { model, requests } = makeModel((url) => {
      if (url === "/settings/connectors/add-account") {
        isSignedIn = true;
        return okWith({});
      }
      return okWith(isSignedIn ? withNotion : permissionsView());
    });
    await model.load();

    await model.connectService("notion");

    expect(requests[1]).toEqual({
      url: "/settings/connectors/add-account",
      method: "POST",
      body: { service_name: "notion" },
    });
    expect(requests[2]?.url).toBe(PERMISSIONS_URL);
    expect(model.data).toEqual(withNotion);
    expect(model.isRowBusy(connectServiceRowKey("notion"))).toBe(false);
  });

  it("resolves to the account the sign-in added, not one the service already had", async () => {
    const first = slackConnection({ account: "first@example.com", show_account_label: true });
    const second = slackConnection({ account: "second@example.com", show_account_label: true });
    let isSignedIn = false;
    const { model } = makeModel((url) => {
      if (url === "/settings/connectors/add-account") {
        isSignedIn = true;
        return okWith({});
      }
      return okWith(permissionsView({ connections: isSignedIn ? [first, second] : [first] }));
    });
    await model.load();

    const section = await model.connectService("slack");

    expect(section).toBe("conn:slack:second@example.com");
  });

  it("takes its turn behind a flip made during the sign-in rather than undoing it", async () => {
    // The sign-in blocks for seconds and the pane stays live underneath it, so
    // an un-chained re-read could answer with the view from before the flip.
    const releases: (() => void)[] = [];
    const { model } = makeModel((url) => {
      if (url === "/settings/connectors/add-account")
        return new Promise<StubResponse>((resolve) => {
          releases.push(() => resolve(okWith({})));
        });
      if (url.endsWith("/self-toggle")) return okWith(permissionsView({ host_id: "flipped" }));
      return okWith(permissionsView({ host_id: "reloaded" }));
    });
    await model.load();

    const connect = model.connectService("notion");
    await settle();
    const flip = model.toggleSelf("shared-path-1", true);
    releases[0]();
    await Promise.all([connect, flip]);

    // The flip was queued behind the sign-in, so the reload runs after it and
    // its view is the one on screen -- never the pre-flip one.
    expect(model.data?.host_id).toBe("reloaded");
  });

  it("resolves to nothing when the reload carries no new connection for the service", async () => {
    const { model } = makeModel((url) =>
      url === "/settings/connectors/add-account" ? okWith({}) : okWith(permissionsView()),
    );
    await model.load();

    // The sign-in reported success but the service is absent from the reload:
    // there is nothing to show, and no other connection stands in for it.
    expect(await model.connectService("notion")).toBeNull();
  });

  it("reports a refused sign-in without re-reading", async () => {
    const loaded = permissionsView();
    const { model, requests } = makeModel((url) =>
      url === "/settings/connectors/add-account"
        ? { ok: false, status: 400, body: { error: "sign-in was cancelled" } }
        : okWith(loaded),
    );
    await model.load();

    const section = await model.connectService("notion");

    expect(section).toBeNull();
    expect(requests).toHaveLength(2);
    expect(model.data).toEqual(loaded);
    // A refused sign-in is raised as a popup, not as the inline banner the
    // load and toggle failures use: what the service says is an errand the
    // user has to run somewhere else, so it has to be read.
    expect(model.alertMessage).toBe("Could not connect: sign-in was cancelled");
    expect(model.errorMessage, "the inline banner is left for the other failures").toBe("");
  });

  it("keeps the connection failure up until the user dismisses it", () => {
    const { model } = makeModel(() => okWith(permissionsView()));
    model.alertMessage = "Could not connect: ask an administrator";

    // Switching section clears the inline error as noise; the popup is not
    // noise, so the same call must leave it standing.
    model.clearErrorMessage();
    expect(model.alertMessage).toBe("Could not connect: ask an administrator");

    model.dismissAlert();
    expect(model.alertMessage).toBe("");
  });
});

describe("connect action", () => {
  it("sends a browser service to the sign-in and a credential service to the form", () => {
    expect(connectActionFor(BROWSER_SIGN_IN)).toBe("browser_sign_in");
    expect(connectActionFor(credentialsSignIn())).toBe("credential_form");
  });

  it("offers nothing for a service with no sign-in and no inputs to ask for", () => {
    expect(connectActionFor(credentialsSignIn({ credential_parameters: [] }))).toBe("unconnectable");
  });

  it("holds the form back until every value, and any name it needs, is there", () => {
    const signIn = credentialsSignIn();
    expect(isCredentialFormComplete(signIn, { "access-key-id": "AKIA" }, "")).toBe(false);
    expect(isCredentialFormComplete(signIn, { "access-key-id": "AKIA", "secret-access-key": "   " }, "")).toBe(
      false,
    );
    expect(isCredentialFormComplete(signIn, { "access-key-id": "AKIA", "secret-access-key": "s3cret" }, "")).toBe(
      true,
    );
    const named = credentialsSignIn({ is_account_name_required: true });
    const values = { "access-key-id": "AKIA", "secret-access-key": "s3cret" };
    expect(isCredentialFormComplete(named, values, "")).toBe(false);
    expect(isCredentialFormComplete(named, values, "work")).toBe(true);
  });
});

describe("PermissionsModel connect with credentials", () => {
  const connected = permissionsView({
    connections: [slackConnection(), awsConnection()],
    available_connections: [],
  });

  it("posts what the form carries and lands on the connection it created", async () => {
    const { model, requests } = makeModel((url) =>
      url.endsWith("/connect-credentials") ? okWith(connected) : okWith(permissionsView()),
    );
    await model.load();
    model.openCredentialForm("aws");
    model.credentialValues = { "access-key-id": "AKIA", "secret-access-key": "s3cret" };
    model.credentialAccountName = "work";

    const section = await model.connectWithCredentials("aws");

    expect(requests[1]).toEqual({
      url: `${PERMISSIONS_URL}/connect-credentials`,
      method: "POST",
      body: {
        service_name: "aws",
        value_by_parameter_name: { "access-key-id": "AKIA", "secret-access-key": "s3cret" },
        account_name: "work",
      },
    });
    // The write answers with the refreshed view, so no re-read follows it.
    expect(requests).toHaveLength(2);
    expect(model.data).toEqual(connected);
    expect(section).toBe("conn:aws:");
    expect(model.credentialFormServiceName).toBeNull();
    expect(model.isRowBusy(connectServiceRowKey("aws"))).toBe(false);
  });

  it("keeps the form and everything typed when a credential is refused", async () => {
    const loaded = permissionsView({ available_connections: [awsAvailable()] });
    const { model } = makeModel((url) =>
      url.endsWith("/connect-credentials")
        ? { ok: false, status: 400, body: { error: "AWS rejected those credentials: bad key id" } }
        : okWith(loaded),
    );
    await model.load();
    model.openCredentialForm("aws");
    model.credentialValues = { "access-key-id": "AKIA", "secret-access-key": "wrong" };

    const section = await model.connectWithCredentials("aws");

    expect(section).toBeNull();
    expect(model.credentialErrorMessage).toBe("AWS rejected those credentials: bad key id");
    expect(model.credentialFormServiceName).toBe("aws");
    expect(model.credentialValues).toEqual({ "access-key-id": "AKIA", "secret-access-key": "wrong" });
    // The refusal belongs next to the fields, not to the pane at large.
    expect(model.errorMessage).toBe("");
    expect(model.data).toEqual(loaded);
  });

  it("resolves to nothing when the stored credentials produced no new connection", async () => {
    const { model } = makeModel(() => okWith(permissionsView()));
    await model.load();
    model.openCredentialForm("aws");

    expect(await model.connectWithCredentials("aws")).toBeNull();
  });

  it("opens each form empty, and drops what was typed when it is closed", async () => {
    const { model } = makeModel(() => okWith(permissionsView()));
    await model.load();
    model.openCredentialForm("aws");
    model.credentialValues = { "access-key-id": "AKIA" };
    model.credentialAccountName = "work";

    model.openCredentialForm("coolify");

    expect(model.credentialFormServiceName).toBe("coolify");
    expect(model.credentialValues).toEqual({});
    expect(model.credentialAccountName).toBe("");

    model.closeCredentialForm();

    expect(model.credentialFormServiceName).toBeNull();
  });
});

describe("permission section keys", () => {
  it("keys a connection by service and account, not by position", () => {
    expect(connectionSectionId(slackConnection())).toBe("conn:slack:");
    expect(connectionSectionId(slackConnection({ account: "work@example.com" }))).toBe(
      "conn:slack:work@example.com",
    );
  });

  it("keeps a requested section only while it still exists", () => {
    const view = permissionsView();
    expect(resolvePermissionsSection(view, "conn:slack:")).toBe("conn:slack:");
    expect(resolvePermissionsSection(view, LOCAL_FILES_SECTION)).toBe(LOCAL_FILES_SECTION);
    // A connection that was revoked out from under the deep link.
    expect(resolvePermissionsSection(view, "conn:notion:")).toBe("conn:slack:");
    expect(resolvePermissionsSection(view, null)).toBe("conn:slack:");
  });

  it("falls back to Add connection when there is nothing connected", () => {
    const empty = permissionsView({ connections: [] });
    expect(resolvePermissionsSection(empty, null)).toBe(ADD_CONNECTION_SECTION);
    expect(resolvePermissionsSection(null, null)).toBe(ADD_CONNECTION_SECTION);
  });

  it("gives each control kind its own busy identity", () => {
    const keys = [
      connectorToggleRowKey("slack-api", "", "read"),
      selfToggleRowKey("read"),
      revokeAllRowKey("slack", ""),
      disconnectRowKey("slack", ""),
      connectServiceRowKey("slack"),
    ];
    expect(new Set(keys).size).toBe(keys.length);
    // Parts are separated, so no run of names can collide with another key.
    expect(connectorToggleRowKey("a", "b", "c")).not.toBe(connectorToggleRowKey("a b", "", "c"));
  });
});
