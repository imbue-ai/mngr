// Shared non-fixture test utilities, explicitly imported by *.test.ts files.
// Deliberately NOT named *.test.ts so vitest does not collect it as a suite.

import m from "mithril";
import type {
  UiNotificationEntry,
  UiWorkspacesMessage,
} from "./channel/messages";
import type { SettingsOverview } from "./models/settings";

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
  if (typeof vnode === "string" || typeof vnode === "number")
    return String(vnode);
  if (Array.isArray(vnode)) return vnode.map(renderedText).join(" ");
  const text = (vnode as unknown as { text?: unknown }).text;
  if (text !== undefined && text !== null) return String(text);
  const children = (vnode as unknown as { children?: unknown }).children;
  return renderedText(children as m.Vnode | null);
}

/** A one-workspace list message: agent `agent-aa11` on host `host-bb22`. */
export function workspacesMessage(
  overrides: Partial<UiWorkspacesMessage> = {},
): UiWorkspacesMessage {
  return {
    type: "workspaces",
    workspaces: [
      {
        id: "agent-aa11",
        name: "alpha",
        accent: "#aabbcc",
        host_id: "host-bb22",
        is_backend_unreachable: false,
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

/** One notification-feed entry as the wire carries it: an unresolved
 * permission ask from the workspacesMessage workspace (alpha / agent-aa11),
 * with every field overridable per test. */
export function notificationEntry(
  id: string,
  overrides: Partial<UiNotificationEntry> = {},
): UiNotificationEntry {
  return {
    id,
    kind: "permission_request",
    created_at: "2026-08-18T00:00:00Z",
    is_resolved: false,
    outcome: null,
    title: "Slack access",
    body: "wants to read messages",
    request_id: `req-${id}`,
    workspace_agent_id: "agent-aa11",
    workspace_name: "alpha",
    workspace_accent: "#aabbcc",
    service_name: "",
    ...overrides,
  };
}

/** The `/ui/api/settings` payload, with every permission surface empty. */
export function settingsOverview(overrides: Partial<SettingsOverview> = {}): SettingsOverview {
  return {
    services_overview: [],
    file_sharing_grants: [],
    workspace_delegation_grants: [],
    permissions_unavailable: false,
    is_master_password_set: false,
    report_unexpected_errors: true,
    version: "v-one",
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
export async function withReceiverGuardedGlobalFetch(
  payload: unknown,
  run: () => Promise<void>,
): Promise<void> {
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

/** An in-memory stand-in for the sticky-preference `localStorage`, injected
 * through the models' `storage` option (tests run under node, which has no
 * `localStorage` at all). `values` exposes what was written. */
export function memoryStorage(): Pick<Storage, "getItem" | "setItem"> & {
  values: Map<string, string>;
} {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
}

/** Run `run` with `window.mindsNative` set to `surface`, or with a `window`
 * carrying no bridge at all when it is null -- which is the browser build.
 *
 * The bridge resolves `window.mindsNative` on every call, so a plain
 * assignment is enough; vitest runs in the node environment, where `window`
 * is otherwise absent and every bridge call would throw on the bare global. */
export async function withMindsNative(
  surface: Record<string, unknown> | null,
  run: () => Promise<void>,
): Promise<void> {
  const globals = globalThis as { window?: unknown };
  const original = globals.window;
  globals.window = surface === null ? {} : { mindsNative: surface };
  try {
    await run();
  } finally {
    if (original === undefined) delete globals.window;
    else globals.window = original;
  }}

// -- Walking a rendered vnode tree ------------------------------------------
//
// View tests assert against the tree a component returns rather than a mounted
// DOM, so they need to find nodes in it. One set of walkers for every suite:
// eight files used to carry their own copies, which had already drifted over
// whether a vnode's `text` counted.

/** The parts of a mithril vnode these walkers look at. Structural rather than
 * `m.Vnode` so a children array, a bare string and a component vnode can all be
 * passed to the same walk. */
export interface AnyVnode {
  tag?: unknown;
  attrs?: Record<string, unknown> | null;
  children?: unknown;
  text?: unknown;
}

/** Every vnode in the tree, parents before their children. */
export function collectVnodes(node: unknown, out: AnyVnode[] = []): AnyVnode[] {
  if (node === null || node === undefined || typeof node !== "object")
    return out;
  if (Array.isArray(node)) {
    for (const child of node) collectVnodes(child, out);
    return out;
  }
  const vnode = node as AnyVnode;
  out.push(vnode);
  return collectVnodes(vnode.children, out);
}

/** Every string in the tree, in render order: bare children and vnode `text`
 * alike, since mithril stores a lone string child as either one. */
export function collectText(node: unknown, out: string[] = []): string[] {
  if (node === null || node === undefined || typeof node === "boolean")
    return out;
  if (typeof node === "string") {
    out.push(node);
    return out;
  }
  if (Array.isArray(node)) {
    for (const child of node) collectText(child, out);
    return out;
  }
  const vnode = node as AnyVnode;
  if (typeof vnode.text === "string") out.push(vnode.text);
  return collectText(vnode.children, out);
}

export function attrsOf(vnode: AnyVnode): Record<string, unknown> {
  return vnode.attrs ?? {};
}

/** A vnode's class string. `class` and `className` are both accepted: mithril
 * keeps whichever the caller wrote. */
export function classesOf(vnode: AnyVnode): string {
  const attrs = attrsOf(vnode);
  return String(attrs.className ?? attrs.class ?? "");
}

/** `classesOf` split into tokens, for asserting one class without pinning the
 * order or the rest of the string. */
export function classTokensOf(vnode: AnyVnode): string[] {
  return classesOf(vnode)
    .split(/\s+/)
    .filter((token) => token !== "");
}

/** All the tree's text as one space-joined string, for `toContain` assertions. */
export function allText(node: unknown): string {
  return collectText(node).join(" ");
}

/** Every vnode carrying `name` as an attribute, whatever its value -- the way
 * the views' `data-*` hooks are found. */
export function withAttr(node: unknown, name: string): AnyVnode[] {
  return collectVnodes(node).filter(
    (vnode) => attrsOf(vnode)[name] !== undefined,
  );
}
