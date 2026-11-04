import m from "mithril";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { AppContext } from "../../../app-context";
import {
  clearAppContextForTests,
  registerAppContext,
} from "../../../app-context";
import { InboxModel } from "../../../models/inbox";
import type { RemoteExecutionPermissionDetail } from "../../../models/inbox";
import { REMOTE_EXECUTION_CONSEQUENCES } from "../../components/RemoteExecutionWarning";
import { RemoteExecutionPermissionDetailView } from "./RemoteExecutionPermissionDetail";
import { allText, byId } from "../../../testing";

const DETAIL: RemoteExecutionPermissionDetail = {
  kind: "remote_execution",
  request_id: "evt-rx",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "install the CLI you asked for",
  device_hostname: "kims-laptop",
  is_setting_enabled: true,
  is_workspace_shared: false,
};

beforeEach(() => {
  registerAppContext({
    shell: {
      accountId: "user-7",
      overlayQuery: (params: Record<string, string>) => ({
        ...params,
        account: "user-7",
      }),
    },
  } as unknown as AppContext);
});

afterEach(() => {
  clearAppContextForTests();
});

function makeModel(detail: RemoteExecutionPermissionDetail): InboxModel {
  const model = new InboxModel();
  model.detail = detail;
  model.selectedId = detail.request_id;
  model.checkedPermissions = new Set(["remote-execution"]);
  return model;
}

/** Render the dialog without a DOM: the root is a PermissionsShell vnode, so
 * the dialog's own markup is its `body` attr. */
function renderShellAttrs(
  detail: RemoteExecutionPermissionDetail,
  model = makeModel(detail),
): Record<string, unknown> {
  const instance =
    RemoteExecutionPermissionDetailView() as unknown as m.Component;
  const vnode = m(instance, {
    model,
    detail,
  } as unknown as m.Attributes) as m.Vnode;
  const root = (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  );
  return root.attrs as unknown as Record<string, unknown>;
}

function renderBody(
  detail: RemoteExecutionPermissionDetail,
  model = makeModel(detail),
): m.Children {
  return renderShellAttrs(detail, model).body as m.Children;
}

describe("RemoteExecutionPermissionDetailView", () => {
  it("heads the dialog with the warning, naming the workspace and this computer, and lists the consequences", () => {
    const body = renderBody(DETAIL);

    const banner = byId(body, "remote-execution-dialog-banner");
    expect(allText(banner)).toContain(
      "every agent in alpha run any command on kims-laptop as you",
    );
    const consequences = allText(
      byId(body, "remote-execution-dialog-consequences"),
    );
    for (const consequence of REMOTE_EXECUTION_CONSEQUENCES) {
      expect(consequences).toContain(consequence);
    }
  });

  it("names what approving does on the button and offers nothing to narrow", () => {
    const attrs = renderShellAttrs(DETAIL);

    expect(attrs.headerLabel).toBe("This computer");
    expect(attrs.approveLabel).toBe("Allow commands on this computer");
    expect(attrs.account).toBeUndefined();
    expect(attrs.credentialEntrySwitch).toBeUndefined();
  });

  it("warns when the workspace is shared, and only then", () => {
    expect(
      byId(renderBody(DETAIL), "remote-execution-shared-notice"),
    ).toBeUndefined();

    const shared = renderBody({ ...DETAIL, is_workspace_shared: true });

    expect(allText(byId(shared, "remote-execution-shared-notice"))).toContain(
      "alpha is shared",
    );
  });

  it("withholds Approve and points at Settings while this device's switch is off", () => {
    const detail = { ...DETAIL, is_setting_enabled: false };
    const model = makeModel(detail);

    const body = renderBody(detail, model);

    expect(model.isApproveAllowed()).toBe(false);
    const notice = byId(body, "remote-execution-setting-notice");
    expect(allText(notice)).toContain("Settings > This computer");
    expect(byId(notice, "remote-execution-open-settings")).toBeDefined();
    expect(byId(notice, "remote-execution-recheck")).toBeDefined();
  });

  it("offers Approve once the switch is on", () => {
    const model = makeModel(DETAIL);

    const body = renderBody(DETAIL, model);

    expect(model.isApproveAllowed()).toBe(true);
    expect(byId(body, "remote-execution-setting-notice")).toBeUndefined();
  });
});
