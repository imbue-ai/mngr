// Shared non-fixture test utilities, explicitly imported by *.test.ts files.
// Deliberately NOT named *.test.ts so vitest does not collect it as a suite.

import m from "mithril";
import type { UiWorkspacesMessage } from "./channel/messages";

/** Render a component to its root vnode by instantiating the closure and
 * calling view() directly -- the inner-app idiom of testing render logic
 * without a DOM. m() normalizes attrs/children exactly as mithril would at
 * runtime. */
export function renderRoot<A>(
  component: () => m.Component<A>,
  attrs: A,
  ...children: m.Children[]
): m.Vnode {
  const instance = component() as unknown as m.Component;
  const vnode = m(instance, attrs as m.Attributes, ...children) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
    instance,
    vnode,
  );
}

/** Every string a rendered tree would put on screen, for the assertions that
 * are about what the reader sees rather than about markup. */
export function renderedText(vnode: m.Vnode | null): string {
  if (vnode === null || vnode === undefined) return "";
  if (typeof vnode === "string" || typeof vnode === "number") return String(vnode);
  if (Array.isArray(vnode)) return vnode.map(renderedText).join(" ");
  const text = (vnode as unknown as { text?: unknown }).text;
  if (text !== undefined && text !== null) return String(text);
  const children = (vnode as unknown as { children?: unknown }).children;
  return renderedText(children as m.Vnode | null);
}

/** A one-workspace list message: agent `agent-aa11` on host `host-bb22`. */
export function workspacesMessage(overrides: Partial<UiWorkspacesMessage> = {}): UiWorkspacesMessage {
  return {
    type: "workspaces",
    workspaces: [
      {
        id: "agent-aa11",
        name: "alpha",
        accent: "#aabbcc",
        host_id: "host-bb22",
        is_stale: false,
        supports_shutdown: true,
        liveness: "RUNNING",
        account: "",
        create_attempt_state: "",
        is_remote: false,
        location: "",
      },
    ],
    destroying_agent_ids: [],
    restorable_workspace_ids: ["agent-aa11", "host-bb22"],
    remote_workspace_states: {},
    ...overrides,
  };
}

/** A JSON Response carrying the given payload with the given status. */
export function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Run `run` with `globalThis.fetch` swapped for a stub that mimics the
 * browser's receiver check: it throws "Illegal invocation" unless invoked as
 * a plain call (as a model's default `fetchImpl` wrapper must), and otherwise
 * resolves to a JSON response carrying `payload`. Restores the real fetch. */
export async function withReceiverGuardedGlobalFetch(payload: unknown, run: () => Promise<void>): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = function (this: unknown) {
    if (this !== undefined && this !== globalThis) {
      throw new TypeError("Illegal invocation");
    }
    return Promise.resolve(jsonResponse(payload));
  } as typeof fetch;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

/** Flush pending promise work: three microtask hops cover the await chains
 * inside the models (fetch result -> body parse -> state application). */
export async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
