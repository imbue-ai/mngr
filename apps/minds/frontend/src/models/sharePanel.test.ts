import { describe, expect, it } from "vitest";
import { settle, sharePanelOptions } from "../testing";
import { createAppQueryClient } from "./queryClient";
import type {
  MachineSharingResponse,
  SharingGrantList,
  SharingGrantsDocument,
} from "./workspaceOptions";
import { RESOLVE_USER_URL } from "./workspaceOptions";
import { SharePanelModel } from "./sharePanel";
import type { Grant, SharePanelModelOptions } from "./sharePanel";

const WHOLE = "system_interface";

interface FetchResult {
  ok: boolean;
  status: number;
  body: unknown;
}

interface RecordedRequest {
  url: string;
  method: string;
  body: unknown;
}

type Responder = (
  url: string,
  init?: RequestInit,
) => FetchResult | Promise<FetchResult>;

interface Deferred {
  promise: Promise<FetchResult>;
  resolve: (result: FetchResult) => void;
}

/** A promise a test settles when it wants the write it stands for to land. */
function deferred(): Deferred {
  let resolve: (result: FetchResult) => void = () => undefined;
  const promise = new Promise<FetchResult>((resolveResult) => {
    resolve = resolveResult;
  });
  return { promise, resolve };
}

function makeSharePanel(
  responder: Responder,
  overrides: Partial<SharePanelModelOptions> = {},
): { model: SharePanelModel; requests: RecordedRequest[] } {
  const requests: RecordedRequest[] = [];
  const model = new SharePanelModel(
    sharePanelOptions({
      fetchJson: (url: string, init?: RequestInit) => {
        requests.push({
          url,
          method: init?.method ?? "GET",
          body:
            typeof init?.body === "string"
              ? (JSON.parse(init.body) as unknown)
              : null,
        });
        return Promise.resolve(responder(url, init));
      },
      ...overrides,
    }),
  );
  return { model, requests };
}

function grantList(
  overrides: Partial<SharingGrantList> = {},
): SharingGrantList {
  return { users: [], emails: [], email_domains: [], ...overrides };
}

function grantsDocument(
  workspace: Partial<SharingGrantList> = {},
  services: Record<string, SharingGrantList> = {},
): SharingGrantsDocument {
  return { workspace: grantList(workspace), services };
}

function sharingResponse(
  overrides: Partial<MachineSharingResponse> = {},
): MachineSharingResponse {
  return {
    enabled: true,
    url: "https://m.relay.example/",
    grants: grantsDocument(),
    ...overrides,
  };
}

function okResult(body: unknown): FetchResult {
  return { ok: true, status: 200, body };
}

/** The 400 the grants route answers with when it refuses one domain. */
function refusalResult(domain: string): FetchResult {
  return {
    ok: false,
    status: 400,
    body: {
      error: "grant_refused",
      refusals: [
        {
          scope: "workspace",
          kind: "email_domain",
          value: domain,
          message: `${domain} cannot be granted permissions because it is a public email provider.`,
        },
      ],
    },
  };
}

/** The document a grants write is sending, for a stub that echoes it back the
 * way the route does. */
function sentDocument(init: RequestInit | undefined): SharingGrantsDocument {
  return (JSON.parse(init?.body as string) as { grants: SharingGrantsDocument })
    .grants;
}

/** The whole document a recorded grants write carried. */
function writtenDocument(request: RecordedRequest): SharingGrantsDocument {
  return (request.body as { grants: SharingGrantsDocument }).grants;
}

function grantsWrites(requests: RecordedRequest[]): RecordedRequest[] {
  return requests.filter(
    (request) => request.method === "PUT" && request.url.endsWith("/grants"),
  );
}

function grantValues(
  model: SharePanelModel,
  target = WHOLE,
): (string | null)[] {
  return model.grantsFor(target).map((grant) => grant.grantee.value);
}

/** The words a row shows for its failure, or null while it has not failed. */
function failureMessageOf(grant: Grant): string | null {
  return grant.status.state === "failed" ? grant.status.failureMessage : null;
}

/** The labels of the provisioning steps the panel reports as done. */
function stepsDone(model: SharePanelModel): string[] {
  return model.provisioningSteps
    .filter((step) => step.isDone)
    .map((step) => step.label);
}

describe("SharePanelModel load", () => {
  it("keys the sharing API by the workspace id when one is known", async () => {
    const { model, requests } = makeSharePanel(
      () => okResult(sharingResponse()),
      { agentId: "agent-" + "b".repeat(32) },
    );

    await model.load();

    expect(requests[0].url).toBe(
      "/api/v1/workspace-sharing/agent-" + "b".repeat(32),
    );
    expect(model.loadStatus).toBe("ready");
  });

  it("reads the grants of an unpublished workspace", async () => {
    const { model } = makeSharePanel(() =>
      okResult(
        sharingResponse({
          enabled: false,
          url: null,
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      ),
    );

    await model.load();

    expect(model.isPublished).toBe(false);
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });

  it("re-runs a failed removal while publishing is off", async () => {
    let writeCount = 0;
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "DELETE")
        return okResult(
          sharingResponse({
            enabled: false,
            url: null,
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        );
      if (init?.method === "PUT") {
        writeCount += 1;
        if (writeCount === 1) return { ok: false, status: 500, body: {} };
        return okResult(
          sharingResponse({
            enabled: false,
            url: null,
            grants: sentDocument(init),
          }),
        );
      }
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();
    await model.unpublish();
    expect(model.canAdd).toBe(false);

    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[0].key);
    await settle();
    const restored = model.grantsFor(WHOLE)[0];
    expect(restored.status).toEqual({
      state: "failed",
      failedOperation: "remove",
      failureMessage: "Could not save",
    });

    model.retryGrant(WHOLE, restored.key);
    await settle();

    expect(grantValues(model)).toEqual([]);
    expect(writtenDocument(grantsWrites(requests)[1]).workspace.emails).toEqual(
      [],
    );
  });

  it("refuses to edit against grants the backend could not read", async () => {
    const { model, requests } = makeSharePanel(() =>
      okResult(sharingResponse({ grants: null })),
    );

    await model.load();

    expect(model.loadStatus).toBe("load_failed");
    expect(model.loadErrorMessage).toContain("could not be loaded");
    // Adding against a list nobody has seen would replace it.
    expect(model.canAdd).toBe(false);

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    expect(model.grantsFor(WHOLE)).toHaveLength(0);
    expect(grantsWrites(requests)).toHaveLength(0);
  });

  it("offers no retry of a row while the stored list is unknown", async () => {
    let isFirstRead = true;
    const { model, requests } = makeSharePanel(() => {
      if (isFirstRead) {
        isFirstRead = false;
        return okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        );
      }
      return okResult(sharingResponse({ grants: null }));
    });
    await model.load();
    const key = model.grantsFor(WHOLE)[0].key;
    await model.load();
    expect(model.canAdd).toBe(false);

    model.retryGrant(WHOLE, key);
    await settle();

    expect(grantsWrites(requests)).toHaveLength(0);
  });
});

describe("SharePanelModel adding a grant", () => {
  it("shows the row at once and settles it when the write lands", async () => {
    const write = deferred();
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return write.promise;
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");

    expect(grantValues(model)).toEqual(["friend@example.com"]);
    expect(model.grantsFor(WHOLE)[0].status.state).toBe("saving");
    await settle();
    expect(grantsWrites(requests)).toHaveLength(1);
    expect(writtenDocument(grantsWrites(requests)[0]).workspace.emails).toEqual(
      ["friend@example.com"],
    );

    write.resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      ),
    );
    await settle();

    expect(model.grantsFor(WHOLE)[0].status.state).toBe("settled");
  });

  it("saves a grant on the app target it was added to", async () => {
    const { model, requests } = makeSharePanel(() =>
      okResult(sharingResponse()),
    );
    await model.load();

    model.addGrant("web", "email", "friend@example.com");
    await settle();

    const document = writtenDocument(grantsWrites(requests)[0]);
    expect(document.services["web"].emails).toEqual(["friend@example.com"]);
    expect(document.workspace.emails).toEqual([]);
    expect(model.grantCount("web")).toBe(1);
    expect(model.grantCount(WHOLE)).toBe(0);
  });

  it("grants nothing while publishing is off", async () => {
    const { model, requests } = makeSharePanel(() =>
      okResult(sharingResponse({ enabled: false, url: null })),
    );
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    expect(model.grantsFor(WHOLE)).toHaveLength(0);
    expect(grantsWrites(requests)).toHaveLength(0);
    expect(model.addRow(WHOLE).refusalMessage).toBeNull();
  });
});

