// The associate-workspace prompt (mirror of the old Associate.jinja),
// rendered inside the workspace settings (and, later, sharing) surfaces when
// the workspace has no associated account yet. Either offers a picker of the
// signed-in accounts (PATCHing the v1 workspace resource's account_id, then
// returning to ``redirectUrl`` or reloading), or links to sign-in when no
// account exists.
import m from "mithril";

import type { AssociateAccountPayload } from "../chrome_state";
import { buttonClasses, SELECT_CLASSES } from "../ui";
import { Icon } from "./Icon";

export interface AssociatePromptAttrs {
  agentId: string;
  accounts: AssociateAccountPayload[];
  redirectUrl?: string;
}

export function AssociatePrompt(): m.Component<AssociatePromptAttrs> {
  let selectedUserId: string | null = null;
  let isSubmitInFlight = false;
  let error: string | null = null;

  async function submit(agentId: string, redirectUrl: string | undefined): Promise<void> {
    if (selectedUserId === null || selectedUserId === "") return;
    isSubmitInFlight = true;
    error = null;
    m.redraw();
    try {
      const response = await fetch(`/api/v1/workspaces/${encodeURIComponent(agentId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: selectedUserId }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (redirectUrl !== undefined && redirectUrl !== "") window.location.href = redirectUrl;
      else window.location.reload();
    } catch (submitError) {
      isSubmitInFlight = false;
      error = `Could not associate account: ${submitError instanceof Error ? submitError.message : String(submitError)}`;
      m.redraw();
    }
  }

  return {
    view(vnode) {
      const { agentId, accounts, redirectUrl } = vnode.attrs;
      if (selectedUserId === null && accounts.length > 0) selectedUserId = accounts[0].user_id;
      return m("div", { class: "minds-card p-4 my-3" }, [
        m(
          "p",
          { class: "font-semibold mb-2" },
          "This workspace needs to be associated with an account before sharing can be configured.",
        ),
        accounts.length > 0
          ? [
              m("div", { id: "associate-form", class: "flex gap-2 items-center mt-2" }, [
                m("div", { class: "relative w-auto" }, [
                  m(
                    "select",
                    {
                      name: "user_id",
                      class: SELECT_CLASSES,
                      value: selectedUserId,
                      onchange: (event: Event) => {
                        selectedUserId = (event.target as HTMLSelectElement).value;
                      },
                    },
                    accounts.map((account) => m("option", { value: account.user_id }, account.email)),
                  ),
                  m(
                    "span",
                    { class: "pointer-events-none absolute inset-y-0 right-2 flex items-center text-secondary" },
                    m(Icon, { name: "chevron-down" }),
                  ),
                ]),
                m(
                  "button",
                  {
                    type: "button",
                    class: buttonClasses("primary"),
                    disabled: isSubmitInFlight,
                    onclick: () => void submit(agentId, redirectUrl),
                  },
                  "Associate",
                ),
              ]),
              error !== null
                ? m("p", { id: "associate-error", class: "type-body text-important mt-2", role: "alert" }, error)
                : null,
            ]
          : m("p", { class: "mt-2" }, [
              m("a", { href: "/auth/login", class: "text-accent hover:underline" }, "Sign in or create an account"),
              " to enable sharing.",
            ]),
      ]);
    },
  };
}
