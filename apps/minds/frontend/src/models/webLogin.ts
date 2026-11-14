// The browser sign-in flow: the backend launches `mngr imbue_cloud auth
// login` (which opens the hosted accounts page in the system browser and
// receives the session on a localhost loopback); this model starts that flow
// and polls its status so the WebLoginModal can narrate the wait, offer the
// copy-the-link fallback, and surface errors. Dismissing the modal only hides
// it -- the subprocess keeps listening until its own timeout, and a sign-in
// that still completes simply shows up via the accounts channel. Starting
// again while it is still listening rejoins that flow (opening its sign-in
// page in the browser again) rather than racing a second one.
//
// The poll is also what hands focus back: signing in happens in the system
// browser, so the app has to raise itself once the flow lands (see raiseApp).

import m from "mithril";
import { electronBridge } from "../electron-bridge";
import type { UserFacingError } from "./errors";
import { TextResponseBody, bodyFromText, fallbackErrorSentence, responseDetails } from "./errors";

export type WebLoginState = "idle" | "starting" | "waiting" | "finishing" | "done" | "error";

interface FlowStatusBody {
  state?: string;
  login_url?: string | null;
  user_id?: string | null;
  email?: string | null;
  error?: string | null;
  error_details?: string | null;
  is_retry_offered?: boolean;
}

/** The request that starts the sign-in reached no answer the page can use. */
const SIGN_IN_NOT_STARTED = fallbackErrorSentence(
  "Imbue Studio couldn't start the sign-in",
);

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
  // The account the flow signed in, once the poll has seen it land. A page
  // waiting on this sign-in takes the user id up: the accounts frame that
  // lists the account can arrive after the poll reads "done".
  userId = "";
  email = "";
  error: UserFacingError | null = null;
  /** Signing in again can help; it can't when the sign-in went through but couldn't be saved here. */
  isRetryOffered = true;
  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;
  private readonly bringAppToFront: () => void;
  private readonly openInBrowser: (url: string) => void;
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
    // The Electron shell routes a new-window request for an external URL to the system browser.
    openInBrowser: (url: string) => void = (url) => void window.open(url, "_blank", "noopener"),
  ) {
    this.fetchImpl = fetchImpl;
    this.redraw = redraw;
    this.bringAppToFront = bringAppToFront;
    this.openInBrowser = openInBrowser;
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
    this.error = null;
    this.isRetryOffered = true;
    this.userId = "";
    this.email = "";
    this.hasRaisedApp = false;
    const generation = ++this.generation;
    this.redraw();
    let status = 0;
    let text: string;
    try {
      const response = await this.fetchImpl("/auth/api/web-login/start", { method: "POST", credentials: "same-origin" });
      status = response.status;
      text = await response.text();
    } catch (thrown) {
      text = thrown instanceof Error ? thrown.message : String(thrown);
    }
    // The user may have dismissed (or restarted) the flow while the start
    // request was in flight; a superseded continuation must not reopen the
    // modal or clobber the newer flow's state.
    if (generation !== this.generation) return;
    const read = bodyFromText(text);
    const body =
      read !== null && typeof read === "object" && !(read instanceof TextResponseBody) ? (read as StartBody) : null;
    if (body === null || status < 200 || status >= 300 || !body.flow_id) {
      // No answer, one that is not ours (a crash's plain-text 500), or the
      // route's own refusal, whose text is for See details: none names a
      // cause the user can act on.
      this.state = "error";
      this.error = {
        message: SIGN_IN_NOT_STARTED,
        details: status === 0 ? `No response. ${text}` : responseDetails({ status, body: read }),
      };
      this.redraw();
      return;
    }
    this.activeFlowId = body.flow_id;
    this.state = "waiting";
    // Joining a sign-in still waiting from earlier: its browser tab may be long
    // gone, so open the page again rather than leave the user to find it.
    if (body.is_already_running && body.login_url) {
      this.loginUrl = body.login_url;
      this.openInBrowser(body.login_url);
    }
    this.schedulePoll();
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
        this.error = { message: "The sign-in flow expired. Please try again.", details: "" };
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
    this.userId = body.user_id ?? this.userId;
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
      this.isRetryOffered = body.is_retry_offered !== false;
      this.error = {
        message: body.error || "Sign-in failed. Please try again.",
        details: body.error_details ?? "",
      };
    } else {
      this.state = body.state === "finishing" ? "finishing" : "waiting";
      if (this.state === "finishing") this.raiseApp();
      this.schedulePoll();
    }
    this.redraw();
  }
}

interface StartBody {
  flow_id?: string;
  is_already_running?: boolean;
  login_url?: string | null;
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
