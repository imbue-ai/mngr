// One signed-in account's card on the Accounts page: identity row, actions,
// and the asynchronously-loaded plan/usage section. Port of the account loop
// in templates/pages/Accounts.jinja + AccountPlanSection.jinja + accounts.js.

import m from "mithril";
import type {
  AccountEntry,
  AccountsDetailModel,
} from "../../../models/accountsDetail";
import { Button, ButtonLink } from "../../components/Button";
import { Card } from "../../components/Card";
import { Link } from "../../components/Link";
import { routeLinkAttrs } from "../../components/route-link";
import { Select } from "../../components/FormControls";
import { Spinner } from "../../components/Spinner";
import { StatusBadge } from "../../components/StatusBadge";

interface AccountCardAttrs {
  model: AccountsDetailModel;
  account: AccountEntry;
}

function planSection(
  model: AccountsDetailModel,
  account: AccountEntry,
  selectedPlanByUserId: Map<string, string>,
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
  // The draft lives in component state (keyed by user_id): a per-render
  // local would be reset by the redraw that follows the select's onchange.
  const selectedPlan = selectedPlanByUserId.get(account.user_id) ?? view.plan_name;
  return m("div", [
    m("div", { class: "flex items-center justify-between mb-2" }, [
      m("div", { class: "type-label text-secondary" }, [
        "Plan: ",
        m(
          "span",
          { class: "font-semibold text-primary" },
          view.plan_display_name,
        ),
      ]),
      view.available_plans.length > 1
        ? m("div", { class: "flex items-center gap-2" }, [
            m(
              Select,
              {
                name: "plan",
                width: "w-32",
                onchange: (event: Event) => {
                  selectedPlanByUserId.set(account.user_id, (event.target as HTMLSelectElement).value);
                },
              },
              view.available_plans.map((plan_name) =>
                m(
                  "option",
                  { value: plan_name, selected: plan_name === selectedPlan },
                  plan_name.charAt(0).toUpperCase() + plan_name.slice(1),
                ),
              ),
            ),
            m(
              Button,
              {
                variant: "secondary",
                onclick: () =>
                  void model.switchPlan(account.user_id, selectedPlan),
              },
              "Switch plan",
            ),
          ])
        : null,
    ]),
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
                  "Removes the oldest backups (each machine keeps its latest) until you are back under the limit.",
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
            { extra: "type-helper", ...routeLinkAttrs("/workspaces/destroyed") },
            "Review destroyed machine backups →",
          ),
        )
      : null,
  ]);
}

export function AccountCard(): m.Component<AccountCardAttrs> {
  const selectedPlanByUserId = new Map<string, string>();
  return {
    view(vnode) {
      const { model, account } = vnode.attrs;
      const machineNoun = "machine(s)";
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
              `${account.workspace_count} ${machineNoun}${account.is_default ? " · Default" : ""}`,
            ),
          ]),
          m("div", { class: "flex gap-2" }, [
            !account.is_enabled
              ? m(
                  ButtonLink,
                  { href: "/auth/login", variant: "primary" },
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
                onclick: () => void model.logOut(account.user_id),
              },
              "Log out",
            ),
          ]),
        ]),
        m(
          "div",
          { class: "mt-3 pt-3 border-t border-default" },
          planSection(model, account, selectedPlanByUserId),
        ),
      ]);
    },
  };
}
