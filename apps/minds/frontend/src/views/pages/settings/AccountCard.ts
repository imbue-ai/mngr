// One signed-in account's card on the Accounts page: identity row, actions,
// and the asynchronously-loaded plan/usage section. Port of the account loop
// in templates/pages/Accounts.jinja + AccountPlanSection.jinja + accounts.js.

import m from "mithril";
import type { UiAccountEntry } from "../../../channel/messages";
import type { AccountsDetailModel } from "../../../models/accountsDetail";
import { Button } from "../../components/Button";
import { webLogin } from "../../../models/webLogin";
import { Card } from "../../components/Card";
import { Link } from "../../components/Link";
import { Notice } from "../../components/Notice";
import { routeLinkAttrs } from "../../components/route-link";
import { Select } from "../../components/FormControls";
import { Spinner } from "../../components/Spinner";
import { StatusBadge } from "../../components/StatusBadge";

interface AccountCardAttrs {
  model: AccountsDetailModel;
  account: UiAccountEntry;
}

// Shared plan copy (mirrors the hosted signup page's plan selector -- keep in
// sync with SIGNUP_PLANS in apps/remote_service_connector/frontend/src/plans.ts).
const PLAN_DESCRIPTION_BY_NAME: Record<string, string> = {
  explorer:
    "2 free cloud workspaces. You agree to share product data from those workspaces with Imbue " +
    "to help improve Imbue Studio.",
  free:
    "1 free cloud workspace. Your workspace may be paused when idle. Availability depends on host " +
    "capacity: if hosts run out, this plan is limited first. Our goal is to make your data private " +
    "and secure.",
};

// The stored plan name "free" is shown to people as "Limited".
const PLAN_LABEL_BY_NAME: Record<string, string> = { free: "Limited" };

function planLabel(planName: string): string {
  return PLAN_LABEL_BY_NAME[planName] ?? planName.charAt(0).toUpperCase() + planName.slice(1);
}

// Switching TO explorer is the analytics consent, so it needs an explicit
// affirmative agreement, not just a dropdown pick.
const EXPLORER_AGREEMENT_COPY =
  "I agree to the privacy policy for the Explorer edition, which includes sharing product data " +
  "from my workspace with Imbue.";

function pendingPlanDetails(
  view: { plan_name: string },
  privacyPolicyUrl: string,
  selectedPlan: string,
  isAgreementChecked: boolean,
  onAgreementChange: (isChecked: boolean) => void,
): m.Children {
  if (selectedPlan === view.plan_name) return null;
  const description = PLAN_DESCRIPTION_BY_NAME[selectedPlan];
  if (description === undefined && selectedPlan !== "explorer") return null;
  const learnMore = privacyPolicyUrl
    ? m(
        Link,
        {
          href: privacyPolicyUrl,
          target: "_blank",
          rel: "noopener",
          extra: "type-helper",
        },
        "Learn more.",
      )
    : null;
  return m("div", { class: "mb-2" }, [
    description !== undefined
      ? m("p", { class: "type-helper text-tertiary mb-1" }, [
          description,
          " ",
          learnMore,
        ])
      : null,
    selectedPlan === "explorer"
      ? m(
          "label",
          {
            class:
              "flex items-start gap-2 type-helper text-secondary cursor-pointer",
          },
          [
            m("input", {
              id: "explorer-agreement-checkbox",
              type: "checkbox",
              checked: isAgreementChecked,
              class: "mt-0.5 cursor-pointer",
              onchange: (event: Event) => {
                onAgreementChange((event.target as HTMLInputElement).checked);
              },
            }),
            m("span", EXPLORER_AGREEMENT_COPY),
          ],
        )
      : null,
  ]);
}

