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
import type { SidebarItemDefinition } from "./sidebar-items";
import { registerSidebarItem } from "./sidebar-items";

interface LlmApi {
  claim(slotName: string, renderCallback?: SlotRenderCallback): boolean;
  registerRoute(path: string, handler: RouteRenderCallback | PluginRouteHandler): boolean;
  getResponse(responseId: string): Promise<ResponseItem | null>;
  getConversations(): Conversation[];
  getConversation(conversationId: string): Conversation | null;
  insertResponse(conversationId: string, responseItem: ResponseItem): Promise<void>;
  registerSidebarItem(definition: SidebarItemDefinition): void;
  on<K extends HookName>(eventName: K, callback: HookCallback<HookDataMap[K]>): void;
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

  registerSidebarItem(definition: SidebarItemDefinition): void {
    registerSidebarItem(definition);
  },

  on<K extends HookName>(eventName: K, callback: HookCallback<HookDataMap[K]>): void {
    registerHook(eventName, callback);
  },
};

export { llmApi };

export type { LlmApi };
