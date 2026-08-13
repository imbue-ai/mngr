import { beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../testing";
import {
  InboxModel,
  expandSharePathHome,
  isPermissionCheckboxDisabled,
  isSharePathWithinRoots,
  submittedPermissions,
} from "./inbox";
import type {
  FileSharingPermissionDetail,
  ManualCredentialsPrompt,
  PredefinedPermissionDetail,
  ResolvedRequest,
} from "./inbox";

// The workspace a card belongs to is NOT the agent that filed the request --
// latchkey requests come from the workspace's system-services sibling -- so the
// two ids are kept distinct here.
const CARD_A = {
  id: "evt-a",
  kind_label: "permission",
  ws_name: "alpha",
  display_name: "Slack",
  accent: "#123456",
  workspace_agent_id: "agent-alpha",
};
const CARD_B = {
  id: "evt-b",
  kind_label: "permission",
  ws_name: "beta",
  display_name: "Gmail",
  accent: "#654321",
  workspace_agent_id: "agent-beta",
};

// Two more queue positions, so a successor picked by position can be told
// apart from one picked off the head of the list.
const CARD_C = { ...CARD_A, id: "evt-c", ws_name: "gamma" };
const CARD_D = { ...CARD_A, id: "evt-d", ws_name: "delta" };

const PREDEFINED_DETAIL: PredefinedPermissionDetail = {
  kind: "predefined",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "read the channel",
  scope: "slack-api",
  display_name: "Slack",
  service_name: "slack",
  permission_groups: [
    {
      heading: "Full access",
      is_extras: false,
      rows: [
        {
          permission: "slack-read-all",
          label: "Read everything",
          description: "",
          is_wildcard: false,
        },
      ],
    },
    {
      heading: "Extras",
      is_extras: true,
      rows: [
        {
          permission: "any",
          label: "Everything (unrestricted)",
          description: "",
          is_wildcard: true,
        },
      ],
    },
  ],
  checked_permissions: ["slack-read-all"],
  account_choices: [
    { value: "", label: "Default account", hint: "", is_credential_setup_needed: false, is_account_name_needed: false },
  ],
  selected_account_value: "",
  new_account_value: ":new-account",
  wildcard_permission: "any",
  will_open_browser: false,
  manual_credentials: null,
};

const AWS_PROMPT: ManualCredentialsPrompt = {
  parameters: [
    { name: "access-key-id", label: "Access key id" },
    { name: "secret-access-key", label: "Secret access key" },
  ],
  message: "AWS does not support browser sign-in",
};

// A service with no browser sign-in: the connected account is fine, the
// not-yet-connected one and the new-account choice need credentials typed in.
const MANUAL_DETAIL: PredefinedPermissionDetail = {
  ...PREDEFINED_DETAIL,
  display_name: "AWS",
  account_choices: [
    {
      value: "alice@x",
      label: "alice@x",
      hint: "",
      is_credential_setup_needed: false,
      is_account_name_needed: false,
    },
    {
      value: "bob@x",
      label: "bob@x",
      hint: "not connected yet -- asks you for credentials",
      is_credential_setup_needed: true,
      is_account_name_needed: false,
    },
    {
      value: ":new-account",
      label: "+ Add account",
      hint: "asks you for credentials",
      is_credential_setup_needed: true,
      is_account_name_needed: true,
    },
  ],
  selected_account_value: "bob@x",
  manual_credentials: AWS_PROMPT,
};

const FILE_DETAIL: FileSharingPermissionDetail = {
  kind: "file_sharing",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "summarize",
  file_path: "/home/user/doc.txt",
  access: "READ",
  access_human_label: "read-only",
  allowed_roots: ["/home/user", "/tmp/shared"],
  home_dir: "/home/user",
};

describe("share path helpers", () => {
  it("expands a leading tilde to the home dir but leaves ~user alone", () => {
    expect(expandSharePathHome("~/notes.txt", "/home/u")).toBe(
      "/home/u/notes.txt",
    );
    expect(expandSharePathHome("~", "/home/u")).toBe("/home/u");
    expect(expandSharePathHome("~other/x", "/home/u")).toBe("~other/x");
    expect(expandSharePathHome("~/x", "")).toBe("~/x");
  });

  it("accepts paths at or beneath a root, case-insensitively, and rejects others", () => {
    const roots = ["/home/User", "/tmp/shared/"];
    expect(isSharePathWithinRoots("/home/user/doc.txt", roots)).toBe(true);
    expect(isSharePathWithinRoots("/home/user", roots)).toBe(true);
    expect(isSharePathWithinRoots("/tmp/shared/x", roots)).toBe(true);
    expect(isSharePathWithinRoots("/etc/passwd", roots)).toBe(false);
    expect(isSharePathWithinRoots("/home/username-else", roots)).toBe(false);
    expect(isSharePathWithinRoots("", roots)).toBe(false);
  });
});

describe("wildcard exclusivity", () => {
  it("disables specific permissions while the wildcard is checked", () => {
    const checked = new Set(["any"]);
    expect(isPermissionCheckboxDisabled("slack-read-all", "any", checked)).toBe(
      true,
    );
    expect(isPermissionCheckboxDisabled("any", "any", checked)).toBe(false);
    expect(
      isPermissionCheckboxDisabled(
        "slack-read-all",
        "any",
        new Set(["slack-read-all"]),
      ),
    ).toBe(false);
  });

  it("drops the specific permissions from the submitted set whenever the wildcard is in it", () => {
    expect(
      submittedPermissions(new Set(["slack-read-all", "any"]), "any"),
    ).toEqual(["any"]);
    expect(
      submittedPermissions(new Set(["any", "slack-read-all"]), "any"),
    ).toEqual(["any"]);
    expect(submittedPermissions(new Set(["slack-read-all"]), "any")).toEqual([
      "slack-read-all",
    ]);
    // A detail with no wildcard submits its selection untouched.
    expect(submittedPermissions(new Set(["slack-read-all", ""]), "")).toEqual([
      "slack-read-all",
      "",
    ]);
  });
});

describe("InboxModel", () => {
  let calls: Array<{ url: string; init?: RequestInit }>;

  function makeModel(
    responses: Record<string, () => Response>,
    onClose?: () => void,
    onResolved?: (resolved: ResolvedRequest) => void,
  ): InboxModel {
    calls = [];
    return new InboxModel({
      fetcher: (url, init) => {
        calls.push({ url, init });
        const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
        const producer = responses[key];
        if (!producer) throw new Error(`Unexpected fetch: ${key}`);
        return Promise.resolve(producer());
      },
      onClose,
      onResolved,
    });
  }

  beforeEach(() => {
    calls = [];
  });

  it("loads the card list and seeds detail state on selection", async () => {
    const model = makeModel({
      "GET /ui/api/inbox": () =>
        jsonResponse({ cards: [CARD_A, CARD_B] }),
      "GET /ui/api/inbox/evt-a/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
    });
    await model.loadList();
    expect(model.cards.map((card) => card.id)).toEqual(["evt-a", "evt-b"]);

    await model.select("evt-a");

    expect(model.detail?.kind).toBe("predefined");
    expect([...model.checkedPermissions]).toEqual(["slack-read-all"]);
    expect(model.selectedAccount).toBe("");
    expect(model.isApproveAllowed()).toBe(true);
  });

  it("opens every request on its summary, including the one after an Adjust", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
    });
    await model.select("evt-a");
    expect(model.isPermissionEditorShown).toBe(false);

    model.showPermissionEditor();
    expect(model.isPermissionEditorShown).toBe(true);

    await model.select("evt-a");
    expect(model.isPermissionEditorShown).toBe(false);
  });

  it("surfaces a failed list load and lets the pending-set reconciliation retry it", async () => {
    let isServerUp = false;
    const model = makeModel({
      "GET /ui/api/inbox": () =>
        isServerUp
          ? jsonResponse({ cards: [CARD_A] })
          : jsonResponse({ error: "boom" }, 503),
    });

    await model.loadList();
    expect(model.listErrorMessage).toContain("Could not load requests");
    // The load attempt completed, so the page's live-refresh gate must open.
    expect(model.isListLoaded).toBe(true);

    // Still down: the reconciliation retries (and fails) rather than treating
    // the pending set as already handled.
    await model.refreshIfPendingChanged(["evt-a"]);
    expect(model.listErrorMessage).not.toBeNull();

    // Back up: the SAME pending set retries again and clears the error.
    isServerUp = true;
    await model.refreshIfPendingChanged(["evt-a"]);
    expect(model.listErrorMessage).toBeNull();
    expect(model.cards.map((card) => card.id)).toEqual(["evt-a"]);
  });

  it("marks the list load failed when the fetch itself throws", async () => {
    const model = makeModel({
      "GET /ui/api/inbox": () => {
        throw new Error("network down");
      },
    });

    await model.loadList();

    expect(model.listErrorMessage).toContain("Could not load requests");
    expect(model.isListLoaded).toBe(true);
  });

  it("gates approve on the file-sharing path being inside a shared root", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () =>
        jsonResponse({ detail: FILE_DETAIL }),
    });
    await model.select("evt-a");

    expect(model.isApproveAllowed()).toBe(true);
    model.filePathValue = "/etc/passwd";
    expect(model.isApproveAllowed()).toBe(false);
    expect(model.isSharePathHintShown()).toBe(true);
    model.filePathValue = "~/ok.txt";
    expect(model.isApproveAllowed()).toBe(true);
    expect(model.isSharePathHintShown()).toBe(false);
  });

  it("submits the predefined grant form and advances to the next request", async () => {
    let closed = false;
    const model = makeModel(
      {
        "GET /ui/api/inbox": () =>
          jsonResponse({ cards: [CARD_B] }),
        "GET /ui/api/inbox/evt-a/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "GET /ui/api/inbox/evt-b/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () =>
          jsonResponse({ outcome: "GRANTED", message: "done" }),
      },
      () => {
        closed = true;
      },
    );
    model.cards = [CARD_A, CARD_B];
    model.isListLoaded = true;
    await model.select("evt-a");

    await model.approve();

    const grantCall = calls.find((call) => call.url.includes("/grant"));
    expect(grantCall).toBeDefined();
    const form = grantCall?.init?.body as FormData;
    expect(form.getAll("permissions")).toEqual(["slack-read-all"]);
    expect(form.get("account")).toBe("");
    // Advanced to the surviving request rather than closing.
    expect(model.selectedId).toBe("evt-b");
    expect(closed).toBe(false);
  });

  it("submits only the wildcard when a specific permission was ticked before it", async () => {
    const model = makeModel(
      {
        "GET /ui/api/inbox": () => jsonResponse({ cards: [] }),
        "GET /ui/api/inbox/evt-a/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () =>
          jsonResponse({ outcome: "GRANTED" }),
      },
      () => undefined,
    );
    await model.select("evt-a");
    // The detail arrives with the specific permission already ticked, so
    // ticking the wildcard second is the order the disabled checkboxes leave
    // in the set.
    expect([...model.checkedPermissions]).toEqual(["slack-read-all"]);
    model.checkedPermissions.add("any");

    await model.approve();

    const form = calls.find((call) => call.url.includes("/grant"))?.init
      ?.body as FormData;
    expect(form.getAll("permissions")).toEqual(["any"]);
  });


  it("shows the credential form as soon as an account that needs credentials is selected", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
    });

    await model.select("evt-a");

    // No Approve click needed: the form is part of the detail.
    expect(model.manualCredentialsPrompt()).toEqual(AWS_PROMPT);
    expect(model.isManualAccountNameNeeded()).toBe(false);
    // ...and Approve stays disabled while it is empty.
    expect(model.isApproveAllowed()).toBe(false);
    expect(calls.filter((call) => call.url.includes("/grant"))).toEqual([]);
  });

  it("hides the credential form when a connected account is selected instead", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
    });
    await model.select("evt-a");

    model.selectedAccount = "alice@x";

    expect(model.manualCredentialsPrompt()).toBeNull();
    expect(model.isApproveAllowed()).toBe(true);
  });

  it("never shows a credential form for a service that signs in through a browser", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
    });

    await model.select("evt-a");

    expect(model.manualCredentialsPrompt()).toBeNull();
  });

  it("enables Approve once every credential input is filled in", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
    });
    await model.select("evt-a");

    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    expect(model.isApproveAllowed()).toBe(false);
    model.manualCredentialValues["secret-access-key"] = "   ";
    expect(model.isApproveAllowed()).toBe(false);
    model.manualCredentialValues["secret-access-key"] = "shh-9013";
    expect(model.isApproveAllowed()).toBe(true);
  });

  it("submits the typed credential values with the approve", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
      "POST /requests/evt-a/grant": () => jsonResponse({ outcome: "GRANTED" }),
      "GET /ui/api/inbox": () => jsonResponse({ cards: [] }),
    });
    await model.select("evt-a");
    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    model.manualCredentialValues["secret-access-key"] = "shh-9013";

    await model.approve();

    const grantCall = calls.find((call) => call.url.includes("/grant"));
    const form = grantCall?.init?.body as FormData;
    expect(form.get("manual_credentials")).toBe(
      JSON.stringify({ "access-key-id": "AKIA-4471", "secret-access-key": "shh-9013" }),
    );
    expect(form.get("account")).toBe("bob@x");
    expect(form.get("account_name")).toBe("");
    expect(form.getAll("permissions")).toEqual(["slack-read-all"]);
  });

  it("requires a name for the new account when the selected choice needs one", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
    });
    await model.select("evt-a");
    model.selectedAccount = ":new-account";
    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    model.manualCredentialValues["secret-access-key"] = "shh-9013";

    expect(model.isManualAccountNameNeeded()).toBe(true);
    expect(model.isApproveAllowed()).toBe(false);
    model.manualAccountName = "work";
    expect(model.isApproveAllowed()).toBe(true);
  });

  it("replaces the form's instruction with the reason a rejected attempt gives", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
      "POST /requests/evt-a/grant": () =>
        jsonResponse({
          outcome: "NEEDS_MANUAL_CREDENTIALS",
          message: "AWS did not accept those credentials.",
          manual_credentials: { ...AWS_PROMPT, message: "AWS did not accept those credentials." },
        }),
    });
    await model.select("evt-a");
    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    model.manualCredentialValues["secret-access-key"] = "shh-9013";

    await model.approve();

    expect(model.manualCredentialsPrompt()?.message).toBe("AWS did not accept those credentials.");
    // The typed values survive so one field can be corrected and retried.
    expect(model.manualCredentialValues["access-key-id"]).toBe("AKIA-4471");
    expect(model.isApproveBusy).toBe(false);
    expect(model.errorMessage).toBeNull();
    expect(model.isApproveAllowed()).toBe(true);
  });

  it("blocks approval when Minds cannot work out which credentials to ask for", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () =>
        jsonResponse({
          detail: {
            ...MANUAL_DETAIL,
            manual_credentials: { parameters: [], message: "Minds cannot work out which credentials to ask for" },
          },
        }),
    });

    await model.select("evt-a");

    expect(model.manualCredentialsPrompt()?.parameters).toEqual([]);
    expect(model.isApproveAllowed()).toBe(false);
  });

  it("drops typed credentials when a request is re-selected", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
    });
    await model.select("evt-a");
    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    model.manualAccountName = "work";

    await model.select("evt-a");

    expect(model.manualCredentialValues).toEqual({});
    expect(model.manualAccountName).toBe("");
    expect(model.manualCredentialsFeedback).toBeNull();
  });

  it("asks the view to scroll the reason into view once per failed approval", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
      "POST /requests/evt-a/grant": () =>
        jsonResponse({
          outcome: "NEEDS_MANUAL_CREDENTIALS",
          message: "AWS did not accept those credentials.",
          manual_credentials: { ...AWS_PROMPT, message: "AWS did not accept those credentials." },
        }),
    });
    await model.select("evt-a");
    // Nothing to scroll to before an attempt has failed.
    expect(model.takePendingFailureScroll()).toBe(false);
    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    model.manualCredentialValues["secret-access-key"] = "shh-9013";

    await model.approve();

    expect(model.takePendingFailureScroll()).toBe(true);
    // Handed out once, so later redraws do not fight the user's own scrolling.
    expect(model.takePendingFailureScroll()).toBe(false);
  });

  it("marks the form as showing a failure only after an attempt comes back", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: MANUAL_DETAIL }),
      "POST /requests/evt-a/grant": () =>
        jsonResponse({
          outcome: "NEEDS_MANUAL_CREDENTIALS",
          message: "AWS did not accept those credentials.",
          manual_credentials: { ...AWS_PROMPT, message: "AWS did not accept those credentials." },
        }),
    });
    await model.select("evt-a");
    expect(model.isManualCredentialsFailureShown()).toBe(false);
    model.manualCredentialValues["access-key-id"] = "AKIA-4471";
    model.manualCredentialValues["secret-access-key"] = "shh-9013";

    await model.approve();

    expect(model.isManualCredentialsFailureShown()).toBe(true);
    // Re-selecting the request drops the failure state with the typed values.
    await model.select("evt-a");
    expect(model.isManualCredentialsFailureShown()).toBe(false);
    expect(model.takePendingFailureScroll()).toBe(false);
  });

  it("also asks for a scroll when the approval fails outright", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
      "POST /requests/evt-a/grant": () => jsonResponse({ outcome: "FAILED", message: "sign-in failed" }),
    });
    await model.select("evt-a");

    await model.approve();

    expect(model.errorMessage).toBe("sign-in failed");
    expect(model.takePendingFailureScroll()).toBe(true);
  });

  it("keeps the request pending and shows the reason on FAILED", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
      "POST /requests/evt-a/grant": () =>
        jsonResponse({ outcome: "FAILED", message: "sign-in failed" }),
    });
    await model.select("evt-a");

    await model.approve();

    expect(model.errorMessage).toBe("sign-in failed");
    expect(model.isApproveBusy).toBe(false);
  });

  it("closes once the request it resolved was the last one pending", async () => {
    let closed = false;
    const model = makeModel(
      {
        "GET /ui/api/inbox": () => jsonResponse({ cards: [] }),
        "GET /ui/api/inbox/evt-a/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () =>
          jsonResponse({ outcome: "GRANTED" }),
      },
      () => {
        closed = true;
      },
    );
    model.cards = [CARD_A];
    model.isListLoaded = true;
    await model.select("evt-a");

    await model.approve();

    expect(closed).toBe(true);
  });

  it("fires a keepalive deny, fades the card, and advances", async () => {
    const model = makeModel({
      "GET /ui/api/inbox": () =>
        jsonResponse({ cards: [CARD_A, CARD_B] }),
      "GET /ui/api/inbox/evt-b/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
      "POST /requests/evt-a/deny": () => jsonResponse({ outcome: "DENIED" }),
    });
    model.cards = [CARD_A, CARD_B];
    model.isListLoaded = true;
    model.selectedId = "evt-a";

    model.deny();
    await vi.waitFor(() => {
      expect(model.selectedId).toBe("evt-b");
    });

    expect(model.denyingIds.has("evt-a")).toBe(true);
    const denyCall = calls.find((call) => call.url.includes("/deny"));
    expect(denyCall?.init?.keepalive).toBe(true);
  });

  it("advances a deny forwards, the same way an approve goes", async () => {
    // Deny marks the request as denying before it advances, so a successor
    // picked from the already-filtered queue loses the position it is measured
    // from and lands on the head of the queue -- sending the user backwards to
    // a request they had read past, while an approve on that same request went
    // forwards.
    const cards = [CARD_A, CARD_B, CARD_C, CARD_D];
    const model = makeModel({
      "GET /ui/api/inbox": () => jsonResponse({ cards }),
      "GET /ui/api/inbox/evt-d/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
      "POST /requests/evt-c/deny": () => jsonResponse({ outcome: "DENIED" }),
    });
    model.cards = cards;
    model.isListLoaded = true;
    model.selectedId = "evt-c";

    model.deny();

    await vi.waitFor(() => {
      expect(model.selectedId).toBe("evt-d");
    });
  });

  it("announces each verdict against the workspace the request belongs to", async () => {
    const resolved: ResolvedRequest[] = [];
    const model = makeModel(
      {
        "GET /ui/api/inbox": () =>
          jsonResponse({ cards: [CARD_B] }),
        "GET /ui/api/inbox/evt-a/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "GET /ui/api/inbox/evt-b/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () =>
          jsonResponse({ outcome: "GRANTED" }),
        "POST /requests/evt-b/deny": () => jsonResponse({ outcome: "DENIED" }),
      },
      undefined,
      (announced) => resolved.push(announced),
    );
    model.cards = [CARD_A, CARD_B];
    model.isListLoaded = true;
    await model.select("evt-a");

    await model.approve();
    model.deny();

    // Taken from each request's own card, not from whatever detail happens to
    // be on screen when the verdict lands: PREDEFINED_DETAIL names the filing
    // sibling ("agent-1"), which is never the workspace the user is looking at.
    expect(resolved).toEqual([
      { requestId: "evt-a", agentId: "agent-alpha", verdict: "granted" },
      { requestId: "evt-b", agentId: "agent-beta", verdict: "denied" },
    ]);
  });

  it("says nothing about a request the server left pending", async () => {
    const resolved: ResolvedRequest[] = [];
    const model = makeModel(
      {
        "GET /ui/api/inbox/evt-a/detail": () =>
          jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () =>
          jsonResponse({ outcome: "FAILED", message: "upstream refused" }),
      },
      undefined,
      (announced) => resolved.push(announced),
    );
    await model.select("evt-a");

    await model.approve();

    expect(model.errorMessage).toBe("upstream refused");
    expect(resolved).toEqual([]);
  });

  it("advances off a selection another window resolved, never leaving its form up", async () => {
    let listCalls = 0;
    const model = makeModel({
      "GET /ui/api/inbox": () => {
        listCalls += 1;
        return jsonResponse({ cards: [CARD_B] });
      },
      "GET /ui/api/inbox/evt-b/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
    });
    model.cards = [CARD_A, CARD_B];
    model.isListLoaded = true;
    model.selectedId = "evt-a";

    await model.refreshIfPendingChanged(["evt-b"]);
    // Same set again: no extra fetch.
    await model.refreshIfPendingChanged(["evt-b"]);

    expect(listCalls).toBe(1);
    expect(model.selectedId).toBe("evt-b");
    expect(model.detail?.kind).toBe("predefined");
  });

  it("leaves a stale link on its notice when an unrelated request arrives", async () => {
    // The popup was opened straight onto a request that was already gone (a
    // stale in-chat card), so it is showing "no longer available". A request
    // arriving for some other machine must not swap that for a live
    // Approve/Deny form the user never asked to review.
    const model = makeModel({
      "GET /ui/api/inbox": () => jsonResponse({ cards: [CARD_B] }),
      "GET /ui/api/inbox/evt-stale/detail": () => new Response("", { status: 404 }),
    });
    model.isListLoaded = true;
    await model.select("evt-stale");
    expect(model.detail?.kind).toBe("unavailable");

    await model.refreshIfPendingChanged(["evt-b"]);

    expect(model.selectedId).toBe("evt-stale");
    expect(model.detail?.kind).toBe("unavailable");
  });

  it("closes when the only pending request is resolved somewhere else", async () => {
    let closed = false;
    const model = makeModel(
      {
        "GET /ui/api/inbox": () => jsonResponse({ cards: [] }),
      },
      () => {
        closed = true;
      },
    );
    model.cards = [CARD_A];
    model.isListLoaded = true;
    model.selectedId = "evt-a";

    await model.refreshIfPendingChanged([]);

    expect(closed).toBe(true);
  });

  it("leaves a running approval's dialog alone and reconciles once it finishes", async () => {
    let listCalls = 0;
    const model = makeModel({
      "GET /ui/api/inbox": () => {
        listCalls += 1;
        return jsonResponse({ cards: [CARD_B] });
      },
      "GET /ui/api/inbox/evt-b/detail": () =>
        jsonResponse({ detail: PREDEFINED_DETAIL }),
    });
    model.cards = [CARD_A, CARD_B];
    model.isListLoaded = true;
    model.selectedId = "evt-a";
    // An approval waiting on a browser sign-in must not have the dialog it is
    // submitting swapped out from under it.
    model.isApproveBusy = true;

    await model.refreshIfPendingChanged(["evt-b"]);
    expect(model.selectedId).toBe("evt-a");
    expect(listCalls).toBe(0);

    model.isApproveBusy = false;
    await model.refreshIfPendingChanged(["evt-b"]);
    expect(model.selectedId).toBe("evt-b");
  });

  it("skips the redundant list load for the pending set an open already showed", async () => {
    let listCalls = 0;
    const model = makeModel({
      "GET /ui/api/inbox": () => {
        listCalls += 1;
        return jsonResponse({ cards: [CARD_A] });
      },
    });
    model.markPendingSetSeen(["evt-a"]);
    await model.loadList();

    await model.refreshIfPendingChanged(["evt-a"]);

    expect(listCalls).toBe(1);
  });
});