describe("SharePanelModel write queue", () => {
  it("coalesces every edit made during a write into exactly one further write", async () => {
    const writes: Deferred[] = [];
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        const write = deferred();
        writes.push(write);
        return write.promise;
      }
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "first@example.com");
    await settle();
    expect(writes).toHaveLength(1);

    model.addGrant(WHOLE, "email", "second@example.com");
    model.addGrant(WHOLE, "email_domain", "example.org");
    await settle();
    expect(writes).toHaveLength(1);

    writes[0].resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["first@example.com"] }),
        }),
      ),
    );
    await settle();

    expect(writes).toHaveLength(2);
    const second = writtenDocument(grantsWrites(requests)[1]);
    expect(second.workspace.emails).toEqual([
      "first@example.com",
      "second@example.com",
    ]);
    expect(second.workspace.email_domains).toEqual(["example.org"]);

    writes[1].resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({
            emails: ["first@example.com", "second@example.com"],
            email_domains: ["example.org"],
          }),
        }),
      ),
    );
    await settle();
    expect(writes).toHaveLength(2);
  });

  it("keeps a row added during a write that the write's readback cannot carry", async () => {
    const writes: Deferred[] = [];
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        const write = deferred();
        writes.push(write);
        return write.promise;
      }
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "first@example.com");
    await settle();
    model.addGrant(WHOLE, "email", "second@example.com");

    writes[0].resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["first@example.com"] }),
        }),
      ),
    );
    await settle();

    expect(grantValues(model)).toEqual([
      "first@example.com",
      "second@example.com",
    ]);
    expect(model.grantsFor(WHOLE)[0].status.state).toBe("settled");
    expect(model.grantsFor(WHOLE)[1].status.state).toBe("saving");
  });
});

describe("SharePanelModel write failures", () => {
  it("marks the rows a failed write carried and saves them again on retry", async () => {
    let isFirstWrite = true;
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        if (isFirstWrite) {
          isFirstWrite = false;
          return { ok: false, status: 500, body: { error: "boom" } };
        }
        return okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        );
      }
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    const row = model.grantsFor(WHOLE)[0];
    expect(row.status).toEqual({
      state: "failed",
      failedOperation: "add",
      failureMessage: "boom",
    });

    model.retryGrant(WHOLE, row.key);
    expect(model.grantsFor(WHOLE)[0].status.state).toBe("saving");
    await settle();

    expect(model.grantsFor(WHOLE)[0].status.state).toBe("settled");
    expect(grantsWrites(requests)).toHaveLength(2);
  });

  it("keeps the reason a failed write gave, on every row it carried", async () => {
    const REASON =
      "This workspace has no share gateway. Update the template, then publish again.";
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT")
        return { ok: false, status: 502, body: { error: REASON } };
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();

    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[0].key);
    model.addGrant(WHOLE, "email_domain", "acme.example");
    await settle();

    expect(model.grantsFor(WHOLE).map(failureMessageOf)).toEqual([
      REASON,
      REASON,
    ]);
  });

  it("reports a write that failed with no reason as one that could not save", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return { ok: false, status: 0, body: null };
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    expect(failureMessageOf(model.grantsFor(WHOLE)[0])).toBe("Could not save");
  });

  it("marks the row a refusal names and saves the rest of that write again without it", async () => {
    const writes: Deferred[] = [];
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        const write = deferred();
        writes.push(write);
        return write.promise;
      }
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email_domain", "example.net");
    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();
    expect(writes).toHaveLength(1);

    writes[0].resolve(refusalResult("example.net"));
    await settle();

    const refused = model.grantsFor(WHOLE)[0];
    expect(refused.grantee.value).toBe("example.net");
    expect(failureMessageOf(refused)).toContain("public email provider");
    // The refusal wrote nothing, so the address that rode it is still unsaved.
    expect(model.grantsFor(WHOLE)[1].status.state).toBe("saving");

    expect(writes).toHaveLength(2);
    const second = writtenDocument(grantsWrites(requests)[1]);
    expect(second.workspace.email_domains).toEqual([]);
    expect(second.workspace.emails).toEqual(["friend@example.com"]);

    writes[1].resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      ),
    );
    await settle();

    expect(model.grantsFor(WHOLE)[0].status.state).toBe("failed");
    expect(model.grantsFor(WHOLE)[1].status.state).toBe("settled");
  });

  it("keeps a removal the refused write carried and sends it again", async () => {
    const writes: Deferred[] = [];
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        const write = deferred();
        writes.push(write);
        return write.promise;
      }
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();

    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[0].key);
    model.addGrant(WHOLE, "email_domain", "example.net");
    await settle();

    writes[0].resolve(refusalResult("example.net"));
    await settle();

    expect(grantValues(model)).toEqual(["example.net"]);
    expect(writtenDocument(grantsWrites(requests)[1]).workspace.emails).toEqual(
      [],
    );
  });

  it("reports the rows of a refusal that names nothing it can drop", async () => {
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return refusalResult("elsewhere.example");
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    expect(model.grantsFor(WHOLE)[0].status.state).toBe("failed");
    expect(failureMessageOf(model.grantsFor(WHOLE)[0])).toBe("Could not save");
    expect(grantsWrites(requests)).toHaveLength(1);
  });
});

describe("SharePanelModel removing a grant", () => {
  it("drops the row at once and keeps it gone once the write lands", async () => {
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return okResult(sharingResponse());
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();
    const key = model.grantsFor(WHOLE)[0].key;

    model.removeGrant(WHOLE, key);

    expect(grantValues(model)).toEqual([]);
    await settle();
    expect(writtenDocument(grantsWrites(requests)[0]).workspace.emails).toEqual(
      [],
    );
    expect(grantValues(model)).toEqual([]);
  });

  it("returns the row marked failed when the removal cannot be saved", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return { ok: false, status: 0, body: null };
      return okResult(
        sharingResponse({
          grants: grantsDocument({
            emails: ["first@example.com", "second@example.com"],
          }),
        }),
      );
    });
    await model.load();

    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[0].key);
    await settle();

    expect(grantValues(model)).toEqual([
      "first@example.com",
      "second@example.com",
    ]);
    expect(model.grantsFor(WHOLE)[0].status.state).toBe("failed");
    expect(failureMessageOf(model.grantsFor(WHOLE)[0])).toBe("Could not save");
  });

  it("does not let a readback that raced a removal bring the row back", async () => {
    const writes: Deferred[] = [];
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        const write = deferred();
        writes.push(write);
        return write.promise;
      }
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();

    model.addGrant(WHOLE, "email_domain", "example.org");
    await settle();
    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[1].key);

    // The in-flight write was built before the removal, so its readback still
    // carries the removed address.
    writes[0].resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({
            emails: ["friend@example.com"],
            email_domains: ["example.org"],
          }),
        }),
      ),
    );
    await settle();
    expect(grantValues(model)).toEqual(["example.org"]);

    writes[1].resolve(
      okResult(
        sharingResponse({
          grants: grantsDocument({ email_domains: ["example.org"] }),
        }),
      ),
    );
    await settle();
    expect(grantValues(model)).toEqual(["example.org"]);
  });
});

describe("SharePanelModel row order", () => {
  it("puts domain grants first and sorts each group by name, across a readback", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT")
        return okResult(
          sharingResponse({
            grants: grantsDocument({
              emails: ["first@example.com", "second@example.com"],
              email_domains: ["example.org", "example.com"],
            }),
          }),
        );
      return okResult(
        sharingResponse({
          grants: grantsDocument({
            emails: ["first@example.com"],
            email_domains: ["example.org"],
          }),
        }),
      );
    });
    await model.load();
    expect(grantValues(model)).toEqual(["example.org", "first@example.com"]);

    model.addGrant(WHOLE, "email", "second@example.com");
    model.addGrant(WHOLE, "email_domain", "example.com");
    expect(grantValues(model)).toEqual([
      "example.com",
      "example.org",
      "first@example.com",
      "second@example.com",
    ]);

    await settle();

    expect(grantValues(model)).toEqual([
      "example.com",
      "example.org",
      "first@example.com",
      "second@example.com",
    ]);
  });

  it("sorts the rows a readback introduces in among the ones already there", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT")
        return okResult(
          sharingResponse({
            grants: grantsDocument({
              emails: ["mine@example.com", "theirs@example.com"],
              email_domains: ["example.org", "elsewhere.example"],
            }),
          }),
        );
      return okResult(
        sharingResponse({
          grants: grantsDocument({
            emails: ["mine@example.com"],
            email_domains: ["example.org"],
          }),
        }),
      );
    });
    await model.load();

    model.addGrant(WHOLE, "email", "later@example.com");
    await settle();

    expect(grantValues(model)).toEqual([
      "elsewhere.example",
      "example.org",
      "later@example.com",
      "mine@example.com",
      "theirs@example.com",
    ]);
  });
});

