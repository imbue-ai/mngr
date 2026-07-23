import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  FileSharingPermissionDetail,
  InboxDetailPayload,
  PredefinedPermissionDetail,
  WorkspacePermissionDetail,
} from "../chrome_state";
import {
  expandSharePathHome,
  InboxDetail,
  InboxDetailController,
  isSharePathWithinRoots,
  type InboxDetailListGlue,
} from "./InboxDetail";

const REQUEST_ID = "evt-1";

function recordingGlue(): {
  glue: InboxDetailListGlue;
  advanceCalls: (string | null)[];
  denyingMarks: string[];
} {
  const advanceCalls: (string | null)[] = [];
  const denyingMarks: string[] = [];
  return {
    advanceCalls,
    denyingMarks,
    glue: {
      getSelectedId: () => REQUEST_ID,
      markDenying: (id) => denyingMarks.push(id),
      advanceAfterResolution: (id) => {
        advanceCalls.push(id);
        return Promise.resolve();
      },
    },
  };
}

function mount(payload: InboxDetailPayload, glue: InboxDetailListGlue): { root: HTMLElement; controller: InboxDetailController } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const controller = new InboxDetailController(payload, glue, () => root);
  m.mount(root, { view: () => m(InboxDetail, { controller }) });
  return { root, controller };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

function predefined(overrides: Partial<PredefinedPermissionDetail> = {}): PredefinedPermissionDetail {
  return {
    kind: "predefined",
    agent_id: "agent-1",
    request_id: REQUEST_ID,
    ws_name: "alpha",
    rationale: "let me read",
    display_name: "Slack",
    permission_schemas: ["any", "slack-read", "slack-write"],
    description_by_permission_name: { "slack-read": "Read messages" },
    checked_permissions: ["slack-read"],
    wildcard_permission: "any",
    wildcard_label: "all",
    will_open_browser: true,
    unknown_scope: "",
    ...overrides,
  };
}

function fileSharing(overrides: Partial<FileSharingPermissionDetail> = {}): FileSharingPermissionDetail {
  return {
    kind: "file_sharing",
    agent_id: "agent-1",
    request_id: REQUEST_ID,
    ws_name: "alpha",
    rationale: "read the doc",
    file_path: "/Users/alice/notes.md",
    access: "READ",
    access_human_label: "read-only",
    allowed_roots: ["/Users/alice"],
    home_dir: "/Users/alice",
    ...overrides,
  };
}

function workspace(overrides: Partial<WorkspacePermissionDetail> = {}): WorkspacePermissionDetail {
  return {
    kind: "workspace",
    agent_id: "agent-1",
    request_id: REQUEST_ID,
    ws_name: "alpha",
    rationale: "manage them",
    display_name: "Target WS",
    verbs: [
      { permission: "minds-workspaces-read", display_name: "read", description: "list them", is_targeted: false, is_checked: false },
      { permission: "minds-workspaces-destroy", display_name: "destroy", description: "delete", is_targeted: true, is_checked: true },
    ],
    target_workspace_id: "agent-target",
    target_workspace_name: "Target WS",
    show_target_choice: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ outcome: "GRANTED" })));
});

afterEach(() => {
  document.body.innerHTML = "";
  delete window.minds;
  vi.unstubAllGlobals();
});

describe("share-path helpers", () => {
  it("expands a leading ~ to the home dir but leaves ~user alone", () => {
    expect(expandSharePathHome("~/docs", "/home/a")).toBe("/home/a/docs");
    expect(expandSharePathHome("~", "/home/a")).toBe("/home/a");
    expect(expandSharePathHome("~bob/x", "/home/a")).toBe("~bob/x");
    expect(expandSharePathHome("/abs", "/home/a")).toBe("/abs");
  });

  it("matches paths at or beneath a root, case-insensitively", () => {
    expect(isSharePathWithinRoots("/home/a/x", ["/home/a"])).toBe(true);
    expect(isSharePathWithinRoots("/HOME/A", ["/home/a"])).toBe(true);
    expect(isSharePathWithinRoots("/home/ab", ["/home/a"])).toBe(false);
    expect(isSharePathWithinRoots("", ["/home/a"])).toBe(false);
  });
});

