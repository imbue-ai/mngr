import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppContext } from "../../../app-context";
import {
  clearAppContextForTests,
  registerAppContext,
} from "../../../app-context";
import { AccountsStore } from "../../../models/accounts";
import { webLogin } from "../../../models/webLogin";
import {
  accountEntry,
  accountsMessage,
  createFormDefaults,
  jsonResponse,
  secondAccountEntry,
  settle,
} from "../../../testing";
import type { CreateFormModel } from "./form-model";
import {
  ImbueCloudSignInGate,
  SIGNED_IN_ACCOUNT_LOAD_FAILED_MESSAGE,
  SIGNED_IN_COMPUTE_RESET_MESSAGE,
  signInOutcome,
} from "./sign-in-gate";

// The browser sign-in the gate asks for: the tests report its outcome through
// this stand-in rather than run the real flow.
vi.mock("../../../models/webLogin", () => ({
  webLogin: {
    start: vi.fn(async () => {}),
    dismiss: vi.fn(),
    noteSignedIn: vi.fn(),
    state: "idle",
    userId: "",
    email: "",
  },
}));

const ALICE = accountEntry();
const BOB = secondAccountEntry();

afterEach(() => {
  Object.assign(webLogin, { state: "idle", userId: "", email: "" });
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // The module mock's call counts outlive restoreAllMocks.
  vi.clearAllMocks();
});

/** What the gate did to the page it creates for; its form takes any account unless `isComputeKept` says otherwise. */
interface HostLog {
  adoptedAccountIds: string[];
  created: number;
  submitErrors: string[];
  isRemoved: boolean;
  isComputeKept: boolean;
  isSubmitting: boolean;
}

/** A gate in a window on `accountId`, with `signedIn` listed by the accounts store, over a form whose reload answers `defaults`. */
function gateIn(
  accountId: string,
  signedIn: (typeof BOB)[] = [],
  defaults: Response | Error = jsonResponse(
    createFormDefaults({ account: null }),
  ),
): {
  gate: ImbueCloudSignInGate;
  accounts: AccountsStore;
  log: HostLog;
  fetchedUrls: string[];
} {
  const accounts = new AccountsStore();
  accounts.applyAccountsMessage(accountsMessage(signedIn, accountId));
  const log: HostLog = {
    adoptedAccountIds: [],
    created: 0,
    submitErrors: [],
    isRemoved: false,
    isComputeKept: true,
    isSubmitting: false,
  };
  const shell = {
    accountId,
    isAccountSignedOut: () =>
      shell.accountId !== "local" && !accounts.isSignedIn(shell.accountId),
    adoptAccount: (adopted: string) => {
      log.adoptedAccountIds.push(adopted);
      shell.accountId = adopted;
    },
  };
  registerAppContext({ stores: { accounts }, shell } as unknown as AppContext);
  const fetchedUrls: string[] = [];
  vi.stubGlobal("fetch", (url: string) => {
    fetchedUrls.push(url);
    return defaults instanceof Error
      ? Promise.reject(defaults)
      : Promise.resolve(defaults.clone());
  });
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const model = {
    get isSubmitting() {
      return log.isSubmitting;
    },
    set isSubmitting(value: boolean) {
      log.isSubmitting = value;
    },
    adoptAccount: () => log.isComputeKept,
  };
  const gate = new ImbueCloudSignInGate({
    model: model as unknown as CreateFormModel,
    isRemoved: () => log.isRemoved,
    create: () => {
      log.created += 1;
    },
    setSubmitError: (message) => log.submitErrors.push(message),
  });
  return { gate, accounts, log, fetchedUrls };
}