describe("SharePanelModel document building", () => {
  it("keeps the scopes of services the panel does not know about", async () => {
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return okResult(sharingResponse());
      return okResult(
        sharingResponse({
          grants: grantsDocument(
            {},
            { unregistered: grantList({ emails: ["other@example.com"] }) },
          ),
        }),
      );
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    const document = writtenDocument(grantsWrites(requests)[0]);
    expect(document.services["unregistered"].emails).toEqual([
      "other@example.com",
    ]);
  });
});

describe("SharePanelModel target selection", () => {
  it("counts grants per target and falls back to the whole workspace for an unknown one", async () => {
    const { model } = makeSharePanel(() =>
      okResult(
        sharingResponse({
          grants: grantsDocument(
            { emails: ["friend@example.com"], email_domains: ["example.org"] },
            { web: grantList({ emails: ["dev@example.com"] }) },
          ),
        }),
      ),
    );
    await model.load();

    expect(model.grantCount(WHOLE)).toBe(2);
    expect(model.grantCount("web")).toBe(1);
    expect(model.inheritedGrants().map((grant) => grant.key)).toEqual([
      "email_domain:example.org",
      "email:friend@example.com",
    ]);

    model.selectTarget("web");
    expect(model.currentTarget).toBe("web");
    model.selectTarget("nonesuch");
    expect(model.currentTarget).toBe(WHOLE);
  });

  it("offers a link only for a target whose label the backend knows", async () => {
    const { model } = makeSharePanel(() => okResult(sharingResponse()));
    await model.load();

    expect(model.hasLink(WHOLE)).toBe(true);
    expect(model.targetUrl(WHOLE)).toBe("https://shell-r4nd.m.relay.example/");
    expect(model.hasLink("docs")).toBe(false);
    expect(model.targetUrl("docs")).toBeNull();
  });
});

describe("SharePanelModel publication state", () => {
  it("does not claim the switch is off before the status read answers", async () => {
    const read = deferred();
    const { model } = makeSharePanel(() => read.promise);

    const loading = model.load();

    expect(model.isPublicationKnown).toBe(false);
    expect(model.isCheckingPublication).toBe(true);

    read.resolve(okResult(sharingResponse()));
    await loading;

    expect(model.isPublicationKnown).toBe(true);
    expect(model.isCheckingPublication).toBe(false);
    expect(model.isPublished).toBe(true);
  });

  it("leaves the switch undrawn, not off, when the first read fails", async () => {
    const { model } = makeSharePanel(() => ({
      ok: false,
      status: 502,
      body: { error: "relay down" },
    }));

    await model.load();

    expect(model.isPublicationKnown).toBe(false);
    expect(model.isCheckingPublication).toBe(false);
    expect(model.loadStatus).toBe("load_failed");
  });

  it("draws what an earlier panel read at once, and reads again behind it", async () => {
    const queryClient = createAppQueryClient();
    const earlier = makeSharePanel(
      () =>
        okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        ),
      { queryClient },
    );
    await earlier.model.load();
    earlier.model.dispose();
    const read = deferred();
    const { model, requests } = makeSharePanel(() => read.promise, {
      queryClient,
    });

    const loading = model.load();

    expect(model.isPublicationKnown).toBe(true);
    expect(model.isPublished).toBe(true);
    expect(model.isLive).toBe(true);
    expect(model.isRevalidating).toBe(true);
    expect(model.loadStatus).toBe("ready");
    expect(grantValues(model)).toEqual(["friend@example.com"]);

    read.resolve(okResult(sharingResponse({ enabled: false, url: null })));
    await loading;

    const statusReads = requests.filter(
      (request) => !request.url.endsWith("/invitation-outcomes"),
    );
    expect(statusReads).toHaveLength(1);
    expect(model.isRevalidating).toBe(false);
    expect(model.isPublished).toBe(false);
    expect(grantValues(model)).toEqual([]);
  });

  it("cancels the read behind a cached switch when it is thrown, so a stale answer cannot move it back", async () => {
    const queryClient = createAppQueryClient();
    const earlier = makeSharePanel(
      () => okResult(sharingResponse({ enabled: false, url: null })),
      { queryClient },
    );
    await earlier.model.load();
    earlier.model.dispose();
    const read = deferred();
    const { model } = makeSharePanel(
      (url, init) =>
        init?.method === "PUT" ? okResult(sharingResponse()) : read.promise,
      { queryClient },
    );
    const loading = model.load();
    expect(model.isPublished).toBe(false);
    expect(model.isRevalidating).toBe(true);

    await model.publish();

    expect(model.isPublished).toBe(true);
    expect(model.isRevalidating).toBe(false);

    read.resolve(okResult(sharingResponse({ enabled: false, url: null })));
    await loading;

    expect(model.isPublished).toBe(true);
    expect(model.machineUrl).toBe("https://m.relay.example/");
    expect(model.loadStatus).toBe("ready");
  });

  it("starts a later panel from the publication the switch reached", async () => {
    const queryClient = createAppQueryClient();
    const earlier = makeSharePanel(
      (url, init) =>
        okResult(
          init?.method === "PUT"
            ? sharingResponse()
            : sharingResponse({ enabled: false, url: null }),
        ),
      { queryClient },
    );
    await earlier.model.load();
    await earlier.model.publish();
    earlier.model.dispose();
    const { model } = makeSharePanel(() => new Promise(() => undefined), {
      queryClient,
    });

    void model.load();

    expect(model.isPublished).toBe(true);
    expect(model.machineUrl).toBe("https://m.relay.example/");
  });

  it("shows a toggle an earlier panel left in flight, and takes its answer", async () => {
    const queryClient = createAppQueryClient();
    const write = deferred();
    const earlier = makeSharePanel(
      (url, init) =>
        init?.method === "PUT"
          ? write.promise
          : okResult(sharingResponse({ enabled: false, url: null })),
      { queryClient },
    );
    await earlier.model.load();
    const publishing = earlier.model.publish();
    earlier.model.dispose();
    const { model } = makeSharePanel(() => new Promise(() => undefined), {
      queryClient,
    });
    void model.load();

    expect(model.isPublished).toBe(true);
    expect(model.publishWrite).toEqual({ state: "publishing" });
    expect(model.isLive).toBe(false);

    write.resolve(okResult(sharingResponse()));
    await publishing;

    expect(model.isPublished).toBe(true);
    expect(model.publishWrite).toEqual({ state: "idle" });
    expect(model.machineUrl).toBe("https://m.relay.example/");
    expect(model.isAwaitingLink).toBe(true);
  });
});

describe("SharePanelModel publishing", () => {
  it("flips the switch before the publish write resolves", async () => {
    const write = deferred();
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") return write.promise;
      return okResult(sharingResponse({ enabled: false, url: null }));
    });
    await model.load();
    expect(model.canAdd).toBe(false);

    const publishing = model.publish();

    expect(model.isPublished).toBe(true);
    expect(model.publishWrite).toEqual({ state: "publishing" });
    expect(model.canAdd).toBe(true);

    write.resolve(okResult(sharingResponse()));
    await publishing;

    expect(model.publishWrite).toEqual({ state: "idle" });
    expect(model.machineUrl).toBe("https://m.relay.example/");
  });

  it("flips the switch back and keeps the reason when the publish fails", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT")
        return {
          ok: false,
          status: 502,
          body: { error: "the connector refused the share" },
        };
      return okResult(sharingResponse({ enabled: false, url: null }));
    });
    await model.load();

    await model.publish();

    expect(model.isPublished).toBe(false);
    expect(model.publishWrite).toEqual({
      state: "failed",
      message: expect.stringContaining("the connector refused the share"),
    });
  });

  it("keeps every grant when publishing is turned off", async () => {
    const { model } = makeSharePanel((url, init) => {
      const grants = grantsDocument({
        emails: ["friend@example.com"],
        email_domains: ["example.org"],
      });
      if (init?.method === "DELETE")
        return okResult(sharingResponse({ enabled: false, url: null, grants }));
      return okResult(sharingResponse({ grants }));
    });
    await model.load();

    await model.unpublish();

    expect(model.isPublished).toBe(false);
    expect(model.publishWrite).toEqual({ state: "idle" });
    expect(model.canAdd).toBe(false);
    expect(grantValues(model)).toEqual(["example.org", "friend@example.com"]);
  });

  it("flips the switch back on when the unpublish fails", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "DELETE")
        return { ok: false, status: 502, body: { error: "relay unreachable" } };
      return okResult(sharingResponse());
    });
    await model.load();

    await model.unpublish();

    expect(model.isPublished).toBe(true);
    expect(model.publishWrite).toEqual({
      state: "failed",
      message: expect.stringContaining("relay unreachable"),
    });
  });

  it("does not let a grants readback undo a publish still in flight", async () => {
    const publishWrite = deferred();
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT" && url.endsWith("/grants"))
        return okResult(sharingResponse({ enabled: false, url: null }));
      if (init?.method === "PUT") return publishWrite.promise;
      return okResult(sharingResponse({ enabled: false, url: null }));
    });
    await model.load();

    const publishing = model.publish();
    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    expect(model.isPublished).toBe(true);

    publishWrite.resolve(okResult(sharingResponse()));
    await publishing;
    expect(model.isPublished).toBe(true);
  });
});

