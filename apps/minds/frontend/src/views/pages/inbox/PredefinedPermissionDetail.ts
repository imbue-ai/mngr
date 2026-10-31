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
  ManualCredentialParameter,
  PredefinedPermissionDetail as Detail,
} from "../../../models/inbox";
import {
  hasNoStoredAccount,
  isPermissionRowDisabled,
  submittedPermissions,
} from "../../../models/inbox";
import {
  DESKTOP_EGRESS_LABEL,
  renderDesktopEgressHops,
} from "../../components/DesktopEgressRouteSetting";
import { Select } from "../../components/FormControls";
import { Icon16 } from "../../components/Icon";
import { renderPermissionSwitch } from "../../components/PermissionSwitch";
import { serviceMark } from "../../components/ServiceMark";
import { renderSwitch } from "../../components/Switch";
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

/** The account the grant rides on -- shown whenever there is one to name.
 * Gated on the payload, never on the selection: choosing "Add account" must
 * not take the dropdown away, or there would be no way back to a real one.
 *
 * The dialog has two shapes, not three. Nothing signed in yet: no picker at
 * all, and Approve says "Sign in & approve" so the browser hop is not a
 * surprise. Anything signed in: the picker, with the account the grant will
 * ride on already selected and "Add account" last -- including when there
 * is only one, where naming it is what stops a service the user holds several
 * accounts on from reading as ambiguous. One control that always says which
 * account beats several arrangements the user has to tell apart. */
function accountPicker(model: InboxModel, detail: Detail): m.Children {
  if (hasNoStoredAccount(detail)) return null;
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
      // <option> carries one text style, so a hint rides in parentheses.
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
  const isDisabled = isPermissionRowDisabled(
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
      renderSwitch({
        isOn: checked.has(row.permission),
        label: row.label,
        isDisabled,
        data: { "data-grant-permission": row.permission },
        onFlip: (isOn) => {
          if (isOn) checked.add(row.permission);
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

/** How the copy names what the credential form asks for: its inputs, as the
 * user would call them ("a token", "the access key id and secret access key").
 * Never "credentials", which reads as a username and password the dialog does
 * not ask for. */
function describeCredentialInputs(parameters: readonly ManualCredentialParameter[]): {
  nounPhrase: string;
  pronoun: string;
  verb: string;
} {
  const names = parameters.map((parameter) => parameter.label.charAt(0).toLowerCase() + parameter.label.slice(1));
  if (names.length === 1) {
    const article = "aeiou".includes(names[0].charAt(0)) ? "an" : "a";
    return { nounPhrase: `${article} ${names[0]}`, pronoun: "it", verb: "Paste" };
  }
  const joined = `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return { nounPhrase: `the ${joined}`, pronoun: "them", verb: "Enter" };
}

/** The offer to type instead of signing in, for a service that takes both. A
 * footnote under the buttons: it is about what Approve would do, so it sits
 * with Approve. Only while the sign-in is what Approve does -- once the form is
 * up, the way back sits on the form -- and never while an approval runs. */
function credentialEntryOffer(model: InboxModel, detail: Detail): m.Children {
  if (!model.isManualCredentialEntryOffered() || model.manualCredentialsPrompt() !== null) return null;
  if (model.isApproveBusy || detail.manual_credentials === null) return null;
  const inputs = describeCredentialInputs(detail.manual_credentials.parameters);
  return m("p", { id: "permissions-credential-entry-offer", class: "type-helper text-secondary" }, [
    `Already have ${inputs.nounPhrase}? `,
    m(
      "button",
      {
        type: "button",
        id: "permissions-credential-entry-offer-link",
        class: TEXT_LINK_CLASS,
        onclick: () => model.chooseManualCredentialEntry(),
      },
      `${inputs.verb} ${inputs.pronoun} instead of signing in`,
    ),
  ]);
}

/** The way back to the sign-in once the form is up. Sits right under the form,
 * since the form is what the user is leaving behind. */
function credentialEntrySwitch(model: InboxModel): m.Children {
  if (!model.isManualCredentialEntryOffered() || model.manualCredentialsPrompt() === null) return null;
  return m(
    "p",
    { id: "permissions-credential-entry-switch", class: "type-helper text-secondary" },
    m(
      "button",
      {
        type: "button",
        id: "permissions-credential-entry-switch-link",
        class: TEXT_LINK_CLASS,
        onclick: () => model.chooseBrowserSignIn(),
      },
      "Sign in through the browser instead",
    ),
  );
}

const PROXY_SUMMARY_ID = "permissions-proxy-summary";

/** The agent's ask to also send the service's requests through this computer.
 * The switch rides on Approve: denying, or closing the dialog, leaves the
 * route as it is now. */
function proxySection(model: InboxModel, detail: Detail): m.Children {
  const requested = detail.proxy;
  if (requested === null) return null;
  return m("div", { id: "permissions-proxy", class: "flex flex-col gap-1.5" }, [
    m("h3", { class: "type-section text-tertiary" }, "Where requests leave from"),
    m(
      "p",
      { class: "type-body text-secondary" },
      `The agent asked for this workspace's requests to ${detail.display_name} to leave through this computer.`,
    ),
    m("div", { class: "flex items-center justify-between gap-4" }, [
      m("div", { class: "min-w-0" }, [
        m("p", { class: "type-body text-primary truncate" }, DESKTOP_EGRESS_LABEL),
        m(
          "p",
          { id: PROXY_SUMMARY_ID, class: "type-helper text-tertiary mt-0.5" },
          model.isProxyOn
            ? [
                "Requests will leave from ",
                renderDesktopEgressHops(requested.resulting_route, requested.desktops, true),
                ".",
              ]
            : "Requests will keep leaving from where they do now.",
        ),
      ]),
      renderPermissionSwitch({
        isOn: model.isProxyOn,
        isBusy: false,
        isDisabled: false,
        title: null,
        label: DESKTOP_EGRESS_LABEL,
        describedBy: PROXY_SUMMARY_ID,
        permission: "proxy",
        onFlip: (isOn) => {
          model.isProxyOn = isOn;
        },
      }),
    ]),
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
        credentialEntrySwitch: credentialEntrySwitch(model),
        // Says what approving will do, so the browser hop is not a surprise
        // (and for AWS and the like never contradicts the credential form
        // right below the button).
        approveLabel: model.isBrowserSignInPending() ? "Sign in & approve" : "Approve",
        // Read off the live selection, like the label, so credentials typed
        // in instead of a sign-in never get a browser promised.
        progressLabel: model.isBrowserSignInPending()
          ? `Opening a browser window for you to sign in to ${detail.display_name}…`
          : "Granting permission...",
        body: [
          model.isPermissionEditorShown ? editorView(model, detail) : summaryView(model, detail),
          proxySection(model, detail),
        ],
        footer: credentialEntryOffer(model, detail),
      });
    },
  };
}