describe("predefined permission detail", () => {
  it("renders the simple summary and Adjust reveals the editor with the wildcard label", () => {
    const { root, controller } = mount(predefined(), recordingGlue().glue);
    expect(root.querySelector("#permissions-simple-view")).not.toBeNull();
    // Only the requested permission is summarized.
    expect(root.textContent).toContain("slack-read");
    controller.isEditorShown = true;
    m.redraw.sync();
    expect(root.querySelector("#permissions-editor-view")).not.toBeNull();
    // The wildcard shows as "all", never the raw "any".
    const codes = Array.from(root.querySelectorAll("#permissions-editor-view code")).map((el) => el.textContent);
    expect(codes).toContain("all");
    expect(codes).not.toContain("any");
  });

  it("makes the wildcard mutually exclusive: ticking 'all' disables the specific boxes", () => {
    const { root, controller } = mount(predefined({ checked_permissions: [] }), recordingGlue().glue);
    controller.isEditorShown = true;
    m.redraw.sync();
    const wildcard = root.querySelector('input[value="any"]') as HTMLInputElement;
    wildcard.checked = true;
    wildcard.dispatchEvent(new Event("change"));
    m.redraw.sync();
    const specific = root.querySelector('input[value="slack-read"]') as HTMLInputElement;
    expect(specific.disabled).toBe(true);
    // The submitted set collapses to just the wildcard.
    expect(controller.submittedPermissions()).toEqual(["any"]);
  });

  it("gates Approve on a selection existing", () => {
    const enabled = mount(predefined(), recordingGlue().glue);
    expect((enabled.root.querySelector("#permissions-approve-btn") as HTMLButtonElement).disabled).toBe(false);
    document.body.innerHTML = "";
    const disabled = mount(predefined({ checked_permissions: [] }), recordingGlue().glue);
    expect((disabled.root.querySelector("#permissions-approve-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("renders the unknown-scope deny-only variant", () => {
    const { advanceCalls, glue } = recordingGlue();
    const { root } = mount(
      predefined({ unknown_scope: "mystery-scope", permission_schemas: [], checked_permissions: [] }),
      glue,
    );
    expect(root.textContent).toContain("Unknown scope");
    expect(root.textContent).toContain("mystery-scope");
    expect(root.querySelector("#permissions-approve-btn")).toBeNull();
    (root.querySelector("#permissions-deny-btn") as HTMLButtonElement).click();
    expect(advanceCalls).toEqual([REQUEST_ID]);
  });
});

describe("grant submission outcomes", () => {
  it("advances on GRANTED", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ outcome: "GRANTED" }));
    vi.stubGlobal("fetch", fetchMock);
    const { advanceCalls, glue } = recordingGlue();
    const { root } = mount(predefined(), glue);
    (root.querySelector("#permissions-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    expect(fetchMock).toHaveBeenCalledWith(
      `/requests/${REQUEST_ID}/grant`,
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    // The submitted FormData carries the checked permission.
    const body = fetchMock.mock.calls[0][1].body as FormData;
    expect(body.getAll("permissions")).toEqual(["slack-read"]);
    expect(advanceCalls).toEqual([REQUEST_ID]);
  });

  it("shows the manual-credentials box on NEEDS_MANUAL_CREDENTIALS without advancing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ outcome: "NEEDS_MANUAL_CREDENTIALS", message: "set your key", set_credentials_example: "export KEY=..." }),
      ),
    );
    const { advanceCalls, glue } = recordingGlue();
    const { root } = mount(predefined(), glue);
    (root.querySelector("#permissions-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    m.redraw.sync();
    expect(root.querySelector("#permissions-manual-credentials-message")?.textContent).toBe("set your key");
    expect(root.querySelector("#permissions-manual-credentials-command")?.textContent).toBe("export KEY=...");
    expect(advanceCalls).toEqual([]);
    // Approve re-enabled for a retry.
    expect((root.querySelector("#permissions-approve-btn") as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows a retryable error on FAILED", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ outcome: "FAILED", message: "sign-in cancelled" })));
    const { advanceCalls, glue } = recordingGlue();
    const { root } = mount(predefined(), glue);
    (root.querySelector("#permissions-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    m.redraw.sync();
    expect(root.querySelector("#permissions-error-message")?.textContent).toBe("sign-in cancelled");
    expect(advanceCalls).toEqual([]);
  });

  it("marks the card denying and fire-and-forget denies to the /deny URL", () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ outcome: "DENIED" }));
    vi.stubGlobal("fetch", fetchMock);
    const { advanceCalls, denyingMarks, glue } = recordingGlue();
    const { root } = mount(predefined(), glue);
    (root.querySelector("#permissions-deny-btn") as HTMLButtonElement).click();
    expect(denyingMarks).toEqual([REQUEST_ID]);
    expect(fetchMock).toHaveBeenCalledWith(
      `/requests/${REQUEST_ID}/deny`,
      expect.objectContaining({ method: "POST", keepalive: true }),
    );
    expect(advanceCalls).toEqual([REQUEST_ID]);
  });
});