describe("SharePanelModel provisioning", () => {
  /** A model that answers the readiness poll from `readinessBodies` in turn,
   * with the scheduled probes for the test to run and the timers it cleared. */
  function makeProvisioningPanel(readinessBodies: unknown[]): {
    model: SharePanelModel;
    scheduled: (() => void)[];
    cleared: number[];
    probeCount: () => number;
  } {
    const scheduled: (() => void)[] = [];
    const cleared: number[] = [];
    let probes = 0;
    const { model } = makeSharePanel(
      (url, init) => {
        if (url.endsWith("/readiness")) {
          const body =
            readinessBodies[Math.min(probes, readinessBodies.length - 1)];
          probes += 1;
          return okResult(body);
        }
        // The load and the unpublish answer unpublished; only the publish
        // hands back a link.
        if (init?.method === "PUT") return okResult(sharingResponse());
        return okResult(sharingResponse({ enabled: false, url: null }));
      },
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
        clearTimer: (timerId: number) => {
          cleared.push(timerId);
        },
      },
    );
    return { model, scheduled, cleared, probeCount: () => probes };
  }

  it("advances the checklist from the readiness poll", async () => {
    // The previous share's stamp is only a baseline: the relay step is done
    // when it CHANGES, not when one merely exists.
    const { model, scheduled } = makeProvisioningPanel([
      {
        ready: false,
        cert_not_after: null,
        last_tunnel_login_at: "2026-01-01 00:00:00",
      },
      {
        ready: false,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-01-01 00:00:00",
      },
      {
        ready: false,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-08-13 12:00:00",
      },
      {
        ready: true,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-08-13 12:00:00",
      },
    ]);
    await model.load();
    expect(stepsDone(model)).toEqual([]);
    expect(model.activeProvisioningStep.label).toBe("Creating link");

    await model.publish();

    expect(stepsDone(model)).toEqual(["Creating link"]);
    expect(model.activeProvisioningStep.label).toBe("Setting up encryption");
    expect(model.isAwaitingLink).toBe(true);

    scheduled.shift()?.();
    await settle();
    expect(stepsDone(model)).toEqual(["Creating link"]);

    scheduled.shift()?.();
    await settle();
    expect(stepsDone(model)).toEqual([
      "Creating link",
      "Setting up encryption",
    ]);
    expect(model.activeProvisioningStep.label).toBe("Connecting to the relay");

    scheduled.shift()?.();
    await settle();
    expect(stepsDone(model)).toEqual([
      "Creating link",
      "Setting up encryption",
      "Connecting to the relay",
    ]);
    expect(model.activeProvisioningStep.label).toBe("Verifying end to end");

    scheduled.shift()?.();
    await settle();
    expect(stepsDone(model)).toEqual([
      "Creating link",
      "Setting up encryption",
      "Connecting to the relay",
      "Verifying end to end",
    ]);
    expect(model.activeProvisioningStep.label).toBe("Verifying end to end");
    expect(model.isAwaitingLink).toBe(false);
  });

  it("surfaces a retrying gateway and keeps polling, then stops once it halts", async () => {
    const { model, scheduled } = makeProvisioningPanel([
      {
        ready: false,
        gateway_state: "retrying",
        gateway_error:
          "certificate provisioning failed: connector refused the CSR (503)",
        gateway_failed_attempt_count: 2,
        gateway_next_retry_at: "2026-09-13T12:01:00+00:00",
      },
      {
        ready: false,
        gateway_state: "halted",
        gateway_error:
          "certificate provisioning failed: connector refused the CSR (400)",
        gateway_failed_attempt_count: 3,
        gateway_next_retry_at: null,
      },
    ]);
    await model.load();
    await model.publish();

    scheduled.shift()?.();
    await settle();
    expect(model.gatewayState).toBe("retrying");
    expect(model.gatewayError).toContain("refused the CSR (503)");
    expect(model.gatewayFailedAttemptCount).toBe(2);
    expect(model.gatewayNextRetryAt).toBe("2026-09-13T12:01:00+00:00");
    expect(model.isProvisioningHalted).toBe(false);
    expect(scheduled).toHaveLength(1);

    // Halted: only publishing again would try, so the poll stops and the link
    // stays not-live with the reason beside it.
    scheduled.shift()?.();
    await settle();
    expect(model.isProvisioningHalted).toBe(true);
    expect(model.gatewayError).toContain("refused the CSR (400)");
    expect(model.isLive).toBe(false);
    expect(model.isAwaitingLink).toBe(true);
    expect(scheduled).toHaveLength(0);
  });

  it("forgets a gateway state the poll no longer reports", async () => {
    const { model, scheduled } = makeProvisioningPanel([
      {
        ready: false,
        gateway_state: "retrying",
        gateway_error: "still trying",
      },
      { ready: false },
    ]);
    await model.load();
    await model.publish();

    scheduled.shift()?.();
    await settle();
    expect(model.gatewayState).toBe("retrying");

    scheduled.shift()?.();
    await settle();

    expect(model.gatewayState).toBeNull();
    expect(model.gatewayError).toBeNull();
    expect(model.gatewayFailedAttemptCount).toBe(0);
    expect(model.gatewayNextRetryAt).toBeNull();
  });

  it("stops the provisioning poll when publishing is turned off", async () => {
    const { model, scheduled, cleared, probeCount } = makeProvisioningPanel([
      { ready: false },
    ]);
    await model.load();
    await model.publish();
    expect(scheduled).toHaveLength(1);

    await model.unpublish();

    expect(cleared).not.toHaveLength(0);
    const probesBeforeStop = probeCount();
    scheduled.shift()?.();
    await settle();
    expect(probeCount()).toBe(probesBeforeStop);
    expect(scheduled).toHaveLength(0);
  });

  it("keeps polling while an app's link has no label yet", async () => {
    const { model, scheduled } = makeProvisioningPanel([
      { ready: true, service_labels: { docs: "docs-r4nd" } },
    ]);
    await model.load();
    await model.publish();
    model.selectTarget("docs");
    expect(model.isAwaitingLabel("docs")).toBe(true);
    expect(model.hasLink("docs")).toBe(false);

    // Selecting a target restarts the poll, so the timer the previous target
    // left behind answers nothing; drain until the label lands.
    while (scheduled.length > 0 && !model.hasLink("docs")) {
      scheduled.shift()?.();
      await settle();
    }

    expect(model.isAwaitingLabel("docs")).toBe(false);
    expect(model.hasLink("docs")).toBe(true);
  });
});

describe("SharePanelModel injected clock and timers", () => {
  it("reports the clock and runs timers through the hooks it was given", async () => {
    const delays: number[] = [];
    const cancelled: number[] = [];
    let ranCount = 0;
    const { model } = makeSharePanel(() => okResult(sharingResponse()), {
      monotonicNowMs: () => 1234,
      setTimer: (callback: () => void, delayMs: number) => {
        delays.push(delayMs);
        callback();
        return delays.length;
      },
      clearTimer: (handle: number) => {
        cancelled.push(handle);
      },
    });

    expect(model.nowMs()).toBe(1234);
    const handle = model.schedule(() => {
      ranCount += 1;
    }, 50);
    model.cancel(handle);

    expect(delays).toEqual([50]);
    expect(ranCount).toBe(1);
    expect(cancelled).toEqual([handle]);
  });
});

