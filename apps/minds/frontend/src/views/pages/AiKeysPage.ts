// Workspace AI-key mint dialog (/settings/ai-keys?workspace=<host_id>): one
// button mints a LiteLLM key for the workspace and copies the env-var-style
// credential blob for pasting into the workspace's sign-in dialog. Port of
// templates/pages/AiKeys.jinja. Rendered inside the Shell's AppOverlay card --
// a dismissible modal floating over the machine it was opened from (matching
// the legacy openAiKeysModal), so this component supplies content only; the
// AppOverlay supplies the card chrome, padding, scroll, and close X.

import m from "mithril";
import { Button } from "../components/Button";
import { FormLabel, Textarea } from "../components/FormControls";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";

interface AiKeysContext {
  workspace_host_id: string;
  workspace_display_name: string;
  account_email: string;
  error_message: string;
}

class AiKeysMintModel {
  context: AiKeysContext | null = null;
  isLoadFailed = false;
  isMinting = false;
  hasMintedOnce = false;
  statusMessage = "";
  isStatusError = false;
  credentialBlob = "";

  async load(): Promise<void> {
    const workspace = m.route.param("workspace") ?? "";
    try {
      const response = await fetch(
        `/ui/api/ai-keys?workspace=${encodeURIComponent(workspace)}`,
        {
          credentials: "same-origin",
        },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.context = (await response.json()) as AiKeysContext;
    } catch {
      this.isLoadFailed = true;
    }
    m.redraw();
  }

  async mint(): Promise<void> {
    const context = this.context;
    if (context === null || this.isMinting) return;
    this.isMinting = true;
    this.statusMessage = "Creating key...";
    this.isStatusError = false;
    m.redraw();
    try {
      const response = await fetch("/settings/ai-keys/mint", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: context.workspace_host_id }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        credentials?: string;
        error?: string;
      };
      if (response.status !== 200 || !data.credentials) {
        this.statusMessage = data.error ?? "Could not create the key.";
        this.isStatusError = true;
      } else {
        // Always reveal the blob so the user can copy manually if the
        // clipboard write is rejected (insecure context, permissions).
        this.credentialBlob = data.credentials;
        this.hasMintedOnce = true;
        try {
          await navigator.clipboard.writeText(data.credentials);
          this.statusMessage =
            "Credentials copied to your clipboard. Paste them into the workspace sign-in dialog.";
        } catch {
          this.statusMessage =
            "Key created. Copy the credentials below and paste them into the workspace sign-in dialog.";
        }
        this.isStatusError = false;
      }
    } catch {
      this.statusMessage = "Could not reach the server. Please try again.";
      this.isStatusError = true;
    }
    this.isMinting = false;
    m.redraw();
  }
}

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
      "Get AI credentials for your machine",
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
          variant: "primary",
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
            class:
              "mt-4 type-helper text-center " +
              (model.isStatusError ? "text-important" : "text-secondary"),
          },
          model.statusMessage,
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

export function AiKeysPage(): m.Component {
  const model = new AiKeysMintModel();
  return {
    oninit(): void {
      void model.load();
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
        model.isLoadFailed
          ? m(
              Notice,
              { variant: "error" },
              "This page could not be loaded. Refresh to try again.",
            )
          : context === null
            ? m(
                "div",
                { class: "flex items-center gap-2 type-helper text-tertiary" },
                [m(Spinner, { size: "sm" }), "Loading…"],
              )
            : context.error_message !== ""
              ? m(Notice, { variant: "error" }, context.error_message)
              : aiKeysBody(model, context),
      ];
    },
  };
}
