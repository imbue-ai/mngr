import m from "mithril";
import { getAgentById } from "../models/AgentManager";
import { interruptAgent, sendMessage } from "../models/Response";

const MAX_TEXTAREA_HEIGHT_PX = 200;

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

function messageTextKey(agentId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${agentId}`;
}

let messageText = "";
let currentAgentId: string | null = null;
let messageTextareaElement: HTMLTextAreaElement | null = null;

function autoResizeTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT_PX ? "auto" : "hidden";
}

function focusMessageTextarea(): void {
  messageTextareaElement?.focus();
}

// Compatibility export
export function setSelectedModelId(_modelId: string): void {}

export const MessageInput: m.Component<{ agentId: string | null }> = {
  view(vnode) {
    const agentId = vnode.attrs.agentId;

    if (!agentId) {
      return null;
    }

    if (currentAgentId !== agentId) {
      currentAgentId = agentId;
      messageText = localStorage.getItem(messageTextKey(agentId)) ?? "";
    }

    async function handleSend(): Promise<void> {
      if (!agentId || !messageText.trim()) {
        return;
      }

      const text = messageText;
      messageText = "";
      localStorage.removeItem(messageTextKey(agentId));
      m.redraw();

      try {
        await sendMessage(agentId, text);
      } catch {
        // Fire-and-forget: response comes via SSE
      }

      requestAnimationFrame(() => {
        focusMessageTextarea();
      });
    }

    async function handleInterrupt(): Promise<void> {
      if (!agentId) {
        return;
      }
      try {
        await interruptAgent(agentId);
      } catch {
        // Fire-and-forget: errors are non-fatal; user can retry.
      }
    }

    function handleKeydown(event: KeyboardEvent): void {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        handleSend();
      }
    }

    const hasMessageText = messageText.trim().length > 0;
    const showStopButton = getAgentById(agentId)?.supports_interrupt === true;

    return m("div", { class: "message-input mx-auto w-full" }, [
      m("div", { class: "message-input-box flex flex-col" }, [
        m("textarea", {
          class: "message-input-textbox w-full resize-none focus:outline-none",
          placeholder: "Type a message...",
          rows: 1,
          value: messageText,
          oncreate: (textareaVnode: m.VnodeDOM) => {
            messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
            autoResizeTextarea(messageTextareaElement);
            focusMessageTextarea();
          },
          onupdate: (textareaVnode: m.VnodeDOM) => {
            messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
            autoResizeTextarea(messageTextareaElement);
          },
          onremove: () => {
            messageTextareaElement = null;
          },
          oninput: (event: Event) => {
            const textarea = event.target as HTMLTextAreaElement;
            messageText = textarea.value;
            localStorage.setItem(messageTextKey(agentId), messageText);
            autoResizeTextarea(textarea);
          },
          onkeydown: handleKeydown,
        }),
        m("div", { class: "message-input-toolbar" }, [
          m(
            "div",
            { class: "message-input-toolbar-left" },
            showStopButton
              ? m(
                  "button",
                  {
                    class: "message-input-stop-button",
                    title: "Interrupt current turn",
                    onclick: handleInterrupt,
                  },
                  m.trust(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
                  ),
                )
              : null,
          ),
          hasMessageText
            ? m(
                "button",
                {
                  class: "message-input-send-button",
                  onclick: handleSend,
                },
                m.trust(
                  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
                ),
              )
            : null,
        ]),
      ]),
    ]);
  },
};