describe("file-sharing permission detail", () => {
  it("blocks Approve for a path outside the roots and shows the hint", () => {
    const { root, controller } = mount(fileSharing(), recordingGlue().glue);
    expect((root.querySelector("#permissions-approve-btn") as HTMLButtonElement).disabled).toBe(false);
    const input = root.querySelector("#file-sharing-path-input") as HTMLInputElement;
    input.value = "/etc/passwd";
    input.dispatchEvent(new Event("input"));
    m.redraw.sync();
    expect(root.querySelector("#file-sharing-path-hint")).not.toBeNull();
    expect((root.querySelector("#permissions-approve-btn") as HTMLButtonElement).disabled).toBe(true);
    // ~-expansion keeps a home-relative path valid.
    input.value = "~/notes.md";
    input.dispatchEvent(new Event("input"));
    m.redraw.sync();
    expect(root.querySelector("#file-sharing-path-hint")).toBeNull();
    expect((root.querySelector("#permissions-approve-btn") as HTMLButtonElement).disabled).toBe(false);
    void controller;
  });

  it("hides the native pickers without the Electron bridge and shows them with it", () => {
    const withoutBridge = mount(fileSharing(), recordingGlue().glue);
    expect(withoutBridge.root.querySelector("#file-sharing-browse-file-btn")).toBeNull();
    document.body.innerHTML = "";
    window.minds = { showFilePicker: () => Promise.resolve(null) } as unknown as typeof window.minds;
    const withBridge = mount(fileSharing(), recordingGlue().glue);
    expect(withBridge.root.querySelector("#file-sharing-browse-file-btn")).not.toBeNull();
    expect(withBridge.root.querySelector("#file-sharing-browse-folder-btn")).not.toBeNull();
  });

  it("submits the edited path with the grant", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ outcome: "GRANTED" }));
    vi.stubGlobal("fetch", fetchMock);
    const { root } = mount(fileSharing(), recordingGlue().glue);
    const input = root.querySelector("#file-sharing-path-input") as HTMLInputElement;
    input.value = "/Users/alice/other.md";
    input.dispatchEvent(new Event("input"));
    m.redraw.sync();
    (root.querySelector("#permissions-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    const body = fetchMock.mock.calls[0][1].body as FormData;
    expect(body.get("file_path")).toBe("/Users/alice/other.md");
    expect(body.getAll("permissions")).toEqual(["file-sharing"]);
  });
});

describe("workspace permission detail", () => {
  it("renders both verb groups, pre-checks the requested verb, and offers the target choice", () => {
    const { root } = mount(workspace(), recordingGlue().glue);
    expect(root.textContent).toContain("General permissions");
    expect(root.textContent).toContain("Workspace-specific permissions");
    expect((root.querySelector('input[value="minds-workspaces-destroy"]') as HTMLInputElement).checked).toBe(true);
    // Both target radios present when a target was named.
    expect(root.querySelector('input[name="target_scope"][value="selected"]')).not.toBeNull();
    expect(root.querySelector('input[name="target_scope"][value="all"]')).not.toBeNull();
  });

  it("submits the target scope with the grant", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ outcome: "GRANTED" }));
    vi.stubGlobal("fetch", fetchMock);
    const { root } = mount(workspace(), recordingGlue().glue);
    (root.querySelector('input[name="target_scope"][value="all"]') as HTMLInputElement).dispatchEvent(new Event("change"));
    m.redraw.sync();
    (root.querySelector("#permissions-form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushAsync();
    const body = fetchMock.mock.calls[0][1].body as FormData;
    expect(body.get("target_scope")).toBe("all");
  });

  it("shows the broad-only caution and a single 'all' radio when no target was named", () => {
    const { root } = mount(
      workspace({ show_target_choice: false, target_workspace_id: "", target_workspace_name: "", display_name: "workspaces" }),
      recordingGlue().glue,
    );
    expect(root.querySelector('input[name="target_scope"][value="selected"]')).toBeNull();
    expect(root.querySelector('input[name="target_scope"][value="all"]')).not.toBeNull();
    expect(root.textContent).toContain("did not name a specific workspace");
  });
});

describe("unavailable detail", () => {
  it("renders the heading with the optional message", () => {
    const { root } = mount({ kind: "unavailable", message: "already granted" }, recordingGlue().glue);
    expect(root.textContent).toContain("no longer available");
    expect(root.textContent).toContain("already granted");
  });
});
