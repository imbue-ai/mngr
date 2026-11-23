import { describe, expect, it } from "vitest";
import type { FirstRunAccount, FirstRunStage } from "./firstRun";
import {
  FirstRunRecord,
  SIGN_IN_STAGE,
  accountOf,
  cancelSignIn,
  firstNameOf,
  landAccount,
  noteResend,
  refuseCreate,
  retryCreate,
  settleLanding,
  settleVerification,
  startSignIn,
  welcomeHeading,
} from "./firstRun";

const ACCOUNT: FirstRunAccount = { userId: "user-1", email: "gleb@example.com", displayName: "Gleb Denisov" };

describe("the sign-in page's stages", () => {
  it("walks a new account from Sign in to its create", () => {
    let stage: FirstRunStage = startSignIn(SIGN_IN_STAGE);
    expect(stage).toEqual({ kind: "signing-in" });
    stage = landAccount(stage, ACCOUNT);
    expect(stage).toEqual({ kind: "landing", account: ACCOUNT });
    stage = settleLanding(stage, false);
    expect(stage).toEqual({ kind: "checking", account: ACCOUNT });
    stage = settleVerification(stage, true);
    expect(stage).toEqual({ kind: "creating", account: ACCOUNT });
  });

  it("leaves the landing as it was for an account with workspaces, which leaves for its home page", () => {
    const landing = landAccount({ kind: "signing-in" }, ACCOUNT);
    expect(settleLanding(landing, true)).toBe(landing);
  });

  it("goes back to Sign in when the browser sign-in closes without an account", () => {
    expect(cancelSignIn({ kind: "signing-in" })).toEqual(SIGN_IN_STAGE);
    const landing = landAccount({ kind: "signing-in" }, ACCOUNT);
    expect(cancelSignIn(landing)).toBe(landing);
  });

  it("waits on the emailed link, notes a resend, and creates once the email is verified", () => {
    let stage = settleVerification({ kind: "checking", account: ACCOUNT }, false);
    expect(stage).toEqual({ kind: "verify", account: ACCOUNT, lastResend: null });
    stage = noteResend(stage, "sent");
    expect(stage).toEqual({ kind: "verify", account: ACCOUNT, lastResend: "sent" });
    // A poll that still reads unverified keeps the note.
    expect(settleVerification(stage, false)).toBe(stage);
    expect(settleVerification(stage, true)).toEqual({ kind: "creating", account: ACCOUNT });
  });

  it("offers a refused create again from the email check", () => {
    const refused = refuseCreate({ kind: "creating", account: ACCOUNT }, "No capacity.");
    expect(refused).toEqual({ kind: "refused", account: ACCOUNT, message: "No capacity." });
    expect(retryCreate(refused)).toEqual({ kind: "checking", account: ACCOUNT });
  });

  it("ignores a transition that does not belong to the stage", () => {
    expect(landAccount({ kind: "creating", account: ACCOUNT }, { ...ACCOUNT, userId: "user-2" })).toEqual({
      kind: "creating",
      account: ACCOUNT,
    });
    expect(refuseCreate(SIGN_IN_STAGE, "x")).toBe(SIGN_IN_STAGE);
    expect(accountOf(SIGN_IN_STAGE)).toBeNull();
    expect(accountOf({ kind: "checking", account: ACCOUNT })).toBe(ACCOUNT);
  });
});

describe("the welcome's greeting", () => {
  it("uses the first word of the name, else the email's local part, else none", () => {
    expect(firstNameOf(ACCOUNT)).toBe("Gleb");
    expect(firstNameOf({ displayName: "  ", email: "russell@example.com" })).toBe("Russell");
    expect(firstNameOf({ displayName: "", email: "" })).toBe("");
    expect(firstNameOf(null)).toBe("");
  });

  it("greets by name, or plainly without one", () => {
    expect(welcomeHeading("Gleb")).toBe("Welcome, Gleb!");
    expect(welcomeHeading("")).toBe("Welcome!");
  });
});

describe("FirstRunRecord", () => {
  it("names only the create the sign-in page sent", () => {
    const record = new FirstRunRecord();
    expect(record.isFirstRunCreate("")).toBe(false);
    record.noteCreate("attempt-1", ACCOUNT);
    expect(record.isFirstRunCreate("attempt-1")).toBe(true);
    expect(record.isFirstRunCreate("attempt-2")).toBe(false);
    expect(record.account).toBe(ACCOUNT);
  });
});