function planSection(
  model: AccountsDetailModel,
  account: UiAccountEntry,
  selectedPlanByUserId: Map<string, string>,
  isExplorerAgreementCheckedByUserId: Map<string, boolean>,
): m.Children {
  const plan = model.planStateFor(account.user_id);
  if (!plan.isLoaded) {
    return m(
      "div",
      { class: "flex items-center gap-2 type-helper text-tertiary" },
      [m(Spinner, { size: "sm" }), "Loading plan and usage…"],
    );
  }
  if (plan.isUnavailable || plan.planView === null) {
    return m(
      "div",
      { class: "type-helper text-tertiary" },
      "Plan and usage are unavailable right now (could not reach Imbue Cloud).",
    );
  }
  const view = plan.planView;
  const trim = plan.trimStatus;
  const isTrimRunning = trim?.is_running === true;
  const verifyPrompt = model.verifyEmailPromptFor(account.user_id);
  // The draft lives in component state (keyed by user_id): a per-render
  // local would be reset by the redraw that follows the select's onchange.
  const selectedPlan =
    selectedPlanByUserId.get(account.user_id) ?? view.plan_name;
  const isAgreementChecked =
    isExplorerAgreementCheckedByUserId.get(account.user_id) === true;
  const isAgreementNeeded =
    selectedPlan === "explorer" && selectedPlan !== view.plan_name;
  const isSwitchingPlan = model.isSwitchingPlan(account.user_id);
  return m("div", [
    verifyPrompt !== null
      ? m(Notice, { variant: "warn" }, [
          m(
            "div",
            verifyPrompt.wasAutoSent
              ? `Switching plans requires a verified email. We just sent a link to ${verifyPrompt.email} -- click it, then switch again.`
              : `Switching plans requires a verified email. Use "Resend email" to get a link at ${verifyPrompt.email}, click it, then switch again.`,
          ),
          m("div", { class: "flex items-center gap-2 mt-1" }, [
            m(
              Button,
              {
                variant: "secondary",
                disabled: verifyPrompt.isResending,
                onclick: () => void model.resendVerification(account.user_id),
              },
              verifyPrompt.isResending ? "Sending…" : "Resend email",
            ),
            verifyPrompt.wasResent
              ? m("span", { class: "type-helper" }, "Sent.")
              : null,
          ]),
        ])
      : null,
    m("div", { class: "flex items-center justify-between mb-2" }, [
      m("div", { class: "flex items-center gap-2 type-label text-secondary" }, [
        m("span", [
          "Plan: ",
          m(
            "span",
            { class: "font-semibold text-primary" },
            view.plan_display_name,
          ),
        ]),
        plan.isRefreshing
          ? m(Spinner, {
              size: "sm",
              extra: "opacity-50",
              "aria-hidden": "false",
              role: "status",
              "aria-label": "Refreshing plan and usage",
              title: "Refreshing plan and usage",
            })
          : plan.isRefreshFailed
            ? m(
                "span",
                { class: "type-helper text-tertiary" },
                "Could not refresh -- showing earlier figures",
              )
            : null,
      ]),
      view.available_plans.length > 1
        ? m("div", { class: "flex items-center gap-2" }, [
            m(
              Select,
              {
                name: "plan",
                width: "w-32",
                onchange: (event: Event) => {
                  selectedPlanByUserId.set(
                    account.user_id,
                    (event.target as HTMLSelectElement).value,
                  );
                  // A new pick invalidates a previously-checked agreement.
                  isExplorerAgreementCheckedByUserId.delete(account.user_id);
                },
              },
              view.available_plans.map((plan_name) =>
                m(
                  "option",
                  { value: plan_name, selected: plan_name === selectedPlan },
                  planLabel(plan_name),
                ),
              ),
            ),
            m(
              Button,
              {
                variant: "secondary",
                // Enabled only for an actual change: a pick equal to the
                // current plan (the resting state), a missing explorer
                // agreement, or an in-flight switch all refuse.
                disabled:
                  selectedPlan === view.plan_name ||
                  (isAgreementNeeded && !isAgreementChecked) ||
                  isSwitchingPlan,
                onclick: () =>
                  void model.switchPlan(account.user_id, selectedPlan),
              },
              isSwitchingPlan
                ? [m(Spinner, { size: "sm" }), "Switching…"]
                : "Switch plan",
            ),
          ])
        : null,
    ]),
    pendingPlanDetails(
      view,
      plan.privacyPolicyUrl,
      selectedPlan,
      isAgreementChecked,
      (isChecked) => {
        isExplorerAgreementCheckedByUserId.set(account.user_id, isChecked);
      },
    ),
    m(
      "table",
      { class: "w-full type-helper" },
      m(
        "tbody",
        view.usage_rows.map((row) =>
          m("tr", [
            m("td", { class: "py-0.5 pr-4 text-secondary" }, row.label),
            m(
              "td",
              { class: "py-0.5 pr-4 text-primary whitespace-nowrap" },
              `${row.used} of ${row.limit}`,
            ),
            m("td", { class: "py-0.5 text-tertiary" }, row.note),
          ]),
        ),
      ),
    ),
    view.is_over_storage_quota || trim !== null
      ? m("div", { class: "mt-2 flex items-center gap-3" }, [
          view.is_over_storage_quota && !isTrimRunning
            ? m(
                Button,
                {
                  variant: "secondary",
                  onclick: () => void model.trimBackups(account.user_id),
                },
                "Free up backup space",
              )
            : null,
          trim !== null
            ? m("span", { class: "type-helper text-tertiary" }, trim.detail)
            : view.is_over_storage_quota
              ? m(
                  "span",
                  { class: "type-helper text-tertiary" },
                  "Removes the oldest backups (each workspace keeps its latest) until you are back under the limit.",
                )
              : null,
        ])
      : null,
    view.is_over_storage_quota || view.is_at_bucket_quota
      ? m(
          "div",
          { class: "mt-2" },
          m(
            Link,
            {
              extra: "type-helper",
              ...routeLinkAttrs("/workspaces/destroyed"),
            },
            "Review destroyed workspace backups →",
          ),
        )
      : null,
  ]);
}

