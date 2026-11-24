// Shared non-fixture test utilities, explicitly imported by *.test.ts files.
// Deliberately NOT named *.test.ts so vitest does not collect it as a suite.

import m from "mithril";
import { vi } from "vitest";
import { getAppContext, registerAppContext } from "./app-context";
import type {
  UiAccountEntry,
  UiAccountsMessage,
  UiNotificationEntry,
  UiWorkspaceEntry,
  UiWorkspaceUpdate,
  UiWorkspacesMessage,
} from "./channel/messages";
import type { UiDeviceResourcesMessage, UiSharedWorkspace } from "./generated/ui";
import { createEmptyStores } from "./models/boot";
import type { RequestResult } from "./models/errors";
import type {
  BrowserImportModel,
  BrowserImportResult,
} from "./models/browserImport";
import type {
  ConnectionBackendOption,
  CreateFormDefaults,
  CreateRetryPrefill,
} from "./models/create";
import type { UserFacingError } from "./models/errors";
import { createAppQueryClient } from "./models/queryClient";
import type { SettingsOverview } from "./models/settings";
import type {
  ProfileEvent,
  ProfilingInstallOptions,
  ResourceTimingEntry,
} from "./profiling";
import type { SharePanelModelOptions } from "./models/sharePanel";
import type { SharedWorkspaceRow } from "./models/sharedWorkspaces";
import type { WorkspaceOptionsData } from "./models/workspaceOptions";
import { triggerIdFor } from "./views/components/Collapsible";
import { BTN_VARIANTS } from "./views/components/constants";
import { ICONS_16 } from "./views/components/icons";
import { ShellState } from "./views/shell/shell-state";

/** A request helper's result a test expects to have failed, narrowed to its
 * failure so the error helpers can read its body. */
export function failedResult(
  result: RequestResult,
): Extract<RequestResult, { ok: false }> {
  if (result.ok) {
    throw new Error(`expected a failed request, got HTTP ${result.status}`);
  }
  return result;
}

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

/**
 * Render a tree the way a mount would, expanding the components inside it.
 *
 * `renderRoot` calls one view and stops. A component nested in what it returns
 * is left as an opaque vnode, so its markup -- its attributes, and whether it
 * drew anything at all -- is invisible, while the children handed to it still
 * sit in the tree and read as though they were on screen. That is how a shut
 * collapsible's contents can look present to a test and be absent to a reader.
 * Any assertion about what a reader would see has to render through.
 */
export function renderDeep(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(renderDeep);
  if (node === null || node === undefined || typeof node !== "object")
    return node;
  const vnode = node as { tag?: unknown; children?: unknown };
  const view = viewOf(vnode.tag);
  if (view !== null) return renderDeep(view(vnode as m.Vnode));
  if (vnode.children !== undefined)
    return { ...vnode, children: renderDeep(vnode.children) };
  return vnode;
}

/** The view of a component vnode's tag, or null for a plain element. */
function viewOf(tag: unknown): ((vnode: m.Vnode) => m.Children) | null {
  const candidate =
    typeof tag === "function"
      ? (tag as () => unknown)()
      : typeof tag === "object" && tag !== null
        ? tag
        : null;
  const view = (candidate as { view?: unknown } | null)?.view;
  if (typeof view !== "function") return null;
  return (vnode: m.Vnode) =>
    (view as (v: m.Vnode) => m.Children).call(candidate, vnode);
}

/** Every string a rendered tree would put on screen, for the assertions that
 * are about what the reader sees rather than about markup. */
