import m from "mithril";
import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import type { ManualCredentialsPrompt, PredefinedPermissionDetail } from "../../../models/inbox";
import { Notice } from "../../components/Notice";
import { PermissionsShell } from "./PermissionsShell";

const AWS_PROMPT: ManualCredentialsPrompt = {
  parameters: [
    { name: "access-key-id", label: "Access key id" },
    { name: "secret-access-key", label: "Secret access key" },
  ],
  message: "AWS does not support browser sign-in",
};

const MANUAL_DETAIL: PredefinedPermissionDetail = {
  kind: "predefined",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "list the buckets",
  scope: "aws-api",
  display_name: "AWS",
  permission_schemas: ["any", "aws-read-all"],
  description_by_permission_name: {},
  checked_permissions: ["aws-read-all"],
  account_choices: [
    {
      value: ":new-account",
      label: "Connect",
      hint: "asks you for credentials",
      is_credential_setup_needed: true,
      is_account_name_needed: false,
    },
  ],
  selected_account_value: ":new-account",
  new_account_value: ":new-account",
  wildcard_permission: "any",
  wildcard_label: "all",
  will_open_browser: false,
  manual_credentials: AWS_PROMPT,
};

interface ElementVnode {
  tag: unknown;
  attrs: Record<string, unknown> | null;
  children?: unknown;
}

/** Flatten a rendered vnode tree into the element vnodes it contains. */
function flatten(node: unknown): ElementVnode[] {
  if (node === null || node === undefined || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(flatten);
  const vnode = node as ElementVnode;
  const children = "children" in vnode ? flatten(vnode.children) : [];
  return [vnode, ...children];
}

/** Build a model whose selected detail is `detail` (as `select()` would leave it). */
function modelWithDetail(detail: PredefinedPermissionDetail): InboxModel {
  const model = new InboxModel();
  model.detail = detail;
  model.selectedAccount = detail.selected_account_value;
  model.checkedPermissions = new Set(detail.checked_permissions);
  return model;
}

function renderShell(model: InboxModel): ElementVnode[] {
  const instance = PermissionsShell() as unknown as m.Component;
  const attrs = {
    model,
    headerLabel: "AWS permissions",
    wsName: "alpha",
    rationale: "list the buckets",
    progressLabel: "Granting permission...",
    body: m("div", { id: "permissions-body" }),
  };
  const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
  const root = (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(instance, vnode);
  return flatten(root);
}

function inputNames(nodes: ElementVnode[]): unknown[] {
  return nodes.filter((node) => node.tag === "input").map((node) => node.attrs?.name);
}

function hasApproveButton(nodes: ElementVnode[]): boolean {
  return nodes.some((node) => node.attrs?.id === "permissions-approve-btn");
}

function indexOfId(nodes: ElementVnode[], id: string): number {
  return nodes.findIndex((node) => node.attrs?.id === id);
}

function findById(nodes: ElementVnode[], id: string): ElementVnode | undefined {
  return nodes.find((node) => node.attrs?.id === id);
}

/** Whether a node is the error-variant Notice (the design system's red box). */
function isErrorNotice(node: ElementVnode | undefined): boolean {
  return node?.tag === Notice && node?.attrs?.variant === "error";
}

/** Whether anything in the credential form asks to be scrolled into view. */
function hasScrollHooks(nodes: ElementVnode[]): boolean {
  const form = findById(nodes, "permissions-manual-credentials");
  return flatten(form).some((node) => typeof node.attrs?.oncreate === "function");
}

describe("PermissionsShell manual credentials", () => {
  it("renders no credential form for a service that signs in through a browser", () => {
    const nodes = renderShell(modelWithDetail({ ...MANUAL_DETAIL, manual_credentials: null }));

    expect(indexOfId(nodes, "permissions-manual-credentials")).toBe(-1);
    expect(hasApproveButton(nodes)).toBe(true);
  });

  it("renders one labeled input per credential parameter, above the permissions body", () => {
    const nodes = renderShell(modelWithDetail(MANUAL_DETAIL));

    expect(inputNames(nodes)).toEqual(["credential-access-key-id", "credential-secret-access-key"]);
    const labels = nodes.filter((node) => node.tag === "span").flatMap((node) => flatten(node.children));
    expect(JSON.stringify(labels)).toContain("Access key id");
    // The form is the first thing the user sees under the rationale...
    const formIndex = indexOfId(nodes, "permissions-manual-credentials");
    expect(formIndex).toBeGreaterThan(-1);
    expect(formIndex).toBeLessThan(indexOfId(nodes, "permissions-body"));
    // ...and the latchkey command behind it is never shown.
    expect(JSON.stringify(nodes)).not.toContain("latchkey");
    expect(hasApproveButton(nodes)).toBe(true);
  });

  it("also asks for an account name when the selected account needs one", () => {
    const detail: PredefinedPermissionDetail = {
      ...MANUAL_DETAIL,
      account_choices: [{ ...MANUAL_DETAIL.account_choices[0], is_account_name_needed: true }],
    };

    expect(inputNames(renderShell(modelWithDetail(detail)))).toContain("account_name");
  });

  it("shows an error with no Approve button when there is nothing to ask for", () => {
    const nodes = renderShell(
      modelWithDetail({
        ...MANUAL_DETAIL,
        manual_credentials: { parameters: [], message: "Minds cannot work out which credentials to ask for" },
      }),
    );

    expect(inputNames(nodes)).toEqual([]);
    expect(hasApproveButton(nodes)).toBe(false);
    expect(isErrorNotice(findById(nodes, "permissions-manual-credentials-message"))).toBe(true);
  });

  it("states the opening instruction as plain text, not as an error", () => {
    const nodes = renderShell(modelWithDetail(MANUAL_DETAIL));

    const message = findById(nodes, "permissions-manual-credentials-message");
    expect(isErrorNotice(message)).toBe(false);
    expect(String(message?.attrs?.className)).toContain("text-secondary");
  });

  it("turns the form's message into an error notice once an attempt has failed", () => {
    const model = modelWithDetail(MANUAL_DETAIL);
    model.manualCredentialsFeedback = { ...AWS_PROMPT, message: "AWS did not accept those credentials." };

    const nodes = renderShell(model);

    const message = findById(nodes, "permissions-manual-credentials-message");
    expect(isErrorNotice(message)).toBe(true);
    // The inputs stay, so the rejected values can be corrected in place.
    expect(inputNames(nodes)).toEqual(["credential-access-key-id", "credential-secret-access-key"]);
    expect(hasApproveButton(nodes)).toBe(true);
  });

  it("scrolls a failure into view, and only a failure", () => {
    // The opening instruction is where the user already is: nothing to scroll.
    expect(hasScrollHooks(renderShell(modelWithDetail(MANUAL_DETAIL)))).toBe(false);

    const rejectedModel = modelWithDetail(MANUAL_DETAIL);
    rejectedModel.manualCredentialsFeedback = { ...AWS_PROMPT, message: "AWS did not accept those credentials." };
    expect(hasScrollHooks(renderShell(rejectedModel))).toBe(true);

    // An outright failure has its own notice, below the buttons.
    const failedModel = modelWithDetail({ ...MANUAL_DETAIL, manual_credentials: null });
    failedModel.errorMessage = "Sign-in did not complete";
    const nodes = renderShell(failedModel);
    const errorBox = findById(nodes, "permissions-error");
    expect(typeof errorBox?.attrs?.oncreate).toBe("function");
    expect(isErrorNotice(flatten(errorBox?.children)[0])).toBe(true);
  });
});