describe("SharePanelModel validation before the row", () => {
  /** A panel whose workspace already grants friend@example.com. */
  async function loadedPanel(): Promise<SharePanelModel> {
    const { model } = makeSharePanel(() =>
      okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      ),
    );
    await model.load();
    return model;
  }

  it("refuses a public mail provider as a domain grant", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email_domain", "gmail.com");

    expect(model.addRow(WHOLE).refusalMessage).toBe(
      "gmail.com cannot be granted permissions because it is a public email provider.",
    );
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });

  it("refuses text that is not an email address", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email", "example.com");

    expect(model.addRow(WHOLE).refusalMessage).toBe(
      "example.com is not an email address.",
    );
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });

  it("refuses text that is not a domain", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email_domain", "someone@example.com");

    expect(model.addRow(WHOLE).refusalMessage).toBe(
      "someone@example.com is not a domain.",
    );
  });

  it("refuses an address the target already grants, whatever its case", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email", "Friend@Example.com");

    expect(model.addRow(WHOLE).refusalMessage).toBe(
      "friend@example.com is already granted here.",
    );
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });

  it("refuses the granter's own address", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email", "owner@example.com");

    expect(model.addRow(WHOLE).refusalMessage).toBe(
      "owner@example.com is your own address.",
    );
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });

  it("clears the refusal and the typed text when an entry is accepted", async () => {
    const model = await loadedPanel();
    model.addGrant(WHOLE, "email", "owner@example.com");
    model.addRow(WHOLE).value = "colleague@example.com";

    model.addGrant(WHOLE, "email", "colleague@example.com");

    expect(model.addRow(WHOLE).refusalMessage).toBeNull();
    expect(model.addRow(WHOLE).value).toBe("");
    expect(grantValues(model)).toEqual([
      "colleague@example.com",
      "friend@example.com",
    ]);
  });

  it("takes a domain typed with a leading @ as the domain itself", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email_domain", "@acme.com");

    expect(model.addRow(WHOLE).refusalMessage).toBeNull();
    expect(grantValues(model)).toEqual(["acme.com", "friend@example.com"]);
  });

  it("takes a domain whose labels are not ASCII", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email_domain", "münchen.de");

    expect(model.addRow(WHOLE).refusalMessage).toBeNull();
    expect(grantValues(model)).toEqual(["münchen.de", "friend@example.com"]);
  });

  it("refuses a public provider however it was typed", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email_domain", " GMail.com ");

    expect(model.addRow(WHOLE).refusalMessage).toBe(
      "gmail.com cannot be granted permissions because it is a public email provider.",
    );
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });

  it("stores an address in the form the workspace compares it by", async () => {
    const model = await loadedPanel();

    model.addGrant(WHOLE, "email", "  Colleague@Example.COM ");

    expect(grantValues(model)).toEqual([
      "colleague@example.com",
      "friend@example.com",
    ]);
  });

  it("refuses a duplicate against the app's own list rather than the workspace's", async () => {
    const { model } = makeSharePanel(() =>
      okResult(
        sharingResponse({
          grants: grantsDocument(
            { emails: ["friend@example.com"] },
            { web: grantList({ emails: ["dev@example.com"] }) },
          ),
        }),
      ),
    );
    await model.load();

    model.addGrant("web", "email", "friend@example.com");

    expect(model.addRow("web").refusalMessage).toBeNull();
    expect(grantValues(model, "web")).toEqual([
      "dev@example.com",
      "friend@example.com",
    ]);
  });
});

describe("SharePanelModel identity upgrade", () => {
  it("upgrades a resolved address in place and saves it as an account grant", async () => {
    const { model, requests } = makeSharePanel((url, init) => {
      if (url === RESOLVE_USER_URL)
        return okResult({
          user_id: "user-2",
          email: "friend@example.com",
          display_name: "Friend",
          profile_picture_url: null,
        });
      if (init?.method === "PUT")
        return okResult(
          sharingResponse({
            grants: (
              JSON.parse(init.body as string) as {
                grants: SharingGrantsDocument;
              }
            ).grants,
            identities: {
              "user-2": {
                user_id: "user-2",
                email: "friend@example.com",
                display_name: "Friend",
                profile_picture_url: null,
              },
            },
          }),
        );
      return okResult(
        sharingResponse({
          grants: grantsDocument({ email_domains: ["example.org"] }),
        }),
      );
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    const added = model.grantsFor(WHOLE)[1];
    const key = added.key;
    expect(added.grantee.kind).toBe("email");

    await settle();

    const upgraded = model.grantsFor(WHOLE)[1];
    expect(upgraded.key).toBe(key);
    expect(upgraded.grantee).toEqual({
      kind: "user",
      userId: "user-2",
      value: "friend@example.com",
    });
    expect(model.identityFor("user-2")?.display_name).toBe("Friend");
    const lastWrite = grantsWrites(requests).slice(-1)[0];
    expect(writtenDocument(lastWrite).workspace.users).toEqual(["user-2"]);
    expect(writtenDocument(lastWrite).workspace.emails).toEqual([]);
  });

  it("leaves the row an invited address when no account answers", async () => {
    const { model } = makeSharePanel((url, init) => {
      if (url === RESOLVE_USER_URL)
        return { ok: false, status: 404, body: { error: "not found" } };
      if (init?.method === "PUT")
        return okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        );
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    const row = model.grantsFor(WHOLE)[0];
    expect(row.grantee).toEqual({ kind: "email", value: "friend@example.com" });
    expect(row.status.state).toBe("settled");
  });

  it("looks up no account for a domain grant", async () => {
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT")
        return okResult(
          sharingResponse({
            grants: grantsDocument({ email_domains: ["example.org"] }),
          }),
        );
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email_domain", "example.org");
    await settle();

    expect(requests.some((request) => request.url === RESOLVE_USER_URL)).toBe(
      false,
    );
  });
});

describe("SharePanelModel stale answers", () => {
  it("ignores a load answered after a write that already landed", async () => {
    const heldLoads: Deferred[] = [];
    let isFirstLoad = true;
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT" && url.endsWith("/grants"))
        return okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        );
      if (init?.method === "PUT") return okResult(sharingResponse());
      if (url.endsWith("/invitation-outcomes"))
        return okResult({ outcomes: [] });
      if (isFirstLoad) {
        isFirstLoad = false;
        return okResult(sharingResponse({ enabled: false, url: null }));
      }
      const held = deferred();
      heldLoads.push(held);
      return held.promise;
    });
    await model.load();
    await model.publish();

    const reload = model.load();
    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();
    expect(model.grantsFor(WHOLE)[0].status.state).toBe("settled");

    heldLoads[0].resolve(
      okResult(sharingResponse({ grants: grantsDocument() })),
    );
    await reload;

    expect(grantValues(model)).toEqual(["friend@example.com"]);
    expect(model.loadStatus).toBe("ready");
  });

  it("keeps a row added while the publish was still answering", async () => {
    const heldPublishes: Deferred[] = [];
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT" && url.endsWith("/grants"))
        return okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
          }),
        );
      if (init?.method === "PUT") {
        const held = deferred();
        heldPublishes.push(held);
        return held.promise;
      }
      return okResult(sharingResponse({ enabled: false, url: null }));
    });
    await model.load();

    const publishing = model.publish();
    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    heldPublishes[0].resolve(
      okResult(sharingResponse({ grants: grantsDocument() })),
    );
    await publishing;

    expect(grantValues(model)).toEqual(["friend@example.com"]);
    expect(model.isPublished).toBe(true);
    expect(model.machineUrl).toBe("https://m.relay.example/");
  });

  it("leaves the switch off when an unpublish answers before an earlier publish", async () => {
    const heldPublishes: Deferred[] = [];
    const { model } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        const held = deferred();
        heldPublishes.push(held);
        return held.promise;
      }
      if (init?.method === "DELETE")
        return okResult(sharingResponse({ enabled: false, url: null }));
      return okResult(sharingResponse({ enabled: false, url: null }));
    });
    await model.load();

    const publishing = model.publish();
    await model.unpublish();
    expect(model.isPublished).toBe(false);

    heldPublishes[0].resolve(okResult(sharingResponse()));
    await publishing;

    expect(model.isPublished).toBe(false);
    expect(model.publishWrite).toEqual({ state: "idle" });
    expect(model.machineUrl).toBeNull();
  });

  it("does not call the link live when a load answers during a provisioning wait", async () => {
    const scheduled: (() => void)[] = [];
    const { model } = makeSharePanel(
      (url, init) => {
        if (url.endsWith("/readiness")) return okResult({ ready: false });
        if (init?.method === "PUT") return okResult(sharingResponse());
        return okResult(sharingResponse());
      },
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );
    await model.load();
    await model.publish();
    expect(model.isLive).toBe(false);
    const scheduledBeforeLoad = scheduled.length;

    await model.load();

    expect(model.isLive).toBe(false);
    expect(model.isAwaitingLink).toBe(true);
    scheduled[scheduledBeforeLoad - 1]();
    await settle();
    expect(scheduled.length).toBeGreaterThan(scheduledBeforeLoad);
  });

  it("schedules no second probe when the poll restarts during one probe", async () => {
    const scheduled: (() => void)[] = [];
    const heldProbes: Deferred[] = [];
    const { model } = makeSharePanel(
      (url, init) => {
        if (url.endsWith("/readiness")) {
          const held = deferred();
          heldProbes.push(held);
          return held.promise;
        }
        if (init?.method === "PUT") return okResult(sharingResponse());
        return okResult(sharingResponse({ enabled: false, url: null }));
      },
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );
    await model.load();
    await model.publish();

    scheduled[0]();
    await settle();
    expect(heldProbes).toHaveLength(1);

    // Stopped and restarted on the same target while the probe is in flight.
    model.selectTarget("web");
    model.selectTarget(WHOLE);
    const scheduledBeforeAnswer = scheduled.length;

    heldProbes[0].resolve(okResult({ ready: false }));
    await settle();

    expect(scheduled.length).toBe(scheduledBeforeAnswer);
  });
});

