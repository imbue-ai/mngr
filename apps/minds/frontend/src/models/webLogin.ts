// The browser sign-in flow: the backend launches `mngr imbue_cloud auth
// login` (which opens the hosted accounts page in the system browser and
// receives the session on a localhost loopback); this model starts that flow
// and polls its status so the WebLoginModal can narrate the wait, offer the
// copy-the-link fallback, and surface errors. Dismissing the modal only hides
// it -- the subprocess keeps listening until its own timeout, and a sign-in
// that still completes simply shows up via the accounts channel.
//
// The poll is also what hands focus back: signing in happens in the system
// browser, so the app has to raise itself once the flow lands (see raiseApp).

import m from "mithril";
import { electronBridge } from "../electron-bridge";

export type WebLoginState = "idle" | "starting" | "waiting" | "finishing" | "done" | "error";

interface FlowStatusBody {
  state?: string;
  login_url?: string | null;
  email?: string | null;
  error?: string | null;
}

export const POLL_INTERVAL_MS = 1000;

type FetchLike = typeof fetch;

export interface WebLoginStartOptions {
  // Close the modal when the sign-in lands instead of showing "You're signed
  // in": for a page that shows the new account itself (Manage Accounts).
  isClosedOnSignIn?: boolean;
}

export class WebLoginModel {
  state: WebLoginState = "idle";
  // Why the user is being asked to sign in (e.g. the Electron shell's
  // auth_required message); shown above the wait copy.
  message = "";
  loginUrl = "";
  email = "";
  error = "";
  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;
  private readonly bringAppToFront: () => void;
  private activeFlowId = "";
  private isClosedOnSignIn = false;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  // Bumped by dismiss() (and each start()) so a start() continuation that
  // resolves after the user cancelled can detect it has been superseded and
  // must not reopen the modal.
  private generation = 0;
  // Whether this flow has already asked for the raise (see raiseApp).
  private hasRaisedApp = false;

  constructor(
    // A plain-call wrapper: passing the global `fetch` itself would invoke it
    // with the model as its receiver ("Illegal invocation" in browsers).
    fetchImpl: FetchLike = (input, init) => fetch(input, init),
    redraw: () => void = m.redraw,
    bringAppToFront: () => void = () => electronBridge.bringAppToFront(),
  ) {
    this.fetchImpl = fetchImpl;
    this.redraw = redraw;
    this.bringAppToFront = bringAppToFront;
  }

  get isOpen(): boolean {
    return this.state !== "idle";
  }

  /** Start (or surface the already-running) browser sign-in flow. */
  async start(message = "", options: WebLoginStartOptions = {}): Promise<void> {
    this.message = message;
    if (this.state === "starting" || this.state === "waiting" || this.state === "finishing") {
      // Already in flight: just make sure the modal is visible.
      this.redraw();
      return;
    }
    this.state = "starting";
    this.isClosedOnSignIn = options.isClosedOnSignIn ?? false;
    this.loginUrl = "";
    this.error = "";
    this.email = "";
    this.hasRaisedApp = false;
    const generation = ++this.generation;
    this.redraw();
    try {
      const response = await this.fetchImpl("/auth/api/web-login/start", { method: "POST", credentials: "same-origin" });
      const body = (await response.json()) as { flow_id?: string; error?: string };
      // The user may have dismissed (or restarted) the flow while the start
      // request was in flight; a superseded continuation must not reopen the
      // modal or clobber the newer flow's state.
      if (generation !== this.generation) return;
      if (!response.ok || !body.flow_id) {
        this.state = "error";
        this.error = body.error || "Could not start the sign-in flow.";
        this.redraw();
        return;
      }
      this.activeFlowId = body.flow_id;
      this.state = "waiting";
      this.schedulePoll();
    } catch {
      if (generation !== this.generation) return;
      this.state = "error";
      this.error = "Could not reach the app backend. Please try again.";
    }
    this.redraw();
  }

  /** Start the flow again after an error, as it was first started. */
  async retry(): Promise<void> {
    await this.start(this.message, { isClosedOnSignIn: this.isClosedOnSignIn });
  }

