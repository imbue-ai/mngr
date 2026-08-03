import { beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../testing";
import {
  InboxModel,
  expandSharePathHome,
  isPermissionCheckboxDisabled,
  isSharePathWithinRoots,
} from "./inbox";
import type { FileSharingPermissionDetail, PredefinedPermissionDetail } from "./inbox";

const CARD_A = { id: "evt-a", kind_label: "permission", ws_name: "alpha", display_name: "Slack", accent: "#123456" };
const CARD_B = { id: "evt-b", kind_label: "permission", ws_name: "beta", display_name: "Gmail", accent: "#654321" };

const PREDEFINED_DETAIL: PredefinedPermissionDetail = {
  kind: "predefined",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "read the channel",
  scope: "slack-api",
  display_name: "Slack",
  permission_schemas: ["any", "slack-read-all"],
  description_by_permission_name: { "slack-read-all": "Read everything" },
  checked_permissions: ["slack-read-all"],
  account_choices: [{ value: "", label: "Default account", hint: "" }],
  selected_account_value: "",
  new_account_value: ":new-account",
  wildcard_permission: "any",
  wildcard_label: "all",
  will_open_browser: false,
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
    expect(expandSharePathHome("~/notes.txt", "/home/u")).toBe("/home/u/notes.txt");
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
    expect(isPermissionCheckboxDisabled("slack-read-all", "any", checked)).toBe(true);
    expect(isPermissionCheckboxDisabled("any", "any", checked)).toBe(false);
    expect(isPermissionCheckboxDisabled("slack-read-all", "any", new Set(["slack-read-all"]))).toBe(false);
  });
});

describe("InboxModel", () => {
  let calls: Array<{ url: string; init?: RequestInit }>;

  function makeModel(
    responses: Record<string, () => Response>,
    onClose?: () => void,
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
    });
  }

  beforeEach(() => {
    calls = [];
  });

  it("loads the card list and seeds detail state on selection", async () => {
    const model = makeModel({
      "GET /ui/api/inbox": () => jsonResponse({ cards: [CARD_A, CARD_B], auto_open: true }),
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
    });
    await model.loadList();
    expect(model.cards.map((card) => card.id)).toEqual(["evt-a", "evt-b"]);

    await model.select("evt-a");

    expect(model.detail?.kind).toBe("predefined");
    expect([...model.checkedPermissions]).toEqual(["slack-read-all"]);
    expect(model.selectedAccount).toBe("");
    expect(model.isApproveAllowed()).toBe(true);
  });

  it("surfaces a failed list load and lets the pending-set reconciliation retry it", async () => {
    let isServerUp = false;
    const model = makeModel({
      "GET /ui/api/inbox": () =>
        isServerUp ? jsonResponse({ cards: [CARD_A], auto_open: true }) : jsonResponse({ error: "boom" }, 503),
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
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: FILE_DETAIL }),
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
        "GET /ui/api/inbox": () => jsonResponse({ cards: [CARD_B], auto_open: true }),
        "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
        "GET /ui/api/inbox/evt-b/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () => jsonResponse({ outcome: "GRANTED", message: "done" }),
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

  it("shows manual credentials without resolving when the grant needs them", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
      "POST /requests/evt-a/grant": () =>
        jsonResponse({
          outcome: "NEEDS_MANUAL_CREDENTIALS",
          message: "manual required",
          set_credentials_example: "latchkey auth set slack",
        }),
    });
    await model.select("evt-a");

    await model.approve();

    expect(model.manualCredentials).toEqual({ message: "manual required", command: "latchkey auth set slack" });
    expect(model.isApproveBusy).toBe(false);
    expect(model.errorMessage).toBeNull();
  });

  it("keeps the request pending and shows the reason on FAILED", async () => {
    const model = makeModel({
      "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
      "POST /requests/evt-a/grant": () => jsonResponse({ outcome: "FAILED", message: "sign-in failed" }),
    });
    await model.select("evt-a");

    await model.approve();

    expect(model.errorMessage).toBe("sign-in failed");
    expect(model.isApproveBusy).toBe(false);
  });

  it("dismisses instead of advancing when not in keep-open mode", async () => {
    let closed = false;
    const model = makeModel(
      {
        "GET /ui/api/inbox/evt-a/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
        "POST /requests/evt-a/grant": () => jsonResponse({ outcome: "GRANTED" }),
      },
      () => {
        closed = true;
      },
    );
    model.isKeepOpen = false;
    model.cards = [CARD_A, CARD_B];
    await model.select("evt-a");

    await model.approve();

    expect(closed).toBe(true);
  });

  it("fires a keepalive deny, fades the card, and advances", async () => {
    const model = makeModel({
      "GET /ui/api/inbox": () => jsonResponse({ cards: [CARD_A, CARD_B], auto_open: true }),
      "GET /ui/api/inbox/evt-b/detail": () => jsonResponse({ detail: PREDEFINED_DETAIL }),
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

  it("refreshes from a changed pending set and re-fetches a vanished selection", async () => {
    let listCalls = 0;
    const model = makeModel({
      "GET /ui/api/inbox": () => {
        listCalls += 1;
        return jsonResponse({ cards: [CARD_B], auto_open: true });
      },
      "GET /ui/api/inbox/evt-a/detail": () =>
        jsonResponse({ detail: { kind: "unavailable", message: "It has already been processed." } }),
    });
    model.selectedId = "evt-a";

    await model.refreshIfPendingChanged(["evt-b"]);
    // Same set again: no extra fetch.
    await model.refreshIfPendingChanged(["evt-b"]);

    expect(listCalls).toBe(1);
    expect(model.detail?.kind).toBe("unavailable");
  });

  it("posts the auto-open preference fire-and-forget", () => {
    const model = makeModel({
      "POST /ui/api/inbox/auto-open": () => jsonResponse({ ok: true }),
    });

    model.setAutoOpen(false);

    expect(model.autoOpen).toBe(false);
    const call = calls[0];
    expect(call.url).toBe("/ui/api/inbox/auto-open");
    expect(JSON.parse(String(call.init?.body))).toEqual({ enabled: false });
  });
});
