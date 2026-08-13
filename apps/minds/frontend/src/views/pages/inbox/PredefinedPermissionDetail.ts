// Predefined (catalog-backed) permission dialog: the account dropdown, a
// plain-English summary of what approving grants, and -- behind "Adjust" --
// the full editor with one switch per offered permission, grouped by area
// (port of LatchkeyPredefinedPermission.jinja).
//
// Rows render the labels the server derived; a detent schema name is only ever
// a checkbox value, never visible text.

import m from "mithril";
import type {
  UiPermissionGrantGroup,
  UiPermissionGrantRow,
} from "../../../generated/ui";
import type {
  InboxModel,
  PermissionAccountChoice,
  PredefinedPermissionDetail as Detail,
} from "../../../models/inbox";
import {
  isPermissionCheckboxDisabled,
  submittedPermissions,
} from "../../../models/inbox";
import { Select } from "../../components/FormControls";
import { Icon16 } from "../../components/Icon";
import { serviceMark } from "../../components/ServiceMark";
import { PermissionsShell } from "./PermissionsShell";

export interface PredefinedPermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

/** A bare text link. Not a Button: the mockup's Adjust and Back sit in the
 * text column at helper size, which every Button size overrides. */
const TEXT_LINK_CLASS = "type-helper text-accent hover:underline cursor-pointer";

/** The rows the summary lists: exactly what Approve would submit right now,
 * so a wildcard selection never reads as "and these specific ones too". */
function summaryRows(
  detail: Detail,
  checked: ReadonlySet<string>,
): UiPermissionGrantRow[] {
  const submitted = new Set(
    submittedPermissions(checked, detail.wildcard_permission),
  );
  return detail.permission_groups.flatMap((group) =>
    group.rows.filter((row) => submitted.has(row.permission)),
  );
}

/** The accounts that are a real answer to "which account?", as opposed to the
 * sentinel that starts a new sign-in (the server always appends that one, so
 * the raw choice list is never empty and its length says nothing). */
function realAccountChoices(detail: Detail): PermissionAccountChoice[] {
  return detail.account_choices.filter(
    (choice) => choice.value !== detail.new_account_value,
  );
}

/** The dialog has two shapes, not three.
 *
 * Nothing signed in yet: no picker at all, and Approve says "Sign in & approve"
 * so the browser hop is not a surprise. Anything signed in: the picker, with
 * the account the grant will ride on already selected and "+ Add account"
 * last -- including when there is only one, where naming it is what stops a
 * service the user holds several accounts on from reading as ambiguous.
 *
 * A single account used to ride silently, and a middle state that named it in
 * the header replaced that; both are gone, because one control that always
 * says which account beats three arrangements the user has to tell apart. */
function hasNoAccount(detail: Detail): boolean {
  return realAccountChoices(detail).length === 0;
}

/** Whether Approve will run a browser sign-in before it grants anything.
 *
 * Two ways in: nothing is signed in yet, or the picker is sitting on
 * "+ Add account", which is a staged choice rather than an account -- picking it
 * signs nothing in until Approve is pressed. Both make the next click open a
 * browser, so both say so on the button. Reading the model and not just the
 * payload is the point: the second one changes as the user works the dropdown.
 *
 * Neither counts for a service latchkey cannot sign in to at all (AWS, Coolify),
 * which connects by the credentials the dialog asks for -- `manual_credentials`
 * is non-null exactly then. Saying "Sign in" there would contradict the form
 * right below the button and the "(asks you for credentials)" hint the server
 * deliberately words the other way. `will_open_browser` cannot stand in: it is
 * computed for the account the payload was built with, so it does not move as
 * the user works the dropdown. */
function willSignIn(model: InboxModel, detail: Detail): boolean {
  if (detail.manual_credentials !== null) return false;
  return hasNoAccount(detail) || model.selectedAccount === detail.new_account_value;
}

/** The account the grant rides on -- shown whenever there is one to name.
 * Gated on the payload, never on the selection: choosing "+ Add account" must
 * not take the dropdown away, or there would be no way back to a real one. */
function accountPicker(model: InboxModel, detail: Detail): m.Children {
  if (hasNoAccount(detail)) return null;
  return m("div", { id: "permissions-account", class: "flex flex-col gap-1.5" }, [
    m("h3", { class: "type-section text-tertiary" }, "Account"),
    m(
      Select,
      {
        name: "account",
        "aria-label": "Account",
        width: "w-[280px] max-w-full",
        value: model.selectedAccount,
        onchange: (event: Event) => {
          model.selectedAccount = (event.target as HTMLSelectElement).value;
        },
      },
      // Server order, so the new-account sentinel is naturally last. An
      // <option> carries one text style, so a hint rides in parentheses --
      // one of them already contains an em dash of its own.
      detail.account_choices.map((choice) =>
        m(
          "option",
          { value: choice.value },
          choice.hint ? `${choice.label} (${choice.hint})` : choice.label,
        ),
      ),
    ),
  ]);
}