/** The account's notification email, on or off. Invitations to shared
 * workspaces ride this switch; the email an account needs, such as a password
 * reset, never does. */
function notificationSection(
  model: AccountsDetailModel,
  account: UiAccountEntry,
): m.Children {
  const preferences = model.notificationPreferencesFor(account.user_id);
  if (!preferences.isLoaded)
    return m(
      "div",
      { class: "flex items-center gap-2 type-helper text-tertiary" },
      [m(Spinner, { size: "sm" }), "Loading notification settings…"],
    );
  if (preferences.channels === null)
    return m(
      "div",
      { class: "type-helper text-tertiary" },
      "Notification settings are unavailable right now (could not reach Imbue Cloud).",
    );
  const isEnabled = preferences.channels.email_enabled;
  return m("div", { class: "flex items-center justify-between gap-4" }, [
    m("div", [
      m("div", { class: "type-label text-secondary" }, "Email notifications"),
      m(
        "div",
        { class: "type-helper text-tertiary" },
        "Invitations to workspaces shared with you, and other notifications. " +
          "Emails your account needs, such as password resets, are always sent.",
      ),
    ]),
    m("span", { class: "flex shrink-0 items-center gap-2" }, [
      preferences.isWriting ? m(Spinner, { size: "sm" }) : null,
      m("button", {
        type: "button",
        role: "switch",
        "aria-checked": isEnabled ? "true" : "false",
        "aria-label": "Email notifications",
        "data-email-notifications": account.user_id,
        class: preferences.isWriting
          ? "perm-switch shrink-0 is-busy"
          : "perm-switch shrink-0",
        disabled: preferences.isWriting,
        onclick: () =>
          void model.setEmailNotifications(account.user_id, !isEnabled),
      }),
      m(
        "span",
        { class: "type-body text-secondary" },
        isEnabled ? "On" : "Off",
      ),
    ]),
  ]);
}

export function AccountCard(): m.Component<AccountCardAttrs> {
  const selectedPlanByUserId = new Map<string, string>();
  const isExplorerAgreementCheckedByUserId = new Map<string, boolean>();
  return {
    view(vnode) {
      const { model, account } = vnode.attrs;
      const workspaceNoun = "workspace(s)";
      return m(Card, [
        m("div", { class: "flex items-center justify-between" }, [
          m("div", [
            m("div", { class: "font-semibold" }, [
              account.email,
              !account.is_enabled
                ? m(
                    StatusBadge,
                    {
                      variant: "warn",
                      size: "xs",
                      extra: "ml-2",
                      title:
                        "Session was rejected by the server. Sign in again to re-enable.",
                    },
                    "Signed out",
                  )
                : null,
            ]),
            m(
              "div",
              { class: "type-helper text-tertiary" },
              `${account.workspace_count} ${workspaceNoun}${account.is_default ? " · Default" : ""}`,
            ),
          ]),
          m("div", { class: "flex gap-2" }, [
            !account.is_enabled
              ? m(
                  Button,
                  {
                    variant: "primary",
                    onclick: () =>
                      void webLogin.start("", { isClosedOnSignIn: true }),
                  },
                  "Sign in again",
                )
              : null,
            account.is_default
              ? m(
                  "span",
                  {
                    class:
                      "inline-flex items-center justify-center px-3 py-2 rounded-md type-label bg-fill-subtle " +
                      "text-primary border border-default opacity-60 cursor-default",
                  },
                  "Default",
                )
              : m(
                  Button,
                  {
                    variant: "secondary",
                    onclick: () => void model.setDefault(account.user_id),
                  },
                  "Set default",
                ),
            m(
              Button,
              {
                variant: "danger",
                disabled: model.isLoggingOut(account.user_id),
                onclick: () => void model.logOut(account.user_id),
              },
              model.isLoggingOut(account.user_id)
                ? [m(Spinner, { size: "sm" }), "Logging out…"]
                : "Log out",
            ),
          ]),
        ]),
        m(
          "div",
          { class: "mt-3 pt-3 border-t border-default" },
          planSection(
            model,
            account,
            selectedPlanByUserId,
            isExplorerAgreementCheckedByUserId,
          ),
        ),
        m(
          "div",
          { class: "mt-3 pt-3 border-t border-default" },
          notificationSection(model, account),
        ),
      ]);
    },
  };
}
