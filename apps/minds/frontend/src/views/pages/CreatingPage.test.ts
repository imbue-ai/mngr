import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CreateAttemptDetail } from "../../models/create";
import { allText, attrsOf, collectVnodes, jsonResponse } from "../../testing";
import { CreatingPage, failureGuidance, failureTurn } from "./CreatingPage";

describe("failureGuidance", () => {
  it("explains the two recognized authentication failures and nothing else", () => {
    expect(allText(failureGuidance("GITHUB_AUTH_REQUIRED"))).toContain("Install the GitHub app");
    expect(allText(failureGuidance("GIT_AUTH_REQUIRED"))).toContain("rejected anonymous access");
    expect(failureGuidance("")).toBeNull();
    expect(failureGuidance("SOMETHING_ELSE")).toBeNull();
  });
});

describe("failureTurn", () => {
  it("keeps the ids the e2e workspace runner polls, around the failure text", () => {
    const turn = failureTurn("alpha", "clone blew up", true);
    const nodes = collectVnodes(turn);
    const view = nodes.find((node) => attrsOf(node).id === "failure-view");
    const message = nodes.find((node) => attrsOf(node).id === "error-message");
    expect(view).toBeDefined();
    expect(message).toBeDefined();
    expect(allText(message)).toBe("Could not create alpha: clone blew up");
  });
});

describe("CreatingPage across a route change", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function failedRecord(createAttemptId: string): CreateAttemptDetail {
    return {
      kind: "record",
      live: null,
      record: {
        state: "failed",
        workspace_name: `workspace-${createAttemptId}`,
        error: "boom",
        error_kind: "",
        log_tail: [],
        provider_label: "",
        request: {
          display_name: `workspace-${createAttemptId}`,
          launch_mode: "LIMA",
          cloud_account: "",
          backup_provider: "CONFIGURE_LATER",
          region: "",
          instance_type: "",
          repository: "https://example.com/repo.git",
          branch: "",
        },
      },
    };
  }

  // The router hands the page its route params as attrs; the page's own type
  // declares none, so the vnode is built the way mithril would at runtime.
  function vnodeFor(createAttemptId: string): m.VnodeDOM {
    return m(CreatingPage, { agentId: createAttemptId } as m.Attributes) as m.VnodeDOM;
  }

  it("picks up the attempt the route now names instead of keeping the first one", async () => {
    // The router reuses the page instance when the retry routes
    // /creating/<a> -> /creating/<b>, so oninit does not run again.
    const fetched: string[] = [];
    vi.stubGlobal("fetch", (url: string) => {
      fetched.push(url);
      const createAttemptId = url.slice(url.lastIndexOf("/") + 1);
      return Promise.resolve(jsonResponse(failedRecord(createAttemptId)));
    });
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = CreatingPage(vnodeFor("create-attempt-a"));
    const render = (vnode: m.Vnode): m.Vnode => (page.view as (v: m.Vnode) => m.Vnode).call(page, vnode);

    (page.oninit as (v: m.VnodeDOM) => void).call(page, vnodeFor("create-attempt-a"));
    await vi.waitFor(() => expect(allText(render(vnodeFor("create-attempt-a")))).toContain("workspace-create-attempt-a"));

    const later = vnodeFor("create-attempt-b");
    (page.onbeforeupdate as (v: m.VnodeDOM, old: m.VnodeDOM) => void).call(page, later, later);
    // The same render already shows the new attempt loading, not a's failure.
    const loading = render(later);
    expect(attrsOf(loading)["data-agent-id"]).toBe("create-attempt-b");
    expect(allText(loading)).not.toContain("workspace-create-attempt-a");
    await vi.waitFor(() => expect(allText(render(later))).toContain("workspace-create-attempt-b"));
    expect(fetched).toEqual(["/ui/api/create/attempts/create-attempt-a", "/ui/api/create/attempts/create-attempt-b"]);
  });
});
