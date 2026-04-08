/**
 * Hook system for the plugin API. Hooks allow plugins to intercept and
 * transform data at key points in the application lifecycle (e.g. when
 * conversations are fetched, messages are rendered, or stream events arrive).
 * Hooks are chained: each listener receives the result of the previous one.
 */

import type { Conversation } from "./models/Conversation";
import type { ResponseItem } from "./models/Response";

interface GetConversationsHookData {
  conversations: Conversation[];
}

interface GetConversationHookData {
  conversationId: string;
  responses: ResponseItem[];
}

interface PostConversationHookData {
  id: string;
  name: string;
  model: string;
}

interface PostConversationMessageHookData {
  conversationId: string;
  message: string;
  model: string;
  systemPrompt?: string;
}

interface GetResponseHookData {
  response: ResponseItem;
}

interface StreamEventHookData {
  conversationId: string;
  event: {
    type: string;
    content?: string;
    model?: string;
  };
}

interface InsertResponseHookData {
  conversationId: string;
  response: ResponseItem;
}

interface SlotRenderedHookData {
  slotName: string;
  container: HTMLElement;
}

type HookDataMap = {
  ready: void;
  get_conversations: GetConversationsHookData;
  get_conversation: GetConversationHookData;
  post_conversation: PostConversationHookData;
  post_conversation_message: PostConversationMessageHookData;
  get_response: GetResponseHookData;
  stream_event: StreamEventHookData;
  insert_response: InsertResponseHookData;
  slot_rendered: SlotRenderedHookData;
};

type HookName = keyof HookDataMap;

type HookCallback<T> = T extends void
  ? () => void | Promise<void>
  : (data: T) => T | undefined | void | Promise<T | undefined | void>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyHookCallback = (...args: any[]) => any;

const hookListeners: Record<string, AnyHookCallback[]> = {};
let readyFired = false;

export async function runHook<K extends HookName>(
  eventName: K,
  ...args: HookDataMap[K] extends void ? [] : [HookDataMap[K]]
): Promise<HookDataMap[K] extends void ? void : HookDataMap[K]> {
  const listeners = hookListeners[eventName];
  if (!listeners || listeners.length === 0) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return args[0] as any;
  }

  if (eventName === "ready") {
    readyFired = true;
    for (const callback of listeners) {
      await callback();
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return undefined as any;
  }

  let current = args[0];
  for (const callback of listeners) {
    const result = await callback(current);
    if (result !== undefined) {
      current = result;
    }
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return current as any;
}

export function registerHook<K extends HookName>(eventName: K, callback: HookCallback<HookDataMap[K]>): void {
  if (!hookListeners[eventName]) {
    hookListeners[eventName] = [];
  }
  hookListeners[eventName].push(callback as AnyHookCallback);

  // If "ready" has already fired, invoke late registrations immediately
  // so plugins loaded after the app bootstrap still work.
  if (eventName === "ready" && readyFired) {
    (callback as () => void | Promise<void>)();
  }
}

export type {
  GetConversationsHookData,
  GetConversationHookData,
  PostConversationHookData,
  PostConversationMessageHookData,
  GetResponseHookData,
  StreamEventHookData,
  InsertResponseHookData,
  SlotRenderedHookData,
  HookDataMap,
  HookName,
  HookCallback,
};
