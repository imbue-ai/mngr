import m from "mithril";
import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import type { PredefinedPermissionDetail } from "../../../models/inbox";
import { Select } from "../../components/FormControls";
import { PredefinedPermissionDetailView } from "./PredefinedPermissionDetail";
import type { AnyVnode } from "../../../testing";
import { classesOf, collectText, collectVnodes } from "../../../testing";

const DETAIL: PredefinedPermissionDetail = {
  kind: "predefined",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "post the standup summary",
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
          description: "Read every Slack resource",
          is_wildcard: false,
        },
      ],
    },
    {
      heading: "Chat",
      is_extras: false,
      rows: [
        {
          permission: "slack-chat-read",
          label: "Read chat",
          description: "",
          is_wildcard: false,
        },
        {
          permission: "slack-chat-write",
          label: "Manage chat",
          description: "Post messages",
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
  checked_permissions: ["slack-chat-write"],
  account_choices: [
    { value: "alice@x", label: "alice@x", hint: "", is_credential_setup_needed: false, is_account_name_needed: false },
    { value: "bob@x", label: "bob@x", hint: "needs sign-in", is_credential_setup_needed: true, is_account_name_needed: false },
    {
      value: ":new-account",
      label: "+ Add account",
      hint: "opens a browser sign-in",
      is_credential_setup_needed: true,
      is_account_name_needed: false,
    },
  ],
  selected_account_value: "alice@x",
  new_account_value: ":new-account",
  wildcard_permission: "any",
  will_open_browser: false,
  manual_credentials: null,
};

// Every schema name the dialog may submit. None of them may appear as text.
const SCHEMA_NAMES = DETAIL.permission_groups.flatMap((group) =>
  group.rows.map((row) => row.permission),
);

// Render the view without a DOM: instantiate the closure component and call
// view() directly, the same idiom as views/components/components.test.ts. The
// root is a PermissionsShell vnode, so each region of the dialog is one of its
// attrs (`body`, `account`, `mark`, `headerLabel`).
interface ShellAttrs {
  headerLabel: string;
  mark: m.Children;
  account: m.Children;
  approveLabel?: string;
  body: m.Children;
}

function renderShell(
  model: InboxModel,
  detail: PredefinedPermissionDetail,
): ShellAttrs {
  const instance = PredefinedPermissionDetailView() as unknown as m.Component;
  const vnode = m(instance, {
    model,
    detail,
  } as unknown as m.Attributes) as m.Vnode;
  const root = (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  );
  return root.attrs as unknown as ShellAttrs;
}

function renderBody(
  model: InboxModel,
  detail: PredefinedPermissionDetail,
): m.Children {
  return renderShell(model, detail).body;
}

function findById(node: unknown, id: string): AnyVnode | undefined {
  return collectVnodes(node).find((vnode) => (vnode.attrs ?? {}).id === id);
}

function switchInputs(node: unknown): AnyVnode[] {
  return collectVnodes(node).filter((vnode) =>
    classesOf(vnode).includes("perm-switch-input"),
  );
}

function makeModel(detail: PredefinedPermissionDetail = DETAIL): InboxModel {
  const model = new InboxModel();
  model.detail = detail;
  model.selectedId = detail.request_id;
  model.checkedPermissions = new Set(detail.checked_permissions);
  model.selectedAccount = detail.selected_account_value;
  return model;
}

/** Run one closure component's view, so its own markup can be asserted on. */
function renderComponent(vnode: m.Vnode): m.Children {
  const factory = vnode.tag as unknown as () => m.Component;
  const instance = factory();
  const mounted = m(instance, vnode.attrs as m.Attributes, vnode.children) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Children).call(instance, mounted);
}

function optionsOf(node: unknown): { value: unknown; label: string }[] {
  return collectVnodes(node)
    .filter((vnode) => vnode.tag === "option")
    .map((vnode) => ({
      value: (vnode.attrs ?? {}).value,
      label: collectText(vnode).join(""),
    }));
}

describe("PredefinedPermissionDetailView summary", () => {
  it("leads with the plain-English summary of what approving grants", () => {
    const text = collectText(renderBody(makeModel(), DETAIL));

    expect(text).toContain("Approving will let the agent");
    expect(text).toContain("Manage chat");
    expect(text).toContain("Post messages");
    // Permissions the agent did not ask for stay out of the summary.
    expect(text).not.toContain("Read everything");
    expect(text).toContain("Adjust");
  });

  it("keeps the editor -- headings, switches and all -- hidden until Adjust", () => {
    const body = renderBody(makeModel(), DETAIL);

    expect(findById(body, "permissions-simple-view")).toBeDefined();
    expect(findById(body, "permissions-editor-view")).toBeUndefined();
    expect(switchInputs(body)).toHaveLength(0);
    expect(collectText(body)).not.toContain("Extras");
  });

  it("swaps the summary for the grouped editor when Adjust is clicked", () => {
    const model = makeModel();
    const adjust = findById(
      renderBody(model, DETAIL),
      "permissions-adjust-link",
    );
    expect(adjust).toBeDefined();

    ((adjust?.attrs ?? {}).onclick as () => void)();

    const body = renderBody(model, DETAIL);
    expect(findById(body, "permissions-editor-view")).toBeDefined();
    expect(findById(body, "permissions-simple-view")).toBeUndefined();
    // Every offered permission is now a switch, under its group heading.
    expect(switchInputs(body)).toHaveLength(SCHEMA_NAMES.length);
    const text = collectText(body);
    expect(text).toContain("Full access");
    expect(text).toContain("Chat");
    expect(text).toContain("Extras");
  });

  it("points at Adjust, quietly, when the agent asked for nothing specific", () => {
    const model = makeModel();
    model.checkedPermissions = new Set();

    const body = renderBody(model, DETAIL);

    expect(collectText(body).join(" ")).toContain("Nothing yet — use");
    expect(findById(body, "permissions-adjust-link")).toBeDefined();
    // The heading stays: the empty line is the answer to it, not a warning.
    expect(collectText(body)).toContain("Approving will let the agent");
  });

  it("returns to the agent's picks from the editor, discarding what was changed there", () => {
    const model = makeModel();
    model.showPermissionEditor();
    model.checkedPermissions.add("slack-read-all");
    const back = findById(renderBody(model, DETAIL), "permissions-adjust-back-link");
    expect(back).toBeDefined();

    ((back?.attrs ?? {}).onclick as () => void)();

    const body = renderBody(model, DETAIL);
    expect(findById(body, "permissions-simple-view")).toBeDefined();
    expect([...model.checkedPermissions]).toEqual(["slack-chat-write"]);
  });

  it("summarizes only the wildcard once it is checked alongside a specific permission", () => {
    const model = makeModel();
    model.checkedPermissions = new Set(["slack-chat-write", "any"]);

    const text = collectText(renderBody(model, DETAIL));

    expect(text).toContain("Everything (unrestricted)");
    expect(text).not.toContain("Manage chat");
  });
});

describe("PredefinedPermissionDetailView account states", () => {
  const NEW_ACCOUNT = DETAIL.account_choices[2];
  const ALICE = DETAIL.account_choices[0];

  function withChoices(choices: PredefinedPermissionDetail["account_choices"]): PredefinedPermissionDetail {
    return { ...DETAIL, account_choices: choices };
  }

  // Two states, not three: nothing signed in, or something signed in. A single
  // account is NOT a third arrangement -- it gets the same picker as several.
  it("shows the picker for one account, naming it rather than riding silently", () => {
    const detail = withChoices([ALICE, NEW_ACCOUNT]);
    const account = renderShell(makeModel(detail), detail).account;

    expect(collectVnodes(account).find((vnode) => vnode.tag === Select)).toBeDefined();
    expect(optionsOf(account)).toEqual([
      { value: "alice@x", label: "alice@x" },
      { value: ":new-account", label: "+ Add account (opens a browser sign-in)" },
    ]);
  });

  it("offers no picker before the first sign-in, and says Approve will sign in", () => {
    const detail = withChoices([NEW_ACCOUNT]);
    const shell = renderShell(makeModel(detail), detail);

    expect(shell.account, "nothing signed in means nothing to pick between").toBeNull();
    expect(shell.approveLabel).toBe("Sign in & approve");
  });

  it("says plain Approve once an account is signed in", () => {
    expect(renderShell(makeModel(), DETAIL).approveLabel).toBe("Approve");
  });

  it("says Approve will sign in once the picker is on '+ Add account'", () => {
    // Staged, not signed in: the browser hop runs on Approve, so the button has
    // to say so even though the service already has accounts.
    const model = makeModel();
    model.selectedAccount = DETAIL.new_account_value;

    const shell = renderShell(model, DETAIL);
    expect(shell.approveLabel).toBe("Sign in & approve");
    expect(shell.account, "and the dropdown stays, so the choice is reversible").not.toBeNull();
  });

  it("never promises a sign-in for a service that has none", () => {
    // AWS and the like connect by the credentials this dialog asks for, which
    // is exactly when the server sends manual_credentials. Saying "Sign in"
    // would contradict the form under the button and the option's own
    // "(asks you for credentials)" hint.
    const credentialsOnly: PredefinedPermissionDetail = {
      ...DETAIL,
      display_name: "AWS",
      service_name: "aws",
      manual_credentials: { parameters: [{ name: "access-key-id", label: "Access key id" }], message: "" },
      account_choices: [
        ALICE,
        { ...NEW_ACCOUNT, hint: "asks you for credentials", is_account_name_needed: true },
      ],
    };

    expect(renderShell(makeModel(credentialsOnly), credentialsOnly).approveLabel).toBe("Approve");

    const staged = makeModel(credentialsOnly);
    staged.selectedAccount = credentialsOnly.new_account_value;
    expect(renderShell(staged, credentialsOnly).approveLabel).toBe("Approve");

    const firstConnection = { ...credentialsOnly, account_choices: [credentialsOnly.account_choices[1]] };
    expect(renderShell(makeModel(firstConnection), firstConnection).approveLabel).toBe("Approve");
  });
});

describe("PredefinedPermissionDetailView account picker", () => {
  it("is a dropdown listing every account, with '+ Add account' last", () => {
    const account = renderShell(makeModel(), DETAIL).account;

    // A dropdown, not radios: a radio per account is what this replaced.
    const picker = collectVnodes(account).find((vnode) => vnode.tag === Select);
    expect(picker, "the account control is the shared Select primitive").toBeDefined();
    expect(collectVnodes(account).filter((vnode) => (vnode.attrs ?? {}).type === "radio")).toEqual([]);
    // ...and that primitive really does render a <select> around the options.
    const rendered = renderComponent(picker as m.Vnode);
    expect(collectVnodes(rendered).some((vnode) => vnode.tag === "select")).toBe(true);

    expect(collectText(account)).toContain("Account");
    expect(optionsOf(account)).toEqual([
      { value: "alice@x", label: "alice@x" },
      { value: "bob@x", label: "bob@x (needs sign-in)" },
      { value: ":new-account", label: "+ Add account (opens a browser sign-in)" },
    ]);
  });

  it("writes the pick back to the model", () => {
    const model = makeModel();
    const select = collectVnodes(renderShell(model, DETAIL).account).find(
      (vnode) => vnode.tag === Select,
    );

    ((select?.attrs ?? {}).onchange as (event: Event) => void)({
      target: { value: "bob@x" },
    } as unknown as Event);

    expect(model.selectedAccount).toBe("bob@x");
  });

  it("stays out of the way only before the first sign-in", () => {
    // The server always appends the new-account sentinel, so "nothing signed
    // in" arrives as one choice that is not an account at all. That is the one
    // case with nothing to name; a lone real sign-in still gets the picker.
    const firstConnection: PredefinedPermissionDetail = {
      ...DETAIL,
      account_choices: [DETAIL.account_choices[2]],
      selected_account_value: ":new-account",
    };
    expect(renderShell(makeModel(firstConnection), firstConnection).account).toBeNull();
  });
});

describe("PredefinedPermissionDetailView editor", () => {
  function renderEditor(model: InboxModel): m.Children {
    model.showPermissionEditor();
    return renderBody(model, DETAIL);
  }

  it("never renders a detent schema name as visible text, in either view", () => {
    const model = makeModel();
    const summaryText = collectText(renderBody(model, DETAIL)).join(" ");
    const editorText = collectText(renderEditor(model)).join(" ");

    for (const schema of SCHEMA_NAMES) {
      expect(summaryText).not.toContain(schema);
      expect(editorText).not.toContain(schema);
    }
    // The wildcard reads as its label, and its schema name survives only as
    // the checkbox value the grant submits.
    expect(editorText).toContain("Everything (unrestricted)");
    expect(
      switchInputs(renderEditor(model)).map(
        (input) => (input.attrs ?? {}).value,
      ),
    ).toContain("any");
  });

  it("puts the wildcard row in the warning tone behind a divider", () => {
    const body = renderEditor(makeModel());
    const rows = collectVnodes(body).filter((vnode) =>
      classesOf(vnode).includes("perm-row"),
    );

    expect(rows).toHaveLength(SCHEMA_NAMES.length);
    const wildcardLabel = collectVnodes(body).find((vnode) =>
      classesOf(vnode).includes("text-warning"),
    );
    expect(collectText(wildcardLabel)).toEqual(["Everything (unrestricted)"]);
    const extrasGroup = collectVnodes(body).find((vnode) =>
      classesOf(vnode).includes("border-t"),
    );
    expect(collectText(extrasGroup)).toContain("Extras");
  });

  it("flips a permission through the model and disables the specific rows under the wildcard", () => {
    const model = makeModel();
    const body = renderEditor(model);
    const readAll = switchInputs(body).find(
      (input) => (input.attrs ?? {}).value === "slack-read-all",
    );

    ((readAll?.attrs ?? {}).onchange as (event: Event) => void)({
      target: { checked: true },
    } as unknown as Event);
    expect(model.checkedPermissions.has("slack-read-all")).toBe(true);

    model.checkedPermissions.add("any");
    const disabled = switchInputs(renderEditor(model)).filter(
      (input) => (input.attrs ?? {}).disabled === true,
    );
    expect(disabled.map((input) => (input.attrs ?? {}).value)).toEqual([
      "slack-read-all",
      "slack-chat-read",
      "slack-chat-write",
    ]);
  });
});