describe("SharePanelModel failed operations", () => {
  /** A status document granting Carol as the account she is, plus whatever
   * else `workspace` names. */
  function carolGranted(
    workspace: Partial<SharingGrantList> = {},
  ): MachineSharingResponse {
    return sharingResponse({
      grants: grantsDocument({ users: ["user-2"], ...workspace }),
      identities: {
        "user-2": {
          user_id: "user-2",
          email: "carol@example.net",
          display_name: "Carol Reyes",
          profile_picture_url: null,
        },
      },
    });
  }

  it("shows a removed person again when they are re-added before the removal lands", async () => {
    const writes: Deferred[] = [];
    const { model } = makeSharePanel((url, init) => {
      if (url === RESOLVE_USER_URL)
        return { ok: false, status: 404, body: { error: "not found" } };
      if (init?.method === "PUT") {
        const write = deferred();
        writes.push(write);
        return write.promise;
      }
      return okResult(carolGranted());
    });
    /** Answer the oldest write in flight with a document granting Carol as the
     * account she is, which is not the address her re-added row was typed as. */
    const landWrite = async (
      workspace: Partial<SharingGrantList> = {},
    ): Promise<void> => {
      writes.shift()?.resolve(okResult(carolGranted(workspace)));
      await settle();
    };
    await model.load();
    expect(grantValues(model)).toEqual(["carol@example.net"]);

    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[0].key);
    await settle();
    await landWrite();

    model.addGrant(WHOLE, "email", "carol@example.net");
    await settle();
    await landWrite();
    expect(grantValues(model)).toEqual(["carol@example.net"]);
    expect(model.grantsFor(WHOLE)[0].status.state).toBe("settled");

    model.addGrant(WHOLE, "email_domain", "acme.example");
    await settle();
    await landWrite({ email_domains: ["acme.example"] });

    expect(grantValues(model)).toEqual(["acme.example", "carol@example.net"]);
  });

  it("keeps a failed removal's person granted until Retry takes them out", async () => {
    let writeCount = 0;
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        writeCount += 1;
        if (writeCount === 1) return { ok: false, status: 500, body: {} };
        return okResult(sharingResponse({ grants: sentDocument(init) }));
      }
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();

    model.removeGrant(WHOLE, model.grantsFor(WHOLE)[0].key);
    await settle();

    const restored = model.grantsFor(WHOLE)[0];
    expect(restored.grantee.value).toBe("friend@example.com");
    expect(restored.status).toEqual({
      state: "failed",
      failedOperation: "remove",
      failureMessage: "Could not save",
    });

    model.addGrant(WHOLE, "email_domain", "acme.example");
    await settle();
    expect(writtenDocument(grantsWrites(requests)[1]).workspace.emails).toEqual(
      ["friend@example.com"],
    );

    model.retryGrant(WHOLE, restored.key);
    expect(grantValues(model)).toEqual(["acme.example"]);
    await settle();

    expect(writtenDocument(grantsWrites(requests)[2]).workspace.emails).toEqual(
      [],
    );
    expect(grantValues(model)).toEqual(["acme.example"]);
  });

  it("leaves a failed add ungranted until Retry puts it back in", async () => {
    let writeCount = 0;
    const { model, requests } = makeSharePanel((url, init) => {
      if (init?.method === "PUT") {
        writeCount += 1;
        if (writeCount === 1) return { ok: false, status: 500, body: {} };
        return okResult(sharingResponse({ grants: sentDocument(init) }));
      }
      return okResult(sharingResponse());
    });
    await model.load();

    model.addGrant(WHOLE, "email", "friend@example.com");
    await settle();

    const failed = model.grantsFor(WHOLE)[0];
    expect(failed.status).toEqual({
      state: "failed",
      failedOperation: "add",
      failureMessage: "Could not save",
    });

    model.addGrant(WHOLE, "email_domain", "acme.example");
    await settle();
    expect(writtenDocument(grantsWrites(requests)[1]).workspace.emails).toEqual(
      [],
    );

    model.retryGrant(WHOLE, failed.key);
    await settle();

    expect(writtenDocument(grantsWrites(requests)[2]).workspace.emails).toEqual(
      ["friend@example.com"],
    );
    expect(model.grantsFor(WHOLE)[1].status.state).toBe("settled");
  });
});

describe("SharePanelModel status read failures", () => {
  it("reports a failed status read and leaves no poll running", async () => {
    const scheduled: (() => void)[] = [];
    const { model } = makeSharePanel(
      () => ({ ok: false, status: 502, body: { error: "relay down" } }),
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );

    await model.load();

    expect(model.loadStatus).toBe("load_failed");
    expect(model.loadErrorMessage).toContain("relay down");
    expect(model.canAdd).toBe(false);
    expect(scheduled).toHaveLength(0);
  });

  it("reads again when a target is selected after the read failed", async () => {
    let isFirstRead = true;
    const { model, requests } = makeSharePanel(() => {
      if (isFirstRead) {
        isFirstRead = false;
        return { ok: false, status: 502, body: { error: "relay down" } };
      }
      return okResult(
        sharingResponse({
          grants: grantsDocument({ emails: ["friend@example.com"] }),
        }),
      );
    });
    await model.load();
    expect(model.loadStatus).toBe("load_failed");

    model.selectTarget("web");
    await settle();

    const statusReads = requests.filter(
      (request) => !request.url.endsWith("/invitation-outcomes"),
    );
    expect(statusReads).toHaveLength(2);
    expect(model.loadStatus).toBe("ready");
    expect(model.currentTarget).toBe("web");
    expect(grantValues(model)).toEqual(["friend@example.com"]);
  });
});

