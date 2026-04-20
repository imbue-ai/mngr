import type { Conversation } from "./models/Conversation";
import { getConversations } from "./models/Conversation";
import type { ResponseItem } from "./models/Response";
import { getAllResponses, insertResponseItem } from "./models/Response";
import type { HookDataMap, HookName, HookCallback } from "./hooks";
import { runHook, registerHook } from "./hooks";
import { claimSlot } from "./slots";
import type { SlotRenderCallback } from "./slots";
import type { RouteRenderCallback, PluginRouteHandler } from "./plugin-routes";
import { registerPluginRoute } from "./plugin-routes";
import { getPrimaryAgentId } from "./base-path";
import { openIframeTabForAgent, openSubagentTab } from "./views/DockviewWorkspace";

interface OpenTabOptions {
  type: "iframe" | "subagent";
  url?: string;
  title?: string;
  subagentSessionId?: string;
}

interface LlmApi {
  claim(slotName: string, renderCallback?: SlotRenderCallback): boolean;
  registerRoute(path: string, handler: RouteRenderCallback | PluginRouteHandler): boolean;
  getResponse(responseId: string): Promise<ResponseItem | null>;
  getConversations(): Conversation[];
  getConversation(conversationId: string): Conversation | null;
  insertResponse(conversationId: string, responseItem: ResponseItem): Promise<void>;
  on<K extends HookName>(eventName: K, callback: HookCallback<HookDataMap[K]>): void;
  openTab(options: OpenTabOptions): void;
}

const llmApi: LlmApi = {
  claim(slotName: string, renderCallback?: SlotRenderCallback): boolean {
    return claimSlot(slotName, renderCallback);
  },

  registerRoute(path: string, handler: RouteRenderCallback | PluginRouteHandler): boolean {
    return registerPluginRoute(path, handler);
  },

  async getResponse(responseId: string): Promise<ResponseItem | null> {
    for (const responses of Object.values(getAllResponses())) {
      for (const item of responses) {
        if (item.id === responseId) {
          const hookResult = await runHook("get_response", { response: item });
          return hookResult.response;
        }
      }
    }
    return null;
  },

  getConversations(): Conversation[] {
    return [...getConversations()];
  },

  getConversation(conversationId: string): Conversation | null {
    return getConversations().find((conversation) => conversation.id === conversationId) ?? null;
  },

  async insertResponse(_conversationId: string, _responseItem: ResponseItem): Promise<void> {
    await insertResponseItem();
  },

  on<K extends HookName>(eventName: K, callback: HookCallback<HookDataMap[K]>): void {
    registerHook(eventName, callback);
  },

  openTab(options: OpenTabOptions): void {
    const agentId = getPrimaryAgentId();
    if (!agentId) return;

    if (options.type === "subagent" && options.subagentSessionId) {
      openSubagentTab(agentId, options.subagentSessionId, options.title ?? "Sub-agent");
    } else if (options.type === "iframe" && options.url) {
      openIframeTabForAgent(agentId, options.url, options.title ?? options.url);
    }
  },
};

export { llmApi };

export type { LlmApi };
