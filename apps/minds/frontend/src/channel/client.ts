// The single WebSocket channel: connect, dispatch frames to stores, backoff
// reconnect, schema-mismatch reload, and client_state registration.
//
// Reconnect IS resync: the server replays hello + a full snapshot on every
// connection, so the client never patches gaps -- it just reapplies state.
// One channel per window; this replaces the legacy /_chrome/events SSE and
// the Electron main process's SSE relay.

import m from "mithril";
import type {
  UiHealthMessage,
  UiNotificationsMessage,
  UiOpenHelpMessage,
  UiServerMessage,
  UiWorkspaceRefreshMessage,
  UiWorkspaceStoppedMessage,
} from "./messages";
import { parseServerMessage } from "./messages";
import type { AppStores } from "../models/boot";
import { VISIBLE_AFTER_FAILURES, backoffDelayMs } from "./backoff";
import { resolveWindowFocus } from "../window-focus";

const SCHEMA_RELOAD_GUARD_KEY = "minds-ui-schema-reloaded";

export interface ChannelSocketLike {
  send(data: string): void;
  close(): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export interface ChannelOptions {
  stores: AppStores;
  /** Null disables the schema-mismatch reload (no bootstrap to compare against). */
  expectedSchemaVersion: number | null;
  /** Injected in tests; defaults to a real WebSocket at /ui/ws. */
  createSocket?: () => ChannelSocketLike;
  /** Injected in tests; defaults to location.reload. */
  reloadPage?: () => void;
  /** Called on one-shot messages the shell must act on. */
  onWorkspaceStopped?: (message: UiWorkspaceStoppedMessage) => void;
  onOpenHelp?: (message: UiOpenHelpMessage) => void;
  onWorkspaceRefresh?: (message: UiWorkspaceRefreshMessage) => void;
  /** Called after each health message lands. The message's own ``is_snapshot``
   * tells a connect-time replay of current state apart from a live edge. */
  onHealthChanged?: (message: UiHealthMessage) => void;
  /** Called after each notifications message lands (the store already holds
   * it). Same ``is_snapshot`` convention as health: the arrival controller
   * uses it to seed silently on connect-time replays. */
  onNotificationsChanged?: (message: UiNotificationsMessage) => void;
  /** A fresh snapshot is about to replay: the per-workspace health store has
   * just been cleared and everything after this is the server restating the
   * world. */
  onSnapshotStart?: () => void;
  /** Relays state messages to the Electron main process (window bookkeeping);
   * called for workspaces/health/workspace_stopped/open_help. */
  relayShellEvent?: (message: UiServerMessage) => void;
  /** Deterministic jitter for tests; defaults to Math.random. */
  jitter01?: () => number;
  /** Injected in tests; defaults to m.redraw. */
  redraw?: () => void;
  /** Injected in tests; defaults to window.sessionStorage. */
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  /** Injected in tests; defaults to document.hasFocus(). */
  hasWindowFocus?: () => boolean;
}

export function defaultChannelSocketFactory(): ChannelSocketLike {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(
    `${scheme}://${location.host}/ui/ws`,
  ) as unknown as ChannelSocketLike;
}

export class UiChannelClient {
  readonly clientId: string;
  isConnected = false;
  consecutiveFailures = 0;

  private readonly options: ChannelOptions;
  private socket: ChannelSocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private isStopped = false;
  private currentRoute = "/";
  private currentWorkspaceAgentId: string | null = null;

  constructor(options: ChannelOptions) {
    this.options = options;
    this.clientId = crypto.randomUUID().replaceAll("-", "");
  }

  private redraw(): void {
    (this.options.redraw ?? m.redraw)();
  }

  private storage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
    return this.options.storage ?? sessionStorage;
  }

  private hasFocus(): boolean {
    return resolveWindowFocus(this.options.hasWindowFocus);
  }

  start(): void {
    this.connect();
  }

  stop(): void {
    this.isStopped = true;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.socket?.close();
  }

  /** True once enough consecutive failures accrued to surface the indicator. */
  get isVisiblyReconnecting(): boolean {
    return (
      !this.isConnected && this.consecutiveFailures >= VISIBLE_AFTER_FAILURES
    );
  }

  setClientState(route: string, workspaceAgentId: string | null): void {
    // Called from the route resolver's render, i.e. on EVERY redraw; only an
    // actual change warrants a frame. Reconnect registration is unaffected:
    // the onopen path calls sendClientState directly.
    if (
      route === this.currentRoute &&
      workspaceAgentId === this.currentWorkspaceAgentId
    )
      return;
    this.currentRoute = route;
    this.currentWorkspaceAgentId = workspaceAgentId;
    this.sendClientState();
  }