describe("SharePanelModel invitations", () => {
  const OUTCOMES_SUFFIX = "/invitation-outcomes";
  const INVITATIONS_SUFFIX = "/invitations";
  const INVITED_AT = "2026-09-30T12:00:00+00:00";

  /** A published, synced panel: the whole workspace grants one address, one
   * account and one domain, and the web app grants one more address. The
   * outcomes route answers with `outcomes`, which a test may grow. */
  function invitablePanel(
    respondToInvite: Responder = () =>
      okResult({ outcome: "invited", invited_at: INVITED_AT }),
    outcomes: unknown[] = [],
    overrides: Partial<SharePanelModelOptions> = {},
    isPublished: () => boolean = () => true,
  ): { model: SharePanelModel; requests: RecordedRequest[] } {
    return makeSharePanel((url, init) => {
      if (url.endsWith(OUTCOMES_SUFFIX)) return okResult({ outcomes });
      if (url.endsWith(INVITATIONS_SUFFIX)) return respondToInvite(url, init);
      return okResult(
        sharingResponse({
          enabled: init?.method === "DELETE" ? false : isPublished(),
          grants: grantsDocument(
            {
              users: ["user-2"],
              emails: ["friend@example.com"],
              email_domains: ["acme.example"],
            },
            { web: grantList({ emails: ["dev@example.com"] }) },
          ),
          identities: {
            "user-2": {
              user_id: "user-2",
              email: "carol@example.com",
              display_name: "Carol",
              profile_picture_url: null,
            },
          },
          grants_synced: true,
        }),
      );
    }, overrides);
  }

  function rowOf(model: SharePanelModel, target: string, value: string): Grant {
    const row = model
      .grantsFor(target)
      .find((grant) => grant.grantee.value === value);
    expect(row, `no row for ${value}`).toBeDefined();
    return row as Grant;
  }

  function inviteRequests(requests: RecordedRequest[]): RecordedRequest[] {
    return requests.filter((request) =>
      request.url.endsWith(INVITATIONS_SUFFIX),
    );
  }

  it("reads what the granter may learn about each row once the document is in", async () => {
    const { model, requests } = invitablePanel(undefined, [
      {
        kind: "email",
        value: "friend@example.com",
        app: WHOLE,
        outcome: "invited",
        invited_at: "2026-09-29T00:00:00+00:00",
      },
      {
        kind: "user",
        value: "user-2",
        app: WHOLE,
        outcome: "joined",
        joined_at: "2026-09-28T00:00:00+00:00",
        last_visited_at: "2026-09-29T00:00:00+00:00",
      },
      { kind: "email", value: "dev@example.com", app: "web", outcome: null },
    ]);

    await model.load();
    await settle();

    expect(
      requests.filter((request) => request.url.endsWith(OUTCOMES_SUFFIX)),
    ).toHaveLength(1);
    expect(model.grantsSynced).toBe(true);
    expect(
      model.outcomeFor(WHOLE, rowOf(model, WHOLE, "friend@example.com")),
    ).toEqual({
      kind: "invited",
      invitedAt: "2026-09-29T00:00:00+00:00",
      joinedAt: null,
      lastVisitedAt: null,
    });
    expect(
      model.outcomeFor(WHOLE, rowOf(model, WHOLE, "carol@example.com")),
    ).toEqual({
      kind: "joined",
      invitedAt: null,
      joinedAt: "2026-09-28T00:00:00+00:00",
      lastVisitedAt: "2026-09-29T00:00:00+00:00",
    });
    expect(
      model.outcomeFor("web", rowOf(model, "web", "dev@example.com")),
    ).toBeNull();
  });

  it("offers Invite only for a saved person who has not joined, on a published workspace whose document has reached Imbue Cloud", async () => {
    let isPublished = true;
    const { model } = invitablePanel(
      undefined,
      [
        {
          kind: "user",
          value: "user-2",
          app: WHOLE,
          outcome: "joined",
          joined_at: "2026-09-28T00:00:00+00:00",
        },
      ],
      {},
      () => isPublished,
    );
    await model.load();
    await settle();
    const friend = rowOf(model, WHOLE, "friend@example.com");

    expect(model.canInvite(WHOLE, friend)).toBe(true);
    expect(model.canInvite("web", rowOf(model, "web", "dev@example.com"))).toBe(
      true,
    );
    // Joined is the one outcome that proves the grant worked: nothing to send.
    expect(
      model.canInvite(WHOLE, rowOf(model, WHOLE, "carol@example.com")),
    ).toBe(false);
    // Nobody at a domain is ever notified.
    expect(model.canInvite(WHOLE, rowOf(model, WHOLE, "acme.example"))).toBe(
      false,
    );

    model.grantsSynced = false;
    expect(model.isGrantsSyncPending).toBe(true);
    expect(model.canInvite(WHOLE, friend)).toBe(false);

    model.grantsSynced = true;
    expect(model.isGrantsSyncPending).toBe(false);

    // Unpublishing is read from the document, so the panel is taken there by a
    // load rather than by setting the flag.
    isPublished = false;
    await model.load();
    await settle();
    expect(model.isPublished).toBe(false);
    expect(model.canInvite(WHOLE, rowOf(model, WHOLE, "friend@example.com"))).toBe(
      false,
    );
  });

  it("invites an address on the whole workspace and an account on an app, the row pending meanwhile", async () => {
    const answer = deferred();
    const outcomes: unknown[] = [];
    const { model, requests } = invitablePanel(() => answer.promise, outcomes);
    await model.load();
    await settle();
    const friend = rowOf(model, WHOLE, "friend@example.com");

    const inviting = model.invite(WHOLE, friend.key);
    expect(model.inviteStateFor(friend.key)).toEqual({ state: "inviting" });
    expect(model.canInvite(WHOLE, friend)).toBe(false);
    outcomes.push({
      kind: "email",
      value: "friend@example.com",
      app: WHOLE,
      outcome: "invited",
      invited_at: INVITED_AT,
    });
    answer.resolve(okResult({ outcome: "invited", invited_at: INVITED_AT }));
    await inviting;
    await settle();

    expect(inviteRequests(requests).map((request) => request.method)).toEqual([
      "POST",
    ]);
    expect(inviteRequests(requests)[0].body).toEqual({
      email: "friend@example.com",
      app: null,
    });
    expect(model.inviteStateFor(friend.key)).toEqual({ state: "idle" });
    expect(model.outcomeFor(WHOLE, friend)).toEqual({
      kind: "invited",
      invitedAt: INVITED_AT,
      joinedAt: null,
      lastVisitedAt: null,
    });

    await model.invite("web", rowOf(model, "web", "dev@example.com").key);
    await model.invite(WHOLE, rowOf(model, WHOLE, "carol@example.com").key);

    expect(
      inviteRequests(requests)
        .slice(1)
        .map((request) => request.body),
    ).toEqual([
      { email: "dev@example.com", app: "web" },
      { user_id: "user-2", app: null },
    ]);
  });

  it("says why nothing was sent when the allowance or the cooldown refused, and only until the next load", async () => {
    let refusal = "too_soon";
    const { model } = invitablePanel(() => okResult({ outcome: refusal }));
    await model.load();
    await settle();
    const friend = rowOf(model, WHOLE, "friend@example.com");

    await model.invite(WHOLE, friend.key);
    expect(model.inviteStateFor(friend.key)).toEqual({
      state: "refused",
      message: "You invited this person too recently.",
    });
    expect(model.outcomeFor(WHOLE, friend)).toBeNull();
    // A refusal is no bar to trying again.
    expect(model.canInvite(WHOLE, friend)).toBe(true);

    refusal = "over_allowance";
    await model.invite(WHOLE, friend.key);
    expect(model.inviteStateFor(friend.key)).toEqual({
      state: "refused",
      message: "You have reached your invitation limit for now.",
    });

    await model.load();
    expect(
      model.inviteStateFor(rowOf(model, WHOLE, "friend@example.com").key),
    ).toEqual({
      state: "idle",
    });
  });

  it("keeps a could-not-invite answer as the row's outcome, without a reason", async () => {
    const outcomes: unknown[] = [];
    const { model } = invitablePanel(() => {
      outcomes.push({
        kind: "email",
        value: "friend@example.com",
        app: WHOLE,
        outcome: "could_not_invite",
      });
      return okResult({ outcome: "could_not_invite" });
    }, outcomes);
    await model.load();
    await settle();
    const friend = rowOf(model, WHOLE, "friend@example.com");

    await model.invite(WHOLE, friend.key);

    expect(model.inviteStateFor(friend.key)).toEqual({ state: "idle" });
    expect(model.outcomeFor(WHOLE, friend)?.kind).toBe("could_not_invite");
  });

  it("reports the desktop's refusal by its code, and any other failure in its own words", async () => {
    let answer: FetchResult = {
      ok: false,
      status: 409,
      body: { error: "not_invitable", message: "already joined" },
    };
    const { model } = invitablePanel(() => answer);
    await model.load();
    await settle();
    const friend = rowOf(model, WHOLE, "friend@example.com");

    await model.invite(WHOLE, friend.key);
    expect(model.inviteStateFor(friend.key)).toEqual({
      state: "refused",
      message: "This person has already joined.",
    });

    answer = {
      ok: false,
      status: 502,
      body: { error: "Could not invite: the connector is unreachable" },
    };
    await model.invite(WHOLE, friend.key);
    expect(model.inviteStateFor(friend.key)).toEqual({
      state: "refused",
      message: "Could not invite: the connector is unreachable",
    });
  });

  it("loads again a moment after a document that did not reach Imbue Cloud, so it is sent again", async () => {
    const timers: (() => void)[] = [];
    let isSynced = false;
    const { model, requests } = makeSharePanel(
      (url) => {
        if (url.endsWith(OUTCOMES_SUFFIX)) return okResult({ outcomes: [] });
        return okResult(
          sharingResponse({
            grants: grantsDocument({ emails: ["friend@example.com"] }),
            grants_synced: isSynced,
          }),
        );
      },
      {
        setTimer: (callback) => {
          timers.push(callback);
          return timers.length;
        },
      },
    );

    await model.load();
    await settle();
    expect(model.isGrantsSyncPending).toBe(true);
    expect(
      model.canInvite(WHOLE, rowOf(model, WHOLE, "friend@example.com")),
    ).toBe(false);
    expect(timers).toHaveLength(1);

    isSynced = true;
    timers[0]();
    await settle();

    expect(model.grantsSynced).toBe(true);
    expect(model.isGrantsSyncPending).toBe(false);
    expect(
      requests.filter(
        (request) =>
          request.method === "GET" && !request.url.endsWith(OUTCOMES_SUFFIX),
      ),
    ).toHaveLength(2);
    // Synced now: nothing more to schedule.
    expect(timers).toHaveLength(1);
  });

  it("reads the outcomes again after a save lands and after publishing, never while unpublished", async () => {
    const { model, requests } = invitablePanel();
    await model.load();
    await settle();
    const reads = (): number =>
      requests.filter((request) => request.url.endsWith(OUTCOMES_SUFFIX))
        .length;
    const afterLoad = reads();
    expect(afterLoad).toBe(1);

    model.addGrant(WHOLE, "email", "newcomer@example.com");
    await settle();
    await settle();
    expect(reads()).toBeGreaterThan(afterLoad);
    const afterAdd = reads();

    await model.unpublish();
    await settle();
    expect(reads()).toBe(afterAdd);

    await model.publish();
    await settle();
    expect(reads()).toBeGreaterThan(afterAdd);
  });

  it("requests an invitation on its own when a person is granted, once the save reaches Imbue Cloud, and never for a domain", async () => {
    const invitePayloads: { email?: string }[] = [];
    const invitedAddresses = new Set<string>();
    const { model } = makeSharePanel((url, init) => {
      if (url.endsWith(OUTCOMES_SUFFIX))
        return okResult({
          outcomes: [...invitedAddresses].map((email) => ({
            kind: "email",
            value: email,
            app: WHOLE,
            outcome: "invited",
            invited_at: INVITED_AT,
          })),
        });
      if (url.endsWith(INVITATIONS_SUFFIX)) {
        const body = JSON.parse(init?.body as string) as { email?: string };
        invitePayloads.push(body);
        if (body.email !== undefined) invitedAddresses.add(body.email);
        return okResult({ outcome: "invited", invited_at: INVITED_AT });
      }
      if (init?.method === "PUT" && url.endsWith("/grants"))
        return okResult(sharingResponse({ grants: sentDocument(init), grants_synced: true }));
      return okResult(sharingResponse({ grants: grantsDocument(), grants_synced: true }));
    });
    await model.load();
    await settle();

    // Granting a person is what asks: no Invite is pressed.
    model.addGrant(WHOLE, "email", "newcomer@example.com");
    await settle();
    await settle();

    expect(invitePayloads).toEqual([{ email: "newcomer@example.com", app: null }]);
    expect(model.invitationStatusIndicator(WHOLE, rowOf(model, WHOLE, "newcomer@example.com")).kind).toBe("invited");

    // A domain grant notifies nobody, so none is requested for it.
    model.addGrant(WHOLE, "email_domain", "acme.example");
    await settle();
    await settle();
    expect(invitePayloads).toHaveLength(1);

    // The automatic request fires once; a settled, invited row is left alone.
    model.addGrant(WHOLE, "email", "newcomer@example.com");
    await settle();
    await settle();
    expect(invitePayloads).toHaveLength(1);
  });

  it("reduces each row to a single typed status indicator state", async () => {
    const { model } = invitablePanel(undefined, [
      { kind: "email", value: "friend@example.com", app: WHOLE, outcome: "invited", invited_at: INVITED_AT },
      {
        kind: "user",
        value: "user-2",
        app: WHOLE,
        outcome: "joined",
        joined_at: "2026-09-28T00:00:00+00:00",
        last_visited_at: "2026-09-29T00:00:00+00:00",
      },
    ]);
    await model.load();
    await settle();

    expect(model.invitationStatusIndicator(WHOLE, rowOf(model, WHOLE, "friend@example.com"))).toEqual({
      kind: "invited",
      at: INVITED_AT,
      canReinvite: true,
    });
    expect(model.invitationStatusIndicator(WHOLE, rowOf(model, WHOLE, "carol@example.com"))).toEqual({
      kind: "joined",
      firstAt: "2026-09-28T00:00:00+00:00",
      lastAt: "2026-09-29T00:00:00+00:00",
    });
    // A domain row, and a person with nothing known yet, show nothing.
    expect(model.invitationStatusIndicator(WHOLE, rowOf(model, WHOLE, "acme.example"))).toEqual({ kind: "none" });
    expect(model.invitationStatusIndicator("web", rowOf(model, "web", "dev@example.com"))).toEqual({ kind: "none" });
  });

  it("shows the refusal as the status indicator state, with a re-invite still offered", async () => {
    const { model } = invitablePanel(() => okResult({ outcome: "too_soon" }));
    await model.load();
    await settle();
    const friend = rowOf(model, WHOLE, "friend@example.com");

    await model.invite(WHOLE, friend.key);

    expect(model.invitationStatusIndicator(WHOLE, friend)).toEqual({
      kind: "refused",
      message: "You invited this person too recently.",
      canReinvite: true,
    });
  });
});

