import { describe, expect, it } from "vitest";
import type { FirstRunAccount, FirstRunStage } from "./firstRun";
import {
  SIGN_IN_STAGE,
  accountOf,
  cancelSignIn,
  creationHeading,
  firstNameOf,
  landAccount,
  landingVerdict,
  refuseCreate,
  retryCreate,
  settleLanding,
  startSignIn,
  welcomeHeading,
} from "./firstRun";

const ACCOUNT: FirstRunAccount = { userId: "user-1", email: "gleb@example.com", displayName: "Gleb Denisov" };

describe("the sign-in page's stages", () => {
  it("reads the hand-off's workspace count as where the account goes", () => {
    expect(landingVerdict(0)).toBe("create");
    expect(landingVerdict(3)).toBe("home");
    expect(landingVerdict(null)).toBe("ask");
  });

  it("starts an account known to have no workspaces on its create at once", () => {
    const stage: FirstRunStage = startSignIn(SIGN_IN_STAGE);
    expect(stage).toEqual({ kind: "signing-in" });
    expect(landAccount(stage, ACCOUNT, "create")).toEqual({ kind: "creating", account: ACCOUNT });
  });

  it("asks about an account whose count is unknown, and creates once it is known to have none", () => {
    const landing = landAccount({ kind: "signing-in" }, ACCOUNT, "ask");
    expect(landing).toEqual({ kind: "landing", account: ACCOUNT });
    expect(settleLanding(landing, false)).toEqual({ kind: "creating", account: ACCOUNT });
  });

  it("leaves the stage as it was for an account with workspaces, which leaves for its home page", () => {
    const signingIn: FirstRunStage = { kind: "signing-in" };
    expect(landAccount(signingIn, ACCOUNT, "home")).toBe(signingIn);
    const landing = landAccount(signingIn, ACCOUNT, "ask");
    expect(settleLanding(landing, true)).toBe(landing);
  });

  it("goes back to Sign in when the browser sign-in closes without an account", () => {
    expect(cancelSignIn({ kind: "signing-in" })).toEqual(SIGN_IN_STAGE);
    const landing = landAccount({ kind: "signing-in" }, ACCOUNT, "ask");
    expect(cancelSignIn(landing)).toBe(landing);
  });

  it("offers a refused create again", () => {
    const refused = refuseCreate({ kind: "creating", account: ACCOUNT }, "No capacity.");
    expect(refused).toEqual({ kind: "refused", account: ACCOUNT, message: "No capacity." });
    expect(retryCreate(refused)).toEqual({ kind: "creating", account: ACCOUNT });
  });

  it("ignores a transition that does not belong to the stage", () => {
    expect(landAccount({ kind: "creating", account: ACCOUNT }, { ...ACCOUNT, userId: "user-2" }, "create")).toEqual({
      kind: "creating",
      account: ACCOUNT,
    });
    expect(refuseCreate(SIGN_IN_STAGE, "x")).toBe(SIGN_IN_STAGE);
    expect(accountOf(SIGN_IN_STAGE)).toBeNull();
    expect(accountOf({ kind: "landing", account: ACCOUNT })).toBe(ACCOUNT);
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

describe("the creation page's heading", () => {
  it("welcomes the owner of a first workspace, and names any later one", () => {
    expect(creationHeading(true, "Gleb", "workspace-1")).toBe("Welcome, Gleb!");
    expect(creationHeading(true, "", "workspace-1")).toBe("Welcome!");
    expect(creationHeading(false, "Gleb", "workspace-2")).toBe("Setting up workspace-2");
    expect(creationHeading(false, "Gleb", "")).toBe("Setting up your workspace");
  });
});