export function renderedText(vnode: m.Vnode | null): string {
  if (vnode === null || vnode === undefined) return "";
  if (typeof vnode === "string" || typeof vnode === "number")
    return String(vnode);
  if (Array.isArray(vnode)) return vnode.map(renderedText).join(" ");
  // Trusted markup (`m.trust`) is an icon's path data, not text anyone reads.
  if ((vnode as unknown as { tag?: unknown }).tag === "<") return "";
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

/** The accounts frame listing the signed-in `accounts` (and `signedOutAccounts`), as a window on `accountId` receives it. */
export function accountsMessage(
  accounts: UiAccountEntry[] = [accountEntry()],
  accountId = "local",
  signedOutAccounts: UiAccountEntry[] = [],
): UiAccountsMessage {
  return {
    type: "accounts",
    account_id: accountId,
    has_accounts: accounts.length > 0,
    accounts,
    signed_out_accounts: signedOutAccounts,
  };
}

/** One signed-in account entry: alice. */
export function accountEntry(
  overrides: Partial<UiAccountEntry> = {},
): UiAccountEntry {
  return {
    user_id: "user-1",
    email: "alice@example.com",
    display_name: "",
    workspace_count: 0,
    ...overrides,
  };
}

/** A second account signed in beside accountEntry's. */
export function secondAccountEntry(): UiAccountEntry {
  return accountEntry({
    user_id: "user-2",
    email: "bob@example.com",
  });
}

/** A third account signed in beside those two, with workspaces on this install. */
export function thirdAccountEntry(): UiAccountEntry {
  return accountEntry({ user_id: "user-3", email: "carol@example.com", workspace_count: 2 });
}

/** The id of `createAttemptEntry`'s row. */
export const CREATE_ATTEMPT_ID = "create-attempt-0123abcd";

/** A create attempt's row in the workspace list: "gamma", still being made, on no host yet. */
export function createAttemptEntry(): UiWorkspaceEntry {
  return {
    ...workspacesMessage().workspaces[0],
    id: CREATE_ATTEMPT_ID,
    host_id: "",
    name: "gamma",
    create_attempt_state: "creating",
  };
}

/** The workspace domain every shared-row fixture's targets are labels under. */
export const SHARED_WORKSPACE_DOMAIN = "k3j9.a1b2.us1.shares.example";

/** One workspace shared with the window's account, as a "Shared with me" row carries it: an invitation
 * with a shell target ("Beta") and one app target ("Notes"), each with its link, and no verdict yet. */
export function sharedWorkspace(
  overrides: Partial<UiSharedWorkspace> = {},
): UiSharedWorkspace {
  return {
    share_key: "agent-" + "9".repeat(32),
    host_id: "host-" + "9".repeat(32),
    workspace_domain: SHARED_WORKSPACE_DOMAIN,
    owner: {
      user_id: "owner-id",
      email: "owner@acme.example",
      display_name: null,
      profile_picture_url: null,
    },
    status: "invited",
    granted_via: "user",
    last_seen_at: null,
    latest_grant_at: "2026-11-04T09:00:00+00:00",
    targets: [
      {
        app: "system_interface",
        display_name: "Beta",
        link: `https://shell-1.${SHARED_WORKSPACE_DOMAIN}/`,
        granted_via: "user",
      },
      {
        app: "notes",
        display_name: "Notes",
        link: `https://notes-1.${SHARED_WORKSPACE_DOMAIN}/`,
        granted_via: "user",
      },
    ],
    verdict: null,
    verdict_at: null,
    is_manual: false,
    ...overrides,
  };
}

/** `sharedWorkspace`'s owner as someone with a display name, "Sam Okafor", which is then what names them. */
export function namedSharedOwner(): UiSharedWorkspace["owner"] {
  return { ...sharedWorkspace().owner, email: "sam@acme.example", display_name: "Sam Okafor" };
}

/** One app target of a shared-row fixture, its link the app's label under the fixtures' domain. */
export function sharedAppTarget(
  app: string,
  displayName: string,
  isGrantedAsApp = false,
): UiSharedWorkspace["targets"][number] {
  return {
    app,
    display_name: displayName,
    link: `https://${app}-1.${SHARED_WORKSPACE_DOMAIN}/`,
    granted_via: "user",
    is_granted_as_app: isGrantedAsApp,
  };
}

/** The workspaces-frame entry of `sharedWorkspace(shareOverrides)`: listed under the account, not owned,
 * keyed by the share key. */
export function sharedWorkspaceEntry(
  shareOverrides: Partial<UiSharedWorkspace> = {},
  entryOverrides: Partial<UiWorkspaceEntry> = {},
): UiWorkspaceEntry {
  const share = sharedWorkspace(shareOverrides);
  return {
    id: share.share_key,
    name: "Beta",
    accent: "#112233",
    host_id: share.host_id,
    is_backend_unreachable: false,
    supports_shutdown: false,
    liveness: "",
    account: "",
    create_attempt_state: "",
    is_remote: false,
    location: "",
    is_owned: false,
    share,
    ...entryOverrides,
  };
}

/** One shared-workspaces page row: an active share of a workspace this device runs. */
export function sharedWorkspaceRow(
  overrides: Partial<SharedWorkspaceRow> = {},
): SharedWorkspaceRow {
  return {
    host_id: "host-" + "a".repeat(32),
    workspace_id: "agent-live",
    account_email: "alice@example.com",
    display_name: "alpha",
    workspace_domain: "abc.owner.us1.shares.example",
    device_label: "This device",
    share_state: "active",
    record_state: "active",
    created_display: "2026-09-01",
    last_seen_display: "Connected now",
    is_likely_leaked: false,
    is_hosted_here: true,
    ...overrides,
  };
}

/** The connection backends as the server tables them: three with a pinned region, Modal placed per create. */
export const CONNECTION_BACKEND_OPTIONS: readonly ConnectionBackendOption[] = [
  {
    name: "aws",
    label: "AWS",
    is_region_pinned: true,
    is_local_profile_supported: true,
  },
  {
    name: "gcp",
    label: "GCP",
    is_region_pinned: true,
    is_local_profile_supported: false,
  },
  {
    name: "azure",
    label: "Azure",
    is_region_pinned: true,
    is_local_profile_supported: true,
  },
  {
    name: "modal",
    label: "Modal",
    is_region_pinned: false,
    is_local_profile_supported: true,
  },
];

/** A create-form defaults payload for a window on accountEntry's account, with Imbue Cloud selected. */
export function createFormDefaults(
  overrides: Partial<CreateFormDefaults> = {},
): CreateFormDefaults {
  return {
    account: {
      user_id: "user-1",
      email: "alice@example.com",
      state: "SIGNED_IN",
      is_connection_owner_eligible: true,
    },
    launch_modes: ["IMBUE_CLOUD", "LIMA", "DOCKER"],
    selected_launch_mode: "IMBUE_CLOUD",
    docker_runtimes: ["RUNC", "RUNSC"],
    selected_docker_runtime: "RUNSC",
    backup_providers: ["IMBUE_CLOUD", "API_KEY", "CONFIGURE_LATER"],
    selected_backup_provider: "IMBUE_CLOUD",
    region_options_by_launch_mode: {
      IMBUE_CLOUD: ["US-EAST-VA", "US-WEST-OR"],
    },
    region_selected_by_launch_mode: { IMBUE_CLOUD: "US-WEST-OR" },
    instance_types_by_backend: {},
    default_instance_type_by_backend: {},
    machine_size_defaults_by_launch_mode: {
      DOCKER: { cpu_count: 4, memory_gb: 8, disk_gb: null },
      LIMA: { cpu_count: 2, memory_gb: 4, disk_gb: 100 },
      MODAL: { cpu_count: 2, memory_gb: 4, disk_gb: null },
      AWS: { cpu_count: null, memory_gb: null, disk_gb: 50 },
    },
    cloud_accounts: [],
    connection_backends: [...CONNECTION_BACKEND_OPTIONS],
    git_url: "https://github.com/imbue-ai/default-workspace-template.git",
    branch: "minds-v9.9.9",
    color: "#0b292b",
    prefill: null,
    local_prerequisites: [],
    local_launch_mode: "DOCKER",
    ...overrides,
  };
}

/** A retry's prefill: the settings of an Imbue Cloud attempt under accountEntry's account. */
export function retryPrefill(overrides: Partial<CreateRetryPrefill> = {}): CreateRetryPrefill {
  return {
    git_url: "https://github.com/acme/widgets.git",
    branch: "main",
    host_name: "widgets",
    launch_mode: "IMBUE_CLOUD",
    docker_runtime: "RUNSC",
    backup_provider: "IMBUE_CLOUD",
    backup_api_key_env: "",
    region: "",
    cloud_account: "",
    instance_type: "",
    cpu_count: null,
    memory_gb: null,
    disk_gb: null,
    color: "#123456",
    unavailable_reason: "",
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
    chat_agent_id: "",
    workspace_agent_id: "agent-aa11",
    workspace_name: "alpha",
    workspace_accent: "#aabbcc",
    service_name: "",
    ...overrides,
  };
}

/** The `/ui/api/settings` payload. */
export function settingsOverview(
  overrides: Partial<SettingsOverview> = {},
): SettingsOverview {
  return {
    is_master_password_set: false,
    report_unexpected_errors: true,
    version: "v-one",
    update_window_start_hour: 2,
    update_window_end_hour: 5,
    is_auto_update_enabled: false,
    auto_update_prompt_decision: null,
    ...overrides,
  };
}

/** A `POST /ui/api/settings/browser-import` outcome: a successful run unless
 * `overrides` say otherwise. */
export function browserImportOutcome(
  overrides: Partial<BrowserImportResult> = {},
): BrowserImportResult {
  return {
    is_success: true,
    message: "",
    detail: "",
    error_code: null,
    is_full_disk_access_needed: false,
    is_chrome_missing: false,
    ...overrides,
  };
}

/** Leave `model` as a run that came back with the failed `outcome` does: the
 * outcome, and the error shown for it, which is returned. */
export function setFailedImport(
  model: BrowserImportModel,
  outcome: BrowserImportResult,
): UserFacingError {
  const error = { message: outcome.message, details: outcome.detail };
  model.outcome = outcome;
  model.error = error;
  return error;
}

/** The `POST /ui/api/settings/browser-import` outcome of an import the OS
 * refused: the Chrome profile is there, but could not be read. On a Mac
 * (the default) the app lacks Full Disk Access; elsewhere it is a plain
 * permission problem. */
export function unreadableChromeProfileOutcome(
  isFullDiskAccessNeeded = true,
): BrowserImportResult {
  return browserImportOutcome({
    is_success: false,
    message:
      "Imbue Studio couldn't import your Chrome cookies. Try again. If this persists, report a bug.",
    detail: "The Google Chrome profile could not be read: EPERM.",
    error_code: "CHROME_PROFILE_NOT_READABLE",
    is_full_disk_access_needed: isFullDiskAccessNeeded,
  });
}

/** The `POST /ui/api/settings/browser-import` outcome of an import that failed
 * for a reason with no explanation of its own. */
export function failedImportOutcome(): BrowserImportResult {
  return browserImportOutcome({
    is_success: false,
    message:
      "Imbue Studio couldn't import your Chrome cookies. Try again. If this persists, report a bug.",
    detail: "Error: The stored browser state is not valid JSON.",
  });
}

/** The `POST /ui/api/settings/browser-import` outcome on a computer with no
 * Chrome to import from. */
export function chromeMissingOutcome(): BrowserImportResult {
  return browserImportOutcome({
    is_success: false,
    message: "Imbue Studio couldn't find Google Chrome's cookies on this computer.",
    detail: "Google Chrome is not installed in any of the standard locations.",
    is_chrome_missing: true,
  });
}

/** The suffix of the share panel's local read, which answers without Imbue Cloud. */
export const SHARING_LOCAL_READ_SUFFIX = "/local";

/**
 * SharePanelModel options for a workspace granted by owner@example.com, with
 * inert redraw/timer hooks and a query client of its own, so no case reads
 * what another cached; tests pass the `fetchJson` stub they want answered.
 * The panel's local read goes unanswered (a 404 the panel ignores) unless
 * `isLocalReadServed`, so a test of the fresh read's document is not handed
 * that document twice.
 */
export function sharePanelOptions(
  overrides: Partial<SharePanelModelOptions> = {},
  { isLocalReadServed = false }: { isLocalReadServed?: boolean } = {},
): SharePanelModelOptions {
  const fetchJson = overrides.fetchJson;
  if (fetchJson !== undefined && !isLocalReadServed)
    overrides = {
      ...overrides,
      fetchJson: (url, init) =>
        url.endsWith(SHARING_LOCAL_READ_SUFFIX)
          ? Promise.resolve({ ok: false, status: 404, body: null })
          : fetchJson(url, init),
    };
  return {
    hostId: "host-" + "a".repeat(32),
    granterEmail: "owner@example.com",
    wholeService: "system_interface",
    appServices: ["web", "docs"],
    serviceLabels: { web: "web-r4nd", system_interface: "shell-r4nd" },
    publicEmailDomains: ["gmail.com", "outlook.com"],
    redraw: () => undefined,
    setTimer: () => 0,
    clearTimer: () => undefined,
    monotonicNowMs: () => 0,
    queryClient: createAppQueryClient(),
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

/** Publish a workspace list holding only `agentId`, at `liveness`, to the registered app context. */
export function publishWorkspaceLiveness(
  agentId: string,
  liveness: UiWorkspacesMessage["workspaces"][number]["liveness"],
): void {
  getAppContext().stores.workspaces.applyWorkspacesMessage(
    workspacesMessage({ workspaces: [{ ...workspacesMessage().workspaces[0], id: agentId, liveness }] }),
  );
}

/** A workspace read as a minor release behind and idle, with backups to fall back on. */
export const OUT_OF_DATE_UPDATE: UiWorkspaceUpdate = {
  availability: "OUT_OF_DATE",
  current_version: "minds-v0.3.9",
  supported_version: "minds-v0.4.1",
  is_workspace_readable: true,
  activity: "IDLE",
  is_backup_configured: true,
};

/** An armed schedule's window fields for a window that opens at `startHour`
 * in the test machine's local time, as the workspace's file carries it (UTC):
 * the UTC start of the window open now, else the next to open, as the app writes it. */
export function scheduleWindowAtLocalHour(
  startHour: number,
  lengthHours: number,
): Pick<UiWorkspaceUpdate, "schedule_window_start_minute_utc" | "schedule_window_length_minutes"> {
  const now = new Date();
  const opening = new Date(now);
  opening.setHours(startHour, 0, 0, 0);
  if (now.getTime() >= opening.getTime() + lengthHours * 60 * 60 * 1000) opening.setDate(opening.getDate() + 1);
  return {
    schedule_window_start_minute_utc: opening.getUTCHours() * 60 + opening.getUTCMinutes(),
    schedule_window_length_minutes: lengthHours * 60,
  };
}

/** Leave `shell`'s updates store holding `updates`, as a `workspace_updates` frame would. */
export function publishUpdates(
  shell: ShellState,
  updates: Record<string, UiWorkspaceUpdate>,
  isAutoUpdateEnabled: boolean,
): void {
  shell.stores.updates.applyUpdatesMessage({
    type: "workspace_updates",
    updates,
    update_window: "2:00 AM-5:00 AM",
    is_auto_update_enabled: isAutoUpdateEnabled,
  });
}

/** A shell registered as the app's context, its updates store holding `updates`
 * as a `workspace_updates` frame would leave it. */
export function updatesShell(
  updates: Record<string, UiWorkspaceUpdate>,
  isAutoUpdateEnabled: boolean,
): ShellState {
  const shell = new ShellState(createEmptyStores());
  registerAppContext({ stores: shell.stores, shell });
  publishUpdates(shell, updates, isAutoUpdateEnabled);
  return shell;
}

/** Leave `shell`'s requests store holding one request waiting on an answer for each of `workspaceAgentIds`, as a
 * `requests` frame would. */
export function publishPendingRequests(shell: ShellState, workspaceAgentIds: readonly string[]): void {
  shell.stores.requests.applyRequestsMessage({
    count: workspaceAgentIds.length,
    request_ids: workspaceAgentIds.map((agentId) => `request-${agentId}`),
    workspace_agent_ids: [...workspaceAgentIds],
  });
}

/** Where a stubbed bar element measures, as `getBoundingClientRect` reports it. */
export interface StubRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** A document (for a suite with no DOM) whose only elements are `rects`' ids, each measuring at its rect, and
 * everything else absent, the way the workspace button is absent on a hub page. The map is read live, so a case
 * can move the bar under something already mounted. Installed with `vi.stubGlobal`. */
export function stubTitlebarRects(rects: Record<string, StubRect>): void {
  vi.stubGlobal("document", {
    getElementById: (id: string) => (id in rects ? { getBoundingClientRect: () => rects[id] } : null),
  });
}

/** Put an element with `id` in the (jsdom) document, measuring at `rect`. */
export function placeMeasuredElement(id: string, rect: StubRect): void {
  const element = document.createElement("div");
  element.id = id;
  element.getBoundingClientRect = () => rect as DOMRect;
  document.body.appendChild(element);
}

/** One workspace's options payload (`GET /ui/api/workspaces/<id>/options`): "alpha", linked to no account, with
 * no apps registered and no SSH command. */
export function workspaceOptionsData(overrides: Partial<WorkspaceOptionsData> = {}): WorkspaceOptionsData {
  return {
    agent_id: "agent-aa11",
    host_id: "host-bb22",
    name: "alpha",
    color: "#aabbcc",
    palette: { blue: "#aabbcc" },
    is_stale: false,
    is_provider_signed_out: false,
    is_account_owned_provider: false,
    leased_owner_email: "",
    has_account: false,
    account_email: "",
    account_display_name: null,
    account_profile_picture_url: null,
    current_account: null,
    accounts: [],
    app_services: [],
    service_labels: {},
    whole_service: "system_interface",
    public_email_domains: [],
    grantable_email_domain: "",
    ssh_command: "",
    ...overrides,
  };
}

/** A `fetch` that answers with `respond` and records every URL it was asked for. */
export function recordingFetch(
  respond: (url: string) => Promise<Response> = () =>
    Promise.resolve(jsonResponse({})),
): { fetchImpl: (url: string) => Promise<Response>; requests: string[] } {
  const requests: string[] = [];
  const fetchImpl = (url: string): Promise<Response> => {
    requests.push(url);
    return respond(url);
  };
  return { fetchImpl, requests };
}

/** What the local app answers an invite or mobile-link request from an unverified account. */
export const UNVERIFIED_REFUSAL = {
  error: "email_not_verified",
  message: "m",
  email: "owner@example.com",
};

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

/** An `EventSource` that never connects or delivers anything, for code that
 * opens a stream the test does not exercise (node has no `EventSource`).
 * Install it with `vi.stubGlobal("EventSource", InertEventSource)`. */
export class InertEventSource {
  onmessage: unknown = null;
  onerror: unknown = null;
  close(): void {}
}

/** Flush pending promise work: a timer turn runs only after the microtask queue
 * is empty, so every await chain the models queued has run by the time it
 * fires -- including `Response.json()`, whose body read takes more microtask
 * hops than a caller can count. */
export async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

/** Mount `view` into a fresh element appended to document.body (jsdom tests
 * that click through the real DOM). */
export function mountInBody(view: () => m.Children): HTMLElement {
  const root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view });
  return root;
}

/** The button under `root` whose text is exactly `label`; throws when there is
 * none. */
export function buttonLabelled(
  root: ParentNode,
  label: string,
): HTMLButtonElement {
  const button = [...root.querySelectorAll("button")].find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  if (button === undefined) throw new Error(`no "${label}" button`);
  return button;
}

/** The labels of every button under `root`, in page order. */
export function buttonLabels(root: ParentNode): string[] {
  return [...root.querySelectorAll("button")].map(
    (button) => button.textContent?.trim() ?? "",
  );
}

/** The labels of the buttons under `root` drawn as primary, in page order. */
export function primaryButtonLabels(root: ParentNode): string[] {
  return [...root.querySelectorAll("button")]
    .filter((button) => button.className.includes(BTN_VARIANTS.primary))
    .map((button) => button.textContent?.trim() ?? "");
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

/** An `installUpdate` stub whose call stays pending, as the main process's
 * does until the app is known to be staying up, until `resolveInstall()`
 * settles it as a success. */
export function pendingInstallUpdate(): {
  installUpdate: () => Promise<{ error: null }>;
  resolveInstall: () => void;
} {
  let resolveInstall: () => void = () => {};
  return {
    installUpdate: () =>
      new Promise<{ error: null }>((resolve) => {
        resolveInstall = () => resolve({ error: null });
      }),
    resolveInstall: () => resolveInstall(),
  };
}

export interface RecordedMark {
  name: string;
  detail: unknown;
}

export interface RecordedMeasure {
  name: string;
  start: string;
  end: string;
  detail: unknown;
}

export interface RecordedFetchCall {
  input: RequestInfo | URL;
  init: RequestInit | undefined;
}

/** Everything `installProfiling` reaches into, as fakes a test drives by hand. */
export interface FakeProfilingEnvironment {
  options: ProfilingInstallOptions;
  /** The profiler's clock, in `performance.now()` milliseconds. */
  clock: { nowMs: number };
  timeOriginMs: number;
  origin: string;
  marks: RecordedMark[];
  measures: RecordedMeasure[];
  /** The object whose `fetch` the profiler wraps; call `fetchHost.fetch` to go through the wrapper. */
  fetchHost: { fetch: typeof fetch };
  /** Every call that reached the fetch the wrapper replaced, profile posts included. */
  fetchCalls: RecordedFetchCall[];
  /** What that fetch answers; reassign it to make requests fail. */
  respond: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  beacons: { url: string; body: string }[];
  /** The delays of every flush timer scheduled so far. */
  timerDelays: number[];
  /** Run the pending flush timer, as the second elapsing would. */
  fireTimer(): void;
  deliverResourceEntries(entries: ResourceTimingEntry[]): void;
  firePageHide(): void;
}

/** A profiling environment whose clock, timers, fetch, resource timing, page
 * hide, beacon, and random source are all fakes the test drives. The random
 * bytes count upward from one, so no id is ever all zeros by accident. */
export function fakeProfilingEnvironment(
  overrides: Partial<ProfilingInstallOptions> = {},
): FakeProfilingEnvironment {
  const clock = { nowMs: 0 };
  const marks: RecordedMark[] = [];
  const measures: RecordedMeasure[] = [];
  const fetchCalls: RecordedFetchCall[] = [];
  const beacons: { url: string; body: string }[] = [];
  const timerDelays: number[] = [];
  const pendingTimers: { id: number; callback: () => void }[] = [];
  let timerCount = 0;
  let randomCounter = 0;
  let onEntries: ((entries: ResourceTimingEntry[]) => void) | null = null;
  let onPageHide: (() => void) | null = null;
  const environment: FakeProfilingEnvironment = {
    clock,
    timeOriginMs: 1_700_000_000_000,
    origin: "http://127.0.0.1:5000",
    marks,
    measures,
    fetchHost: {
      fetch: (input: RequestInfo | URL, init?: RequestInit) => {
        fetchCalls.push({ input, init });
        return environment.respond(input, init);
      },
    },
    fetchCalls,
    respond: () => Promise.resolve(jsonResponse({})),
    beacons,
    timerDelays,
    fireTimer: () => {
      const timer = pendingTimers.shift();
      if (timer === undefined) throw new Error("No flush timer is pending.");
      timer.callback();
    },
    deliverResourceEntries: (entries) => {
      if (onEntries === null)
        throw new Error("Nothing observes resource timing.");
      onEntries(entries);
    },
    firePageHide: () => {
      if (onPageHide === null) throw new Error("Nothing listens for pagehide.");
      onPageHide();
    },
    options: {},
  };
  environment.options = {
    fetchHost: environment.fetchHost,
    performance: {
      timeOrigin: environment.timeOriginMs,
      now: () => clock.nowMs,
      mark: (name, options) => {
        marks.push({ name, detail: options?.detail });
      },
      measure: (name, options) => {
        measures.push({
          name,
          start: options.start,
          end: options.end,
          detail: options.detail,
        });
      },
      clearMarks: (name) => {
        const idx = marks.findIndex((mark) => mark.name === name);
        if (idx !== -1) marks.splice(idx, 1);
      },
    },
    observeResources: (callback) => {
      onEntries = callback;
      return () => {
        onEntries = null;
      };
    },
    setTimer: (callback, delayMs) => {
      const id = ++timerCount;
      timerDelays.push(delayMs);
      pendingTimers.push({ id, callback });
      return id;
    },
    clearTimer: (timerId) => {
      const idx = pendingTimers.findIndex((timer) => timer.id === timerId);
      if (idx !== -1) pendingTimers.splice(idx, 1);
    },
    onPageHide: (handler) => {
      onPageHide = handler;
      return () => {
        onPageHide = null;
      };
    },
    sendBeacon: (url, body) => {
      beacons.push({ url, body });
      return true;
    },
    randomBytes: (byteCount) => {
      const bytes = new Uint8Array(byteCount);
      for (let idx = 0; idx < byteCount; idx++)
        bytes[idx] = (randomCounter++ % 255) + 1;
      return bytes;
    },
    origin: environment.origin,
    rendererPid: 4242,
    ...overrides,
  };
  return environment;
}

/** The events every batch posted to the profile endpoint carried, in order,
 * whether by fetch or by beacon. */
export function postedProfileEvents(
  environment: FakeProfilingEnvironment,
): ProfileEvent[] {
  const bodies = [
    ...environment.fetchCalls
      .filter((call) => String(call.input) === "/ui/api/profile/events")
      .map((call) => call.init?.body as string),
    ...environment.beacons.map((beacon) => beacon.body),
  ];
  return bodies.flatMap(
    (body) => (JSON.parse(body) as { events: ProfileEvent[] }).events,
  );
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
  }
}

// Walking a rendered vnode tree
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
 * alike, since mithril stores a lone string child as either one. Trusted
 * markup (`m.trust`, which every icon's artwork arrives as) is skipped: its
 * child is a blob of SVG path data, not text anyone reads. */
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
  if (vnode.tag === "<") return out;
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

/** The vnode with this `id`, from a tree or an already collected list. */
export function byId(node: unknown, id: string): AnyVnode | undefined {
  return collectVnodes(node).find((vnode) => attrsOf(vnode).id === id);
}

/** A vnode the test expects to find; the assertion names the gap when it is missing. */
export function required<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("expected a rendered vnode");
  return value;
}

/** The trigger of the `Collapsible` with this id, in a tree rendered through
 * (`renderDeep`): its `aria-expanded` is what says whether the panel is open. */
export function collapsibleTrigger(node: unknown, id: string): AnyVnode {
  return required(byId(node, triggerIdFor(id)));
}

/** Press that trigger, as a click on it would. */
export function pressCollapsible(node: unknown, id: string): void {
  (attrsOf(collapsibleTrigger(node, id)).onclick as () => void)();
}

/** A sessionStorage for the suite (the vitest environment has none): a map behind the Storage methods the
 * models use, installed with `vi.stubGlobal`, so `vi.unstubAllGlobals` removes it. */
export function stubSessionStorage(): Map<string, string> {
  const store = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => store.clear(),
  });
  return store;
}