function summaryView(model: InboxModel, detail: Detail): m.Children {
  const rows = summaryRows(detail, model.checkedPermissions);
  const adjustLink = m(
    "button",
    {
      type: "button",
      id: "permissions-adjust-link",
      // Indented to the text column: 14px check glyph + the 8px gap.
      class: "mt-2 ml-6 " + TEXT_LINK_CLASS,
      onclick: () => model.showPermissionEditor(),
    },
    "Adjust",
  );
  return m("div", { id: "permissions-simple-view" }, [
    m(
      "h3",
      { class: "type-section text-tertiary" },
      "Approving will let the agent",
    ),
    rows.length === 0
      ? m("p", { id: "permissions-empty-summary", class: "mt-2 type-body text-secondary" }, [
          "Nothing yet — use ",
          m("span", { class: "font-semibold" }, "Adjust"),
          " to choose what to grant.",
        ])
      : m(
          "ul",
          { class: "mt-2 flex flex-col gap-2" },
          rows.map((row) =>
            m("li", { class: "flex items-start gap-2" }, [
              m(Icon16, {
                name: "check",
                size: "sm",
                extra: "shrink-0 text-success mt-0.5",
              }),
              m("span", { class: "min-w-0" }, [
                m(
                  "span",
                  { class: "block type-body text-primary font-semibold" },
                  row.label,
                ),
                row.description
                  ? m(
                      "span",
                      { class: "block type-helper text-secondary" },
                      row.description,
                    )
                  : null,
              ]),
            ]),
          ),
        ),
    adjustLink,
  ]);
}

function editorRow(
  model: InboxModel,
  detail: Detail,
  row: UiPermissionGrantRow,
): m.Children {
  const checked = model.checkedPermissions;
  const isDisabled = isPermissionCheckboxDisabled(
    row.permission,
    detail.wildcard_permission,
    checked,
  );
  return m(
    "label",
    {
      class: `perm-row flex items-center justify-between gap-4 py-1.5 ${isDisabled ? "opacity-50" : "cursor-pointer"}`,
    },
    [
      m("span", { class: "min-w-0" }, [
        m(
          "span",
          {
            class: row.is_wildcard
              ? "type-body text-warning"
              : "type-body text-primary",
          },
          row.label,
        ),
        row.description
          ? m(
              "span",
              { class: "block type-helper text-secondary mt-0.5" },
              row.description,
            )
          : null,
      ]),
      m("input", {
        type: "checkbox",
        name: "permissions",
        value: row.permission,
        class: "perm-switch-input shrink-0",
        checked: checked.has(row.permission),
        disabled: isDisabled,
        onchange: (event: Event) => {
          const target = event.target as HTMLInputElement;
          if (target.checked) checked.add(row.permission);
          else checked.delete(row.permission);
        },
      }),
    ],
  );
}

// The catch-all group sits behind a divider so it never reads as one more
// area of the service.
function editorGroup(
  model: InboxModel,
  detail: Detail,
  group: UiPermissionGrantGroup,
): m.Children {
  return m(
    "div",
    {
      class: group.is_extras
        ? "flex flex-col gap-1 border-t border-subtle pt-3"
        : "flex flex-col gap-1",
    },
    [
      m("p", { class: "type-section text-tertiary" }, group.heading),
      m(
        "div",
        { class: "flex flex-col pr-2" },
        group.rows.map((row) => editorRow(model, detail, row)),
      ),
    ],
  );
}

function editorView(model: InboxModel, detail: Detail): m.Children {
  return m("div", { id: "permissions-editor-view", class: "flex flex-col gap-4" }, [
    m(
      "button",
      {
        type: "button",
        id: "permissions-adjust-back-link",
        class: "self-start inline-flex items-center gap-1 " + TEXT_LINK_CLASS,
        onclick: () => model.hidePermissionEditor(),
      },
      [
        m(Icon16, { name: "chevron-left", size: "sm", extra: "shrink-0" }),
        "Back to agent's picks",
      ],
    ),
    ...detail.permission_groups.map((group) => editorGroup(model, detail, group)),
  ]);
}

export function PredefinedPermissionDetailView(): m.Component<PredefinedPermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      return m(PermissionsShell, {
        model,
        headerLabel: detail.display_name,
        mark: serviceMark(
          detail.service_name,
          "w-[18px] h-[18px]",
          "brand",
          m(Icon16, { name: "key", extra: "text-primary" }),
        ),
        rationale: detail.rationale,
        account: accountPicker(model, detail),
        approveLabel: willSignIn(model, detail) ? "Sign in & approve" : "Approve",
        progressLabel: detail.will_open_browser
          ? `Opening a browser window for you to sign in to ${detail.display_name}…`
          : "Granting permission...",
        body: model.isPermissionEditorShown
          ? editorView(model, detail)
          : summaryView(model, detail),
      });
    },
  };
}