  /** Hide the modal. The plugin subprocess (if still waiting) keeps running. */
  dismiss(): void {
    this.generation += 1;
    this.state = "idle";
    this.activeFlowId = "";
    if (this.pollTimer !== null) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    this.redraw();
  }

  /** Bring the app to the front, once, as soon as this flow's sign-in lands.
   *
   * Sign-in happens in the system browser, which took OS focus with it;
   * without this the user is left looking at the browser's "you're in" page
   * with no idea the app behind it has already moved on. Once per flow,
   * because the poller runs every second and raising on every tick would
   * fight the user for focus. Fired on "finishing", and on "done" as well
   * for a mirror fast enough that no poll ever observed "finishing".
   */
  private raiseApp(): void {
    if (this.hasRaisedApp) return;
    this.hasRaisedApp = true;
    this.bringAppToFront();
  }

  /** A flow this model started has landed, as observed by something other
   * than the poll.
   *
   * The accounts channel frame beats the 1s poll whenever the account is
   * already known locally -- re-picking a recently used account in the
   * browser -- and the surface that sees it settles the flow and dismisses
   * it, which stops the poller before it ever reads "finishing". Without
   * this the raise is lost exactly in the case where the sign-in was
   * fastest. Idempotent with the poll's own raise, and a no-op once the flow
   * is idle or failed, so an unrelated account change never pulls focus.
   */
  noteSignedIn(): void {
    if (this.state === "idle" || this.state === "error") return;
    this.raiseApp();
  }

  private schedulePoll(): void {
    if (this.pollTimer !== null) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => {
      void this.poll();
    }, POLL_INTERVAL_MS);
  }

  private async poll(): Promise<void> {
    if (this.state !== "waiting" && this.state !== "finishing") return;
    const flowId = this.activeFlowId;
    if (!flowId) return;
    let body: FlowStatusBody;
    try {
      const response = await this.fetchImpl(`/auth/api/web-login/status/${encodeURIComponent(flowId)}`, {
        credentials: "same-origin",
      });
      // A response for a dismissed or restarted flow must not touch the
      // model (e.g. a stale flow's 404 would flip the current modal into the
      // error state).
      if (flowId !== this.activeFlowId) return;
      if (response.status === 404) {
        this.state = "error";
        this.error = "The sign-in flow expired. Please try again.";
        this.redraw();
        return;
      }
      body = (await response.json()) as FlowStatusBody;
    } catch {
      if (flowId !== this.activeFlowId) return;
      // Transient poll failure: keep waiting.
      this.schedulePoll();
      return;
    }
    if (flowId !== this.activeFlowId) return;
    this.loginUrl = body.login_url ?? this.loginUrl;
    this.email = body.email ?? this.email;
    if (body.state === "done") {
      this.raiseApp();
      if (this.isClosedOnSignIn) {
        this.dismiss();
        return;
      }
      this.state = "done";
    } else if (body.state === "error") {
      this.state = "error";
      this.error = body.error || "Sign-in failed. Please try again.";
    } else {
      this.state = body.state === "finishing" ? "finishing" : "waiting";
      if (this.state === "finishing") this.raiseApp();
      this.schedulePoll();
    }
    this.redraw();
  }
}

// One shared flow for the whole window: every entry point (the start flow,
// accounts page, create flow, the Electron auth_required nudge) drives this
// instance, and the Shell renders its modal.
export const webLogin = new WebLoginModel();

/** Consume the shell's ``web-login=1`` / ``web-login-message`` params.
 *
 * The Electron shell asks a window to start the browser sign-in by handing it
 * a URL carrying these params -- as a full page load (consumed by the boot
 * code in index.ts) or as a shell-navigate IPC into a live SPA (consumed by
 * navigateExternalUrl). Both must strip the params from the URL they act on:
 * a leftover ``web-login=1`` would spuriously restart the flow on the
 * window's next full reload.
 *
 * Deletes the params from ``params`` in place; returns the (possibly empty)
 * message when the sign-in was requested, or null when it was not.
 */
export function consumeWebLoginParams(params: URLSearchParams): string | null {
  if (params.get("web-login") !== "1") return null;
  const message = params.get("web-login-message") ?? "";
  params.delete("web-login");
  params.delete("web-login-message");
  return message;
}
