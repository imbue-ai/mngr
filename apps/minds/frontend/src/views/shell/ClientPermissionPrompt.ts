// The prompt a workspace's page raises when it first asks for a client
// permission (the client-permissions spec, section 5.5): Studio's own modal,
// drawn by this window over the workspace and naming the workspace the way the
// Permissions tab does, so it reads as Studio's and not the page's. What an
// answer does is written on the button that does it: with Remember checked the
// buttons read Allow and Don't allow and the answer is stored; unchecked they
// read Allow for this session and Don't allow this time, and nothing is (D12).
// Which ask is up, and what an answer does, is the model's
// (models/clientPermissions.ts); this only says it and takes the answer.

import m from "mithril";
import type {
  ClientPermissionAsk,
  ClientPermissionName,
} from "../../electron-bridge";
import type { ClientPermissionPromptModel } from "../../models/clientPermissions";
import { Button } from "../components/Button";
import { DialogCloseButton, Modal } from "../components/Modal";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";

/** What the title calls each capability. */
const CAPABILITY_NOUNS: Record<ClientPermissionName, string> = {
  microphone: "your microphone",
};

/** What the page is about to do with it, and where the answer applies. */
const PROMPT_BODIES: Record<ClientPermissionName, string> = {
  microphone:
    "This workspace's pages want to listen to an audio input. The answer applies only on this computer.",
};

export function clientPermissionPromptTitle(ask: ClientPermissionAsk): string {
  return `Allow "${ask.workspaceName}" to use ${CAPABILITY_NOUNS[ask.permission]}?`;
}

export interface ClientPermissionPromptAttrs {
  model: ClientPermissionPromptModel;
  /** The workspace's accent, as the titlebar paints it; null when unknown. */
  accent: string | null;
}

export function ClientPermissionPrompt(): m.Component<ClientPermissionPromptAttrs> {
  return {
    view(vnode) {
      const { model, accent } = vnode.attrs;
      const ask = model.current;
      if (ask === null) return null;
      const { isRemembered, isBusy } = model;
      const isRememberFailed = model.errorMessage !== "";
      return m(
        Modal,
        {
          isOpen: true,
          onClose: () => model.dismiss(),
          size: "md",
          cardExtra: "relative",
          id: "client-permission-prompt",
        },
        [
          m(DialogCloseButton, { onClose: () => model.dismiss() }),
          m("div", { class: "flex flex-col gap-4" }, [
            m(
              "h2",
              {
                class:
                  "type-heading text-primary pr-8 flex items-center gap-2 min-w-0",
              },
              [
                accent === null
                  ? null
                  : m("span", {
                      class: "inline-block h-3 w-3 shrink-0 rounded-full",
                      style: `background-color: ${accent}`,
                      "aria-hidden": "true",
                    }),
                m(
                  "span",
                  { class: "min-w-0" },
                  clientPermissionPromptTitle(ask),
                ),
              ],
            ),
            m(
              "p",
              { class: "type-body text-secondary" },
              PROMPT_BODIES[ask.permission],
            ),
            isRememberFailed
              ? [
                  // The answer has reached main as a one-time one; only the
                  // remembering failed, and that is what is left to put away.
                  m(
                    Notice,
                    { variant: "error", role: "alert" },
                    model.errorMessage,
                  ),
                  m(
                    "div",
                    { class: "flex justify-end" },
                    m(
                      Button,
                      {
                        variant: "secondary",
                        id: "client-permission-prompt-close",
                        onclick: () => model.dismiss(),
                      },
                      "Close",
                    ),
                  ),
                ]
              : [
                  m(
                    "label",
                    { class: "flex items-start gap-2 cursor-pointer" },
                    [
                      m("input", {
                        type: "checkbox",
                        id: "client-permission-prompt-remember",
                        class: "mt-1 shrink-0",
                        checked: isRemembered,
                        disabled: isBusy,
                        onchange: (event: Event) =>
                          model.setRemembered(
                            (event.target as HTMLInputElement).checked,
                          ),
                      }),
                      m(
                        "span",
                        { class: "type-body text-primary" },
                        "Remember for this workspace on this computer",
                      ),
                    ],
                  ),
                  m(
                    "div",
                    {
                      class:
                        "flex flex-wrap items-center justify-end gap-2 pt-1",
                    },
                    [
                      m(
                        Button,
                        {
                          variant: "secondary",
                          id: "client-permission-prompt-deny",
                          disabled: isBusy,
                          onclick: () => void model.choose("denied"),
                        },
                        model.pendingDecision === "denied"
                          ? m(Spinner, { size: "sm" })
                          : isRemembered
                            ? "Don't allow"
                            : "Don't allow this time",
                      ),
                      m(
                        Button,
                        {
                          variant: "primary",
                          id: "client-permission-prompt-allow",
                          disabled: isBusy,
                          onclick: () => void model.choose("granted"),
                        },
                        model.pendingDecision === "granted"
                          ? m(Spinner, { size: "sm" })
                          : isRemembered
                            ? "Allow"
                            : "Allow for this session",
                      ),
                    ],
                  ),
                ],
          ]),
        ],
      );
    },
  };
}
