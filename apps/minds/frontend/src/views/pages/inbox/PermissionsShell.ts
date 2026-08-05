// Shared chrome for every request-detail dialog: header, rationale card,
// the credential form a manual-credentials service asks for, Approve/Deny row,
// and the progress / error notices (the SPA twin of the Permissions* JinjaX
// components + the inbox shell's submission UI states).

import m from "mithril";
import type { InboxModel } from "../../../models/inbox";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { Spinner } from "../../components/Spinner";

const TEXT_INPUT_CLASS =
  "w-full rounded-md border border-default bg-surface-primary px-2 py-1 type-body text-primary";

/** Bring a just-rendered failure message into view.
 *
 * The dialog scrolls, and the credential form sits above a permission list that
 * can be long, so after clicking Approve the user is often left looking at the
 * buttons with the reason off-screen. Scrolls the message itself, by the least
 * amount that shows it, so the buttons stay as close as they can. Runs at most
 * once per failed approval (the model hands the scroll out a single time), so
 * it never fights the user's own scrolling on later redraws. */
function scrollFailureIntoView(model: InboxModel, vnode: m.VnodeDOM): void {
  if (!model.takePendingFailureScroll()) return;
  (vnode.dom as HTMLElement).scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/** The credential form's heading: the opening instruction, or -- once an
 * attempt has failed -- that attempt's reason, as an error notice.
 *
 * A failure also carries the scroll-into-view hooks, on a plain wrapper because
 * that is what mithril's typings accept lifecycle hooks on. */
function manualCredentialsMessage(model: InboxModel, message: string): m.Children {
  const attrs = { id: "permissions-manual-credentials-message", role: "alert" };
  if (!model.isManualCredentialsFailureShown()) {
    return m("p", { ...attrs, class: "type-body text-secondary" }, message);
  }
  return m(
    "div",
    {
      oncreate: (vnode: m.VnodeDOM) => scrollFailureIntoView(model, vnode),
      onupdate: (vnode: m.VnodeDOM) => scrollFailureIntoView(model, vnode),
    },
    m(Notice, { variant: "error", ...attrs }, message),
  );
}

/** One labeled input per value the service's credential command needs, plus the
 * account name when a new account has to be named. The command itself is never
 * shown: Minds runs it, so it is not something the user has to know about. */
function manualCredentialsForm(model: InboxModel): m.Children {
  const prompt = model.manualCredentialsPrompt();
  if (prompt === null) return null;
  // Nothing to fill in: the message is the whole thing.
  if (prompt.parameters.length === 0) {
    return m("div", { id: "permissions-manual-credentials" }, manualCredentialsMessage(model, prompt.message));
  }
  return m(
    "div",
    {
      id: "permissions-manual-credentials",
      class: "rounded-md border border-default bg-fill-subtle p-3 flex flex-col gap-2",
    },
    [
      manualCredentialsMessage(model, prompt.message),
      ...prompt.parameters.map((parameter) =>
        m("label", { class: "flex flex-col gap-1" }, [
          m("span", { class: "type-label text-secondary" }, parameter.label),
          m("input", {
            type: "text",
            name: `credential-${parameter.name}`,
            class: TEXT_INPUT_CLASS,
            autocomplete: "off",
            spellcheck: "false",
            value: model.manualCredentialValues[parameter.name] ?? "",
            oninput: (event: Event) => {
              model.manualCredentialValues[parameter.name] = (event.target as HTMLInputElement).value;
            },
          }),
        ]),
      ),
      model.isManualAccountNameNeeded()
        ? m("label", { class: "flex flex-col gap-1" }, [
            m("span", { class: "type-label text-secondary" }, "Account name"),
            m("input", {
              type: "text",
              name: "account_name",
              class: TEXT_INPUT_CLASS,
              autocomplete: "off",
              spellcheck: "false",
              value: model.manualAccountName,
              oninput: (event: Event) => {
                model.manualAccountName = (event.target as HTMLInputElement).value;
              },
            }),
            m("span", { class: "type-helper text-tertiary" }, "How this account is labelled in Minds."),
          ])
        : null,
    ],
  );
}

export interface PermissionsShellAttrs {
  model: InboxModel;
  headerLabel: string;
  wsName: string;
  rationale: string;
  /** Notice shown while an approval is running (kind-specific copy). */
  progressLabel: string;
  body: m.Children;
}

export function PermissionsShell(): m.Component<PermissionsShellAttrs> {
  return {
    view(vnode) {
      const { model, headerLabel, wsName, rationale, progressLabel, body } = vnode.attrs;
      // A credential form with no inputs is a dead end (Minds cannot work out
      // what to ask for), so Approve goes away entirely.
      const manualPrompt = model.manualCredentialsPrompt();
      const isApproveHidden = manualPrompt !== null && manualPrompt.parameters.length === 0;
      return m("div", { class: "flex flex-col gap-4" }, [
        m("div", [
          m("h2", { class: "type-heading text-primary" }, headerLabel),
          m("p", { class: "type-helper text-tertiary mt-0.5" }, ["Requested by an agent in ", m("b", wsName)]),
        ]),
        rationale
          ? m("div", { class: "rounded-md border border-default bg-fill-subtle p-3 type-body text-secondary" }, rationale)
          : null,
        // The credential form comes first: it is what the user has to act on.
        manualCredentialsForm(model),
        body,
        m("div", { class: "flex items-center gap-2" }, [
          isApproveHidden
            ? null
            : m(
                Button,
                {
                  variant: "primary",
                  id: "permissions-approve-btn",
                  disabled: !model.isApproveAllowed(),
                  onclick: () => void model.approve(),
                },
                model.isApproveBusy ? [m(Spinner, { size: "sm", extra: "mr-1.5" }), "Approving…"] : "Approve",
              ),
          m(
            Button,
            {
              variant: "secondary",
              id: "permissions-deny-btn",
              disabled: model.isApproveBusy,
              onclick: () => model.deny(),
            },
            "Deny",
          ),
        ]),
        model.isProgressShown
          ? m(
              "div",
              { id: "permissions-progress", class: "rounded-md border border-default bg-fill-subtle p-3 type-body text-secondary" },
              progressLabel,
            )
          : null,
        model.errorMessage !== null
          ? m(
              "div",
              {
                id: "permissions-error",
                oncreate: (errorVnode: m.VnodeDOM) => scrollFailureIntoView(model, errorVnode),
                onupdate: (errorVnode: m.VnodeDOM) => scrollFailureIntoView(model, errorVnode),
              },
              m(
                Notice,
                { variant: "error", role: "alert" },
                m("span", { id: "permissions-error-message" }, model.errorMessage),
              ),
            )
          : null,
      ]);
    },
  };
}