  /** Re-registers client_state on a bare focus/blur (route and workspace
   * unchanged, so setClientState's own dedup would otherwise never resend):
   * the server's OS-dispatch gate needs this window's current focus, not just
   * what it is displaying, and there is no other route/workspace change to
   * piggyback the frame on when the reader just alt-tabs away and back. */
  notifyFocusChanged(): void {
    this.sendClientState();
  }

  private connect(): void {
    const factory = this.options.createSocket ?? defaultChannelSocketFactory;
    const socket = factory();
    this.socket = socket;
    socket.onopen = () => {
      this.isConnected = true;
      this.consecutiveFailures = 0;
      this.sendClientState();
      this.redraw();
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      const message = parseServerMessage(event.data);
      if (message !== null) this.dispatch(message);
    };
    socket.onerror = () => {
      // onclose always follows; the failure accounting happens there.
    };
    socket.onclose = () => {
      this.isConnected = false;
      if (this.isStopped) return;
      this.consecutiveFailures += 1;
      const jitter = this.options.jitter01 ?? Math.random;
      // backoffDelayMs caps the base itself; the jitter deliberately spreads
      // around the cap, so no further clamp here (re-clamping would collapse
      // half of the steady-state draws to exactly the cap).
      const delayMs = backoffDelayMs(this.consecutiveFailures, jitter());
      this.reconnectTimer = setTimeout(() => this.connect(), delayMs);
      this.redraw();
    };
  }

  private sendClientState(): void {
    if (!this.isConnected || this.socket === null) return;
    this.socket.send(
      JSON.stringify({
        type: "client_state",
        client_id: this.clientId,
        route: this.currentRoute,
        workspace_agent_id: this.currentWorkspaceAgentId,
        has_focus: this.hasFocus(),
      }),
    );
  }

  private dispatch(message: UiServerMessage): void {
    const stores = this.options.stores;
    // The Electron main process keeps minimal window bookkeeping (which
    // window shows which workspace, dedup of one-shots) fed by this relay.
    switch (message.type) {
      case "workspaces":
      case "health":
      case "workspace_stopped":
      case "open_help":
        this.options.relayShellEvent?.(message);
        break;
      default:
        break;
    }
    switch (message.type) {
      case "hello":
        // Hello is the first frame of every (re)connect snapshot, and the
        // snapshot only carries non-HEALTHY agents: clear the per-workspace
        // health so agents that recovered while disconnected come back clean.
        stores.health.reset();
        this.options.onSnapshotStart?.();
        this.handleHello(message.schema_version);
        break;
      case "workspaces":
        stores.workspaces.applyWorkspacesMessage(message);
        break;
      case "accounts":
        stores.accounts.applyAccountsMessage(message);
        break;
      case "providers":
        stores.providers.applyProvidersMessage(message);
        break;
      case "requests":
        stores.requests.applyRequestsMessage(message);
        break;
      case "notifications":
        stores.notifications.applyNotificationsMessage(message);
        this.options.onNotificationsChanged?.(message);
        break;
      case "health":
        stores.health.applyHealthMessage(message);
        this.options.onHealthChanged?.(message);
        break;
      case "discovery_health":
        stores.health.applyDiscoveryHealthMessage(message);
        break;
      case "environment":
        stores.health.applyEnvironmentMessage(message);
        break;
      case "workspace_stopped":
        this.options.onWorkspaceStopped?.(message);
        break;
      case "open_help":
        this.options.onOpenHelp?.(message);
        break;
      case "workspace_refresh":
        this.options.onWorkspaceRefresh?.(message);
        break;
      case "reload_ui":
        (this.options.reloadPage ?? (() => location.reload()))();
        break;
      default: {
        const unreachable: never = message;
        void unreachable;
      }
    }
    this.redraw();
  }

  private handleHello(serverSchemaVersion: number): void {
    // No bootstrap was inlined, so there is no version to compare against;
    // reloading would serve the same assets again.
    if (this.options.expectedSchemaVersion === null) return;
    if (serverSchemaVersion === this.options.expectedSchemaVersion) {
      this.storage().removeItem(SCHEMA_RELOAD_GUARD_KEY);
      return;
    }
    // The served assets and the running server disagree; a single hard
    // reload picks up matching assets. The sessionStorage latch prevents a
    // reload loop if the mismatch persists (e.g. cached index).
    if (this.storage().getItem(SCHEMA_RELOAD_GUARD_KEY) === "1") return;
    this.storage().setItem(SCHEMA_RELOAD_GUARD_KEY, "1");
    (this.options.reloadPage ?? (() => location.reload()))();
  }
}

// Test-only export: the storage key the mismatch latch uses.
export const SCHEMA_RELOAD_GUARD_KEY_FOR_TESTS = SCHEMA_RELOAD_GUARD_KEY;