describe("what the panel calls a target", () => {
  const inert = () => ({ ok: true, status: 200, body: {} });

  it("calls an app what the workspace calls it", () => {
    const { model } = makeSharePanel(inert, {
      appServices: ["files", "web"],
      serviceDisplayNames: { files: "File Viewer" },
    });

    expect(model.targetDisplayName("files")).toBe("File Viewer");
    // An app that registered no display name is called by its own name.
    expect(model.targetDisplayName("web")).toBe("web");
  });

  it("keeps a name that could never be a hostname label", () => {
    const { model } = makeSharePanel(inert, {
      appServices: ["imbue-hud"],
      serviceDisplayNames: { "imbue-hud": "Imbue HUD" },
    });

    expect(model.targetDisplayName("imbue-hud")).toBe("Imbue HUD");
  });

  it("marks a name two apps share, so their rows can be told apart", () => {
    const { model } = makeSharePanel(inert, {
      appServices: ["hud", "hud-dev", "files"],
      serviceDisplayNames: {
        hud: "Imbue HUD",
        "hud-dev": "Imbue HUD",
        files: "File Viewer",
      },
    });

    expect(model.isDisplayNameAmbiguous("hud")).toBe(true);
    expect(model.isDisplayNameAmbiguous("hud-dev")).toBe(true);
    expect(model.isDisplayNameAmbiguous("files")).toBe(false);
  });

  it("marks nothing when the apps have no display name of their own", () => {
    const { model } = makeSharePanel(inert, {
      appServices: ["web", "docs"],
      serviceDisplayNames: {},
    });

    expect(model.isDisplayNameAmbiguous("web")).toBe(false);
  });
});

describe("SharePanelModel mobile access link", () => {
  const MOBILE_SUFFIX = "/mobile-access-link";

  /** A published, live panel whose whole-workspace link is ready, so the
   * control is offered. The mobile-link route answers with `respond`. */
  function linkablePanel(
    respond: Responder = () =>
      okResult({ outcome: "sent", recipient_email: "owner@example.com" }),
    isPublished = true,
  ): { model: SharePanelModel; requests: RecordedRequest[] } {
    return makeSharePanel((url, init) => {
      if (url.endsWith(MOBILE_SUFFIX)) return respond(url, init);
      if (url.endsWith("/invitation-outcomes"))
        return okResult({ outcomes: [] });
      return okResult(
        sharingResponse({ enabled: isPublished, grants_synced: true }),
      );
    });
  }

  function mobileRequests(requests: RecordedRequest[]): RecordedRequest[] {
    return requests.filter((request) => request.url.endsWith(MOBILE_SUFFIX));
  }

  it("posts to the route with no body, since the address is not the panel's to choose", async () => {
    const { model, requests } = linkablePanel();
    await model.load();

    await model.sendMobileAccessLink();

    expect(mobileRequests(requests)).toEqual([
      {
        url: expect.stringContaining(MOBILE_SUFFIX),
        method: "POST",
        body: null,
      },
    ]);
    expect(model.mobileLinkState).toEqual({
      state: "sent",
      recipientEmail: "owner@example.com",
    });
  });

  it("falls back to the signed-in address when the answer does not name one", async () => {
    const { model } = linkablePanel(() => okResult({ outcome: "sent" }));
    await model.load();

    await model.sendMobileAccessLink();

    expect(model.mobileLinkState).toEqual({
      state: "sent",
      recipientEmail: "owner@example.com",
    });
  });

  it("says the send failed when the route answered without sending", async () => {
    const { model } = linkablePanel(() => okResult({ outcome: "failed" }));
    await model.load();

    await model.sendMobileAccessLink();

    expect(model.mobileLinkState).toEqual({
      state: "refused",
      message: "Could not send the email",
    });
  });

  it("says the same thing for either refusal, naming no internal state", async () => {
    const noAccessPoint =
      "This workspace does not have a secure access point yet. If you've " +
      "just enabled sharing and web access, please wait a few minutes " +
      "before trying again.";
    for (const error of ["no_workspace_link", "not_published"]) {
      const { model } = linkablePanel(() => ({
        ok: false,
        status: 409,
        body: { error, message: "not ready" },
      }));
      await model.load();

      await model.sendMobileAccessLink();

      expect(model.mobileLinkState).toEqual({
        state: "refused",
        message: noAccessPoint,
      });
    }
  });

  it("offers nothing to click while sharing is off, and sends nothing if asked", async () => {
    const { model, requests } = linkablePanel(undefined, false);
    await model.load();

    expect(model.canSendMobileAccessLink).toBe(false);
    await model.sendMobileAccessLink();

    expect(mobileRequests(requests)).toEqual([]);
    expect(model.mobileLinkState).toEqual({ state: "idle" });
  });

  it("stays askable while the workspace's address is still coming up", async () => {
    const { model } = linkablePanel();
    await model.load();
    model.isLive = false;

    expect(model.canSendMobileAccessLink).toBe(true);
  });

  it("clears what a past request said when the panel is looked at again", async () => {
    const { model } = linkablePanel();
    await model.load();
    await model.sendMobileAccessLink();

    await model.load();

    expect(model.mobileLinkState).toEqual({ state: "idle" });
  });
});
