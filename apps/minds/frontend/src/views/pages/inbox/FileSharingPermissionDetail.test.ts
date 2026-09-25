import m from "mithril";
import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import type { FileSharingPermissionDetail } from "../../../models/inbox";
import { FileSharingPermissionDetailView } from "./FileSharingPermissionDetail";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, withAttr } from "../../../testing";

const DETAIL: FileSharingPermissionDetail = {
  kind: "file_sharing",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "keep the project available offline",
  file_path: "/home/user/project",
  access: "WRITE",
  access_human_label: "read & write",
  allowed_roots: ["/home/user", "/tmp/shared"],
  home_dir: "/home/user",
  is_sync_supported: true,
  is_sync_requested: true,
  sync_conflict: "NEWER",
  sync_unavailable_reason: "",
};

function makeModel(detail: FileSharingPermissionDetail): InboxModel {
  const model = new InboxModel();
  model.detail = detail;
  model.selectedId = detail.request_id;
  model.filePathValue = detail.file_path;
  model.isSharePathSynced =
    detail.is_sync_requested && detail.sync_unavailable_reason === "";
  model.sharePathSyncConflict = detail.sync_conflict;
  return model;
}

/** Render the dialog without a DOM: instantiate the closure component and
 * call view() directly. The root is a PermissionsShell vnode, so the dialog's
 * own markup is its `body` attr. */
function renderBody(
  detail: FileSharingPermissionDetail,
  model = makeModel(detail),
): m.Children {
  const instance = FileSharingPermissionDetailView() as unknown as m.Component;
  const vnode = m(instance, {
    model,
    detail,
  } as unknown as m.Attributes) as m.Vnode;
  const root = (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  );
  return (root.attrs as unknown as { body: m.Children }).body;
}

const syncSwitches = (node: unknown): AnyVnode[] =>
  withAttr(node, "data-sync-path");
const conflictSelects = (node: unknown): AnyVnode[] =>
  withAttr(node, "data-conflict-path");

describe("FileSharingPermissionDetailView sync option", () => {
  it("offers the same sync setting the Local files card draws, switched on when the agent asked", () => {
    const body = renderBody(DETAIL);
    expect(syncSwitches(body)).toHaveLength(1);
    expect(attrsOf(syncSwitches(body)[0]).checked).toBe(true);
    const text = allText(body);
    expect(text).toContain("Keep a synchronized copy on the machine");
    expect(text).toContain("The agent asked for this.");
    // Two-way, so the clash question is real and asked.
    expect(conflictSelects(body)).toHaveLength(1);
  });

  it("says nothing about the agent when the sync was the user's idea", () => {
    const body = renderBody({ ...DETAIL, is_sync_requested: false });
    expect(attrsOf(syncSwitches(body)[0]).checked).toBe(false);
    expect(allText(body)).not.toContain("The agent asked for this.");
    // Off, so nothing more to configure yet.
    expect(conflictSelects(body)).toHaveLength(0);
  });

  it("drops the clash question from a read-only request, which syncs one way", () => {
    const body = renderBody({ ...DETAIL, access: "READ" });
    expect(attrsOf(syncSwitches(body)[0]).checked).toBe(true);
    expect(conflictSelects(body)).toHaveLength(0);
    expect(allText(body)).toContain("in one direction");
  });

  it("greys the switch out with the reason when the path cannot be synced", () => {
    const reason =
      "Only folders can be synced, and /home/user/project is a file.";
    const body = renderBody({ ...DETAIL, sync_unavailable_reason: reason });
    const [toggle] = syncSwitches(body);
    expect(attrsOf(toggle).disabled).toBe(true);
    expect(attrsOf(toggle).checked).toBe(false);
    expect(allText(body)).toContain(reason);
  });

  it("offers no sync option at all when this build cannot run one", () => {
    const body = renderBody({ ...DETAIL, is_sync_supported: false });
    expect(syncSwitches(body)).toHaveLength(0);
    expect(allText(body)).not.toContain("Keep a synchronized copy");
  });

  it("carries a flip and a clash choice back to the model", () => {
    const model = makeModel(DETAIL);
    const body = renderBody(DETAIL, model);
    const flip = attrsOf(syncSwitches(body)[0]).onchange as (
      event: Event,
    ) => void;
    flip({ target: { checked: false } } as unknown as Event);
    expect(model.isSharePathSynced).toBe(false);
    const pick = attrsOf(conflictSelects(body)[0]).onchange as (
      event: Event,
    ) => void;
    pick({ target: { value: "WORKSPACE" } } as unknown as Event);
    expect(model.sharePathSyncConflict).toBe("WORKSPACE");
  });
});
