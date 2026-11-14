// Workspace AI-key mint dialog (/settings/ai-keys?workspace=<host_id>): one
// button mints a LiteLLM key for the workspace and copies the env-var-style
// credential blob for pasting into the workspace's sign-in dialog. Port of
// templates/pages/AiKeys.jinja. Rendered inside the Shell's AppOverlay card --
// a dismissible modal floating over the machine it was opened from (matching
// the legacy openAiKeysModal), so this component supplies content only; the
// AppOverlay supplies the card chrome, padding, scroll, and close X.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { AiKeysContext } from "../../models/aiKeys";
import { AiKeysMintModel } from "../../models/aiKeys";
import type { ErrorAction } from "../../models/errors";
import { Button } from "../components/Button";
import { ErrorNotice, restartAppActions } from "../components/ErrorDisplay";
import { FormLabel, Textarea } from "../components/FormControls";
import { Spinner } from "../components/Spinner";
import { variantBesideErrors } from "../components/variantBesideErrors";

/** The minted-key form: the success body once the workspace's owning account
 * has resolved. Extracted so the view keeps the load / error / ready branches
 * flat. */
function aiKeysBody(
  model: AiKeysMintModel,
  context: AiKeysContext,
): m.Children {
  return [
    m(
      "h2",
      { class: "type-heading text-primary text-center mb-2" },
      "Get AI credentials for your workspace",
    ),
    m("p", { class: "type-body text-secondary text-center mb-2" }, [
      "This creates an AI key for ",
      m(
        "span",
        { class: "text-primary font-semibold" },
        context.workspace_display_name,
      ),
      ", billed to ",
      m("span", { class: "text-primary font-semibold" }, context.account_email),
      ".",
    ]),
    m(
      "p",
      { class: "type-helper text-tertiary text-center mb-6" },
      "The key has a daily spending budget. Your usage appears on your Imbue account.",
    ),
    m(
      "div",
      { class: "flex justify-center" },
      m(
        Button,
        {
          variant: variantBesideErrors(
            "primary",
            model.mintError !== null
              ? [
                  {
                    message: model.mintError.message,
                    actions: mintErrorActions(model),
                  },
                ]
              : [],
          ),
          extra: "w-80",
          disabled: model.isMinting,
          onclick: () => void model.mint(),
        },
        model.hasMintedOnce
          ? "Create another key"
          : "Create key & copy credentials",
      ),
    ),
    model.statusMessage !== ""
      ? m(
          "p",
          {
            role: "status",
            class: "mt-4 type-helper text-center text-secondary",
          },
          model.statusMessage,
        )
      : null,
    model.mintError !== null
      ? m(
          "div",
          { class: "mt-4" },
          m(ErrorNotice, {
            id: "ai-keys-mint-error",
            error: model.mintError,
            actions: mintErrorActions(model),
          }),
        )
      : null,
    model.credentialBlob !== ""
      ? m("div", { class: "mt-4" }, [
          m(FormLabel, { target: "credential-blob" }, "Credentials"),
          m(Textarea, {
            id: "credential-blob",
            name: "credential_blob",
            rows: 3,
            extra: "font-mono",
            readonly: true,
            value: model.credentialBlob,
            onclick: (event: Event) =>
              (event.target as HTMLTextAreaElement).select(),
          }),
        ])
      : null,
    m(
      "p",
      { class: "type-helper text-tertiary text-center mt-6" },
      "Then close this and paste the credentials into the Sign in with Imbue dialog.",
    ),
  ];
}

function mintErrorActions(model: AiKeysMintModel): ErrorAction[] {
  return model.isWindowLoginLapsed ? restartAppActions() : [];
}

function loadErrorActions(model: AiKeysMintModel): ErrorAction[] {
  if (model.isWindowLoginLapsed) return restartAppActions();
  return [
    {
      label: "Try again",
      onclick: () => void model.load(m.route.param("workspace") ?? ""),
    },
  ];
}

/** The workspace's Settings -> Account, where the account the server found
 * missing is linked; nothing when the page was opened without a workspace. */
function unlinkedWorkspaceActions(context: AiKeysContext): ErrorAction[] {
  if (context.workspace_id === "") return [];
  return [
    {
      label: "Open workspace settings",
      onclick: () => {
        const { shell, stores } = getAppContext();
        const agentId = stores.workspaces.toAgentScopedId(context.workspace_id);
        shell.routeTo(`/workspace/${agentId}/options`, {
          tab: "settings",
          group: "account",
        });
      },
    },
  ];
}

export function AiKeysPage(): m.Component {
  const model = new AiKeysMintModel();
  return {
    oninit(): void {
      void model.load(m.route.param("workspace") ?? "");
    },
    view(): m.Children {
      const context = model.context;
      // Content only: the Shell's AppOverlay card supplies the modal chrome
      // (backdrop, padding, internal scroll, and the close X).
      return [
        m(
          "h1",
          { class: "type-section text-primary mb-4" },
          "Sign in with Imbue",
        ),
        model.loadError !== null
          ? m(ErrorNotice, {
              id: "ai-keys-load-error",
              error: model.loadError,
              actions: loadErrorActions(model),
            })
          : context === null
            ? m(
                "div",
                { class: "flex items-center gap-2 type-helper text-tertiary" },
                [m(Spinner, { size: "sm" }), "Loading…"],
              )
            : context.error_message !== ""
              ? m(ErrorNotice, {
                  id: "ai-keys-context-error",
                  error: { message: context.error_message, details: "" },
                  actions: unlinkedWorkspaceActions(context),
                  isBugReportOffered: false,
                })
              : aiKeysBody(model, context),
      ];
    },
  };
}