/** Every error mark (`errorMark`) in a rendered tree, found by the alert glyph
 * it trusts in. Render with `renderDeep` first: a mark drawn inside a component
 * is invisible until that component's view has run. */
export function errorMarks(node: unknown): AnyVnode[] {
  return collectVnodes(node).filter(
    (vnode) => vnode.tag === "<" && vnode.children === ICONS_16["alert-filled"],
  );
}

const GIB_BYTES = 1024 ** 3;

type DeviceResourceWireView = NonNullable<UiDeviceResourcesMessage["view"]>;

/** This computer's resource readings and warning levels as the backend sends them: calm, the disk warning at 10 GB. */
export function deviceResourcesWireView(
  overrides: Partial<DeviceResourceWireView> = {},
): DeviceResourceWireView {
  return {
    settings: { disk_warning_free_gb: 10, memory_warning_percent: null, cpu_warning_percent: null },
    readings: {
      cpu: { percent: 25, recent_percent: 20, cores: 8 },
      memory: { used_bytes: 12 * GIB_BYTES, free_bytes: 4 * GIB_BYTES, total_bytes: 16 * GIB_BYTES },
      disk: { used_bytes: 400 * GIB_BYTES, free_bytes: 100 * GIB_BYTES, total_bytes: 500 * GIB_BYTES, seconds_to_full: null },
    },
    alerts: [],
    ...overrides,
  };
}
