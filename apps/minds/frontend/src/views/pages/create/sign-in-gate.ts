// The Imbue Cloud sign-in gate the create form and the template stepper
// share: Imbue Cloud was asked for in a window whose account is not signed in.
// The form already says what to create, so the sign-in only supplies the
// account; once one is in hand (signed in here already, or landed by the
// browser sign-in) the gate takes it up into the window, reloads the form
// under it, and has the page create on its own.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { fetchCreateFormDefaults } from "../../../models/create";
import type { WebLoginState } from "../../../models/webLogin";
import { webLogin } from "../../../models/webLogin";
import type { CreateFormModel } from "./form-model";

/** What the browser sign-in the gate opens says. */
export const IMBUE_CLOUD_SIGN_IN_MESSAGE =
  "Sign in or create an Imbue account to run your workspace on Imbue Cloud. " +
  "You can also cancel and run it directly on your computer.";

/** The form's compute selection did not survive the account that landed. */
export const SIGNED_IN_COMPUTE_RESET_MESSAGE =
  "Signed in, but the selected compute is not available under your account, so it was reset. " +
  "Review the form and press Create.";

/** The form's defaults could not be reloaded under the account that landed. */
export const SIGNED_IN_ACCOUNT_LOAD_FAILED_MESSAGE =
  "Signed in, but could not load your account. Press Create to try again.";

/**
 * Where a sign-in the form is waiting on stands. Either signal settles it: the
 * flow's own verdict, or an account landing on the accounts channel (which can
 * beat the poll). Closing the modal without signing in abandons it, and with
 * it the create; pressing Create asks again.
 */
export function signInOutcome(
  webLoginState: WebLoginState,
  isSignedIn: boolean,
): "signed-in" | "abandoned" | "pending" {
  if (webLoginState === "done" || isSignedIn) return "signed-in";
  if (webLoginState === "idle") return "abandoned";
  return "pending";
}

/** The page the gate creates for. */
export interface SignInGateHost {
  /** The form describing the create; reloaded under the account taken up. */
  model: CreateFormModel;
  /** Whether the page is gone, so nothing lands on it. */
  isRemoved: () => boolean;
  /** Post the create: the window is the account's and the form is reloaded under it. */
  create: () => void;
  /** Where the page shows why the create did not go out ("" clears it). */
  setSubmitError: (message: string) => void;
}

export class ImbueCloudSignInGate {
  // The accounts signed in when a sign-in was asked for, so the one it adds can be told apart.
  private signedInAccountIdsBeforeSignIn = new Set<string>();
  private isAwaitingSignIn = false;
  // The account the browser sign-in landed, as its poll named it; null while
  // no sign-in this gate asked for has landed through the poll.
  private landedUserId: string | null = null;

  constructor(private readonly host: SignInGateHost) {}

  /**
   * Create was pressed needing an account: go ahead on the one in hand, else
   * ask for a sign-in. In hand means the window's own account is signed in, or
   * the window is the device's and some account is signed in here (the form's
   * defaults can predate an account that landed after its sign-in modal was
   * closed; a new sign-in would only open the browser again). A signed-out
   * account's window is asked to sign in again instead: handing it whichever
   * other account is signed in would move the window off its account unasked.
   */
  start(): void {
    const { shell, stores } = getAppContext();
    this.landedUserId = null;
    this.signedInAccountIdsBeforeSignIn = new Set(
      stores.accounts.accounts.map((account) => account.user_id),
    );
    if (stores.accounts.hasAccounts && !shell.isAccountSignedOut()) {
      this.createUnderAccountInHand();
      return;
    }
    this.isAwaitingSignIn = true;
    void webLogin.start(IMBUE_CLOUD_SIGN_IN_MESSAGE);
  }

  /** Whether an account that was not signed in when the sign-in was asked for is listed now. */
  private hasNewAccountLanded(): boolean {
    return getAppContext().stores.accounts.accounts.some(
      (account) => !this.signedInAccountIdsBeforeSignIn.has(account.user_id),
    );
  }

  /** Settle a sign-in being waited on; the page calls this on every update. */
  sync(): void {
    if (!this.isAwaitingSignIn) return;
    switch (signInOutcome(webLogin.state, this.hasNewAccountLanded())) {
      case "signed-in":
        this.isAwaitingSignIn = false;
        // Read before the flow is dismissed: the poll can report the landing
        // before the accounts frame lists the account, and the id is what the
        // window takes up either way.
        this.landedUserId = webLogin.userId || null;
        webLogin.noteSignedIn();
        webLogin.dismiss();
        this.createUnderAccountInHand();
        return;
      case "abandoned":
        this.isAwaitingSignIn = false;
        return;
      case "pending":
        return;
    }
  }

  /**
   * The account the window takes up once one is in hand: the one the sign-in's
   * poll named, else (the accounts frame having beaten the poll) the one that
   * was not signed in when the sign-in was asked for, else the window's own
   * when it is signed in, else the first signed-in account (the device's
   * window going ahead on an account already here, or a sign-in that
   * re-picked one).
   */
  landedAccountId(): string | null {
    const { shell, stores } = getAppContext();
    if (this.landedUserId !== null) return this.landedUserId;
    const landed = stores.accounts.accounts.find(
      (account) => !this.signedInAccountIdsBeforeSignIn.has(account.user_id),
    );
    if (landed !== undefined) return landed.user_id;
    if (stores.accounts.isSignedIn(shell.accountId)) return shell.accountId;
    return stores.accounts.accounts[0]?.user_id ?? null;
  }

  /**
   * An account is in hand: the window takes it up, the form is reloaded under
   * it (its connections, and whether the chosen compute is still on offer),
   * and the create goes out; a compute the account cannot keep, or a reload
   * that fails, is reported on the form instead.
   */
  private createUnderAccountInHand(): void {
    const { shell } = getAppContext();
    const { model } = this.host;
    const accountId = this.landedAccountId();
    if (accountId !== null) shell.adoptAccount(accountId);
    model.isSubmitting = true;
    this.host.setSubmitError("");
    fetchCreateFormDefaults(null, shell.accountId).then(
      (defaults) => {
        if (this.host.isRemoved()) return;
        const isComputeKept = model.adoptAccount(defaults);
        model.isSubmitting = false;
        if (isComputeKept) this.host.create();
        else this.host.setSubmitError(SIGNED_IN_COMPUTE_RESET_MESSAGE);
        m.redraw();
      },
      (error: unknown) => {
        console.error(
          "Could not load the signed-in account for the create",
          error,
        );
        if (this.host.isRemoved()) return;
        model.isSubmitting = false;
        this.host.setSubmitError(SIGNED_IN_ACCOUNT_LOAD_FAILED_MESSAGE);
        m.redraw();
      },
    );
  }
}