describe("ImbueCloudSignInGate", () => {
  it("takes up the account the poll names when the flow is done before the accounts frame lists it", async () => {
    const { gate, log } = gateIn("local");

    gate.start();
    expect(webLogin.start).toHaveBeenCalledTimes(1);
    expect(log.created).toBe(0);

    Object.assign(webLogin, {
      state: "done",
      userId: BOB.user_id,
      email: BOB.email,
    });
    gate.sync();
    await settle();

    expect(gate.landedAccountId()).toBe(BOB.user_id);
    expect(log.adoptedAccountIds).toEqual([BOB.user_id]);
    expect(log.created).toBe(1);
    expect(webLogin.noteSignedIn).toHaveBeenCalledTimes(1);
    expect(webLogin.dismiss).toHaveBeenCalledTimes(1);
    // Settled once: a later update does not let a second create through.
    gate.sync();
    await settle();
    expect(log.created).toBe(1);
  });

  it("settles on the accounts frame when it beats the poll, taking up the account it lists", async () => {
    const { gate, accounts, log } = gateIn("local");
    gate.start();

    accounts.applyAccountsMessage(accountsMessage([BOB]));
    Object.assign(webLogin, { state: "waiting" });
    gate.sync();
    await settle();

    expect(gate.landedAccountId()).toBe(BOB.user_id);
    expect(log.adoptedAccountIds).toEqual([BOB.user_id]);
    expect(log.created).toBe(1);
  });

  it("settles nothing for a sign-in the user abandoned, and asks again on the next Create", async () => {
    const { gate, log } = gateIn("local");
    gate.start();

    Object.assign(webLogin, { state: "idle" });
    gate.sync();
    await settle();
    expect(log.created).toBe(0);

    gate.start();
    expect(webLogin.start).toHaveBeenCalledTimes(2);
  });

  it("goes ahead at once on an account signed in here, without a sign-in", async () => {
    for (const [windowAccountId, expectedAccountId] of [
      ["local", BOB.user_id],
      [ALICE.user_id, ALICE.user_id],
    ]) {
      const { gate, log } = gateIn(windowAccountId, [BOB, ALICE]);

      gate.start();
      await settle();

      expect(webLogin.start).not.toHaveBeenCalled();
      // The device's window takes the first account here; an account's window keeps its own.
      expect(gate.landedAccountId()).toBe(expectedAccountId);
      expect(log.created).toBe(1);
    }
  });

  it("asks a signed-out account's window to sign in again rather than handing it another account", async () => {
    const gone = accountEntry({
      user_id: "user-gone",
      email: "gone@example.com",
    });
    const { gate, accounts, log } = gateIn(gone.user_id, [BOB]);

    gate.start();
    expect(webLogin.start).toHaveBeenCalledTimes(1);
    // The account already here is not what the window is waiting for.
    Object.assign(webLogin, { state: "waiting" });
    gate.sync();
    await settle();
    expect(log.created).toBe(0);

    // The user signs in again as the window's account: the frame lists it before the poll says so.
    accounts.applyAccountsMessage(accountsMessage([BOB, gone]));
    gate.sync();
    await settle();

    expect(gate.landedAccountId()).toBe(gone.user_id);
    expect(log.adoptedAccountIds).toEqual([gone.user_id]);
    expect(log.created).toBe(1);
  });

  it("takes the account up into the window, reloads the form under it, and creates", async () => {
    const { gate, log, fetchedUrls } = gateIn("local", [BOB]);

    gate.start();
    await settle();

    expect(log.adoptedAccountIds).toEqual([BOB.user_id]);
    expect(fetchedUrls).toEqual([
      `/ui/api/create/form-defaults?account=${BOB.user_id}`,
    ]);
    expect(log.submitErrors).toEqual([""]);
    expect([log.created, log.isSubmitting]).toEqual([1, false]);
  });

  it("reports a compute the account cannot keep, or a form that could not be reloaded, instead of creating", async () => {
    const kept = gateIn("local", [BOB]);
    kept.log.isComputeKept = false;
    kept.gate.start();
    await settle();
    expect([kept.log.created, kept.log.submitErrors]).toEqual([
      0,
      ["", SIGNED_IN_COMPUTE_RESET_MESSAGE],
    ]);
    expect(kept.log.isSubmitting).toBe(false);

    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const failed = gateIn("local", [BOB], new Error("offline"));
    failed.gate.start();
    await settle();
    expect([failed.log.created, failed.log.submitErrors]).toEqual([
      0,
      ["", SIGNED_IN_ACCOUNT_LOAD_FAILED_MESSAGE],
    ]);
    expect(failed.log.isSubmitting).toBe(false);
  });

  it("lands nothing on a page that is gone by the time the form is reloaded", async () => {
    const { gate, log } = gateIn("local", [BOB]);
    gate.start();
    log.isRemoved = true;
    await settle();

    expect([log.created, log.submitErrors]).toEqual([0, [""]]);
  });
});

describe("signInOutcome", () => {
  it("counts the sign-in as landed on the flow's verdict or on an account reaching the channel first", () => {
    expect(signInOutcome("done", false)).toBe("signed-in");
    expect(signInOutcome("waiting", true)).toBe("signed-in");
  });

  it("drops the create once the modal is closed with no account", () => {
    expect(signInOutcome("idle", false)).toBe("abandoned");
  });

  it("keeps waiting while the modal is up, including on an error it offers a retry for", () => {
    for (const state of [
      "starting",
      "waiting",
      "finishing",
      "error",
    ] as const) {
      expect(signInOutcome(state, false)).toBe("pending");
    }
  });
});
