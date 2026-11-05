// The account step on the live page: which account a sign-in settles on, and which one the cloud create then runs
// under. A file of its own because it drives the live page, whose sign-in module is replaced here.
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { UiAccountEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import type { CreateFormDefaults } from "../../models/create";
import { VERIFICATION_POLL_MS } from "../../models/emailVerification";
import { CLOUD_CREATE_CHECKING_TEXT, CLOUD_CREATE_STARTING_TEXT, dismissPendingModal, startFlow } from "../../models/startFlow";
import { LOCAL_PROFILE_USER_ID } from "../../models/providers";
import { webLogin } from "../../models/webLogin";
import {
  type AnyVnode,
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  byId,
  collectVnodes,
  createFormDefaults,
  jsonResponse,
  secondAccountEntry,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { pressLast, pressTitlebarLogIn, renderStartPage } from "./StartPage.testing";

vi.mock("../../models/webLogin", () => ({
  webLogin: {
    start: vi.fn(async () => {}),
    dismiss: vi.fn(),
    noteSignedIn: vi.fn(),
    state: "waiting",
    userId: "",
    email: "",
    isOpen: true,
  },
}));

const ALICE = accountEntry();
const BOB = secondAccountEntry();
const VERIFIED_EMAILS = new Set([BOB.email]);
const CREATE_URL = "/api/v1/workspaces";

/** The defaults as the route answers them for the account a `?account=` names, among `accounts`. */
function formDefaults(
  accounts: UiAccountEntry[],
  url: string,
): CreateFormDefaults {
  const accountId = new URL(url, "http://localhost").searchParams.get(
    "account",
  );
  const account = accounts.find((candidate) => candidate.user_id === accountId);
  return createFormDefaults({
    account:
      account === undefined
        ? null
        : {
            user_id: account.user_id,
            email: account.email,
            state: "SIGNED_IN" as const,
            is_connection_owner_eligible: true,
          },
  });
}

/** The address a verification check asks about. */
function emailOf(url: string): string {
  return decodeURIComponent(url.split("email=")[1] ?? "");
}

/** A gate a mocked request awaits, held shut until the test calls `release`. */
function heldGate(): { gate: Promise<void>; release: () => void } {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { gate, release };
}

describe("the account step on the live page", () => {
  let shell: ShellState;
  let fetchMock: ReturnType<typeof vi.fn>;
  // The landing answer per account id the signin-landing route serves; reset empty each test.
  let signinLandingByAccount: Record<string, { state: string; workspace_count: number }>;

  function signIn(accounts: UiAccountEntry[]): void {
    shell.stores.accounts.applyAccountsMessage(accountsMessage(accounts));
  }

  function verificationChecks(): string[] {
    return fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.startsWith("/accounts/verification"))
      .map(emailOf);
  }

  function createCalls(): unknown[][] {
    return fetchMock.mock.calls.filter(([url]) => url === CREATE_URL);
  }

  /** Answer `override` first; a request it resolves to undefined goes to the default mock. */
  function interceptFetch(override: (url: string) => Promise<Response | undefined>): void {
    const defaultFetch = fetchMock.getMockImplementation() as (url: string) => Promise<Response>;
    fetchMock.mockImplementation(async (url: string) => (await override(url)) ?? defaultFetch(url));
  }

  /** Accept error reporting, choose Imbue Cloud, and continue as the account already signed in. */
  function continueAsSignedInAccount(page: { view: () => unknown }): void {
    pressLast(page.view(), "data-answer", "reporting-yes");
    pressLast(page.view(), "data-answer", "cloud");
    pressLast(page.view(), "data-answer", "continue");
  }

  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    signinLandingByAccount = {};
    fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.startsWith("/accounts/verification")) {
        const email = emailOf(url);
        return jsonResponse({ verified: VERIFIED_EMAILS.has(email) });
      }
      if (url.startsWith("/ui/api/create/form-defaults")) {
        return jsonResponse(
          formDefaults(shell.stores.accounts.accounts.slice(), url),
        );
      }
      if (url.startsWith("/ui/api/onboarding/signin-landing")) {
        const accountId =
          new URL(url, "http://localhost").searchParams.get("account") ?? "";
        return jsonResponse(
          signinLandingByAccount[accountId] ?? { state: "done", workspace_count: 0 },
        );
      }
      if (url === CREATE_URL) return jsonResponse({ error: "stopped here" }, 400);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("a second account created after undoing an unverified first one is the one the create runs under", async () => {
    const page = renderStartPage();
    pressLast(page.view(), "data-answer", "reporting-yes");
    pressLast(page.view(), "data-answer", "cloud");

    // Create Alice, who never verifies: the flow waits on her email.
    pressLast(page.view(), "data-answer", "signup");
    signIn([ALICE]);
    page.update();
    await vi.waitFor(() => expect(allText(page.view())).toContain("I verified it"));
    expect(verificationChecks()).toEqual([ALICE.email]);

    // Take the account answer back and create Bob instead.
    pressLast(page.view(), "aria-label", "Change answer");
    pressLast(page.view(), "data-answer", "signup");
    page.update();
    // Alice, signed in all along, is not the account this press is waiting on.
    expect(allText(page.view())).not.toContain("I verified it");
    expect(verificationChecks()).toEqual([ALICE.email]);

    signIn([ALICE, BOB]);
    page.update();
    await vi.waitFor(() => expect(createCalls()).toHaveLength(1));
    expect(verificationChecks()).toEqual([ALICE.email, BOB.email]);
    const createInit = createCalls()[0]?.[1] as RequestInit;
    expect(JSON.parse(String(createInit.body))).toMatchObject({
      account_id: BOB.user_id,
    });
    // The create runs under Bob, so the window became Bob's: its creation page and first workspace are his.
    expect(shell.accountId).toBe(BOB.user_id);
  });

  it("a verification verdict that lands after switching accounts is not applied to the new account", async () => {
    // Alice is verified, but her check is held until after the flow has moved on to Bob, who is not.
    const aliceCheck = heldGate();
    interceptFetch(async (url: string) => {
      if (url.startsWith("/accounts/verification")) {
        const email = emailOf(url);
        if (email === ALICE.email) await aliceCheck.gate;
        return jsonResponse({ verified: email === ALICE.email });
      }
      return undefined;
    });
    signIn([ALICE]);
    const page = renderStartPage();
    continueAsSignedInAccount(page);
    expect(verificationChecks()).toEqual([ALICE.email]);

    pressLast(page.view(), "aria-label", "Change answer");
    pressLast(page.view(), "data-answer", "signin");
    signIn([ALICE, BOB]);
    page.update();
    aliceCheck.release();

    await vi.waitFor(() => expect(allText(page.view())).toContain("I verified it"));
    expect(verificationChecks()).toEqual([ALICE.email, BOB.email]);
    expect(startFlow.state.verificationEmail).toBe(BOB.email);
    expect(createCalls()).toHaveLength(0);
  });

  it("an 'I verified it' press that lands after switching accounts does not answer the new account's question", async () => {
    // Alice's first check says unverified; her press check is held, then says verified once the flow waits on Bob.
    const alicePress = heldGate();
    let aliceChecks = 0;
    interceptFetch(async (url: string) => {
      if (url.startsWith("/accounts/verification")) {
        const email = emailOf(url);
        if (email !== ALICE.email) return jsonResponse({ verified: false });
        aliceChecks += 1;
        if (aliceChecks === 1) return jsonResponse({ verified: false });
        await alicePress.gate;
        return jsonResponse({ verified: true });
      }
      return undefined;
    });
    signIn([ALICE]);
    const page = renderStartPage();
    continueAsSignedInAccount(page);
    await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(ALICE.email));
    pressLast(page.view(), "data-answer", "verified");
    await vi.waitFor(() => expect(aliceChecks).toBe(2));

    pressLast(page.view(), "aria-label", "Change answer");
    pressLast(page.view(), "data-answer", "signin");
    signIn([ALICE, BOB]);
    page.update();
    await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(BOB.email));
    alicePress.release();
    await alicePress.gate;
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(startFlow.state.verificationEmail).toBe(BOB.email);
    expect(startFlow.state.entries.at(-1)).toMatchObject({ id: "verify", answer: null });
    expect(createCalls()).toHaveLength(0);
  });

  it("a create request that fails to build is reported, and the answers can be changed again", async () => {
    // Without a prefill field the create form model cannot apply the defaults, so the body cannot be built.
    const { prefill: _prefill, ...malformed } = formDefaults(
      [BOB],
      `/ui/api/create/form-defaults?account=${BOB.user_id}`,
    );
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/accounts/verification")) return jsonResponse({ verified: true });
      if (url.startsWith("/ui/api/create/form-defaults")) return jsonResponse(malformed);
      return jsonResponse({});
    });
    signIn([BOB]);
    const page = renderStartPage();
    continueAsSignedInAccount(page);

    await vi.waitFor(() =>
      expect(startFlow.state.entries.at(-1)).toMatchObject({
        id: "again",
        ack: "That did not work: could not send the create request.",
      }),
    );
    expect(createCalls()).toHaveLength(0);
    const undos = collectVnodes(page.view()).filter((node) => attrsOf(node)["aria-label"] === "Change answer");
    expect(undos.length).toBeGreaterThan(0);
  });

  it("reads the create defaults without the local probes, both on arrival and for the create", async () => {
    const page = renderStartPage();
    // The arrival read is under the window's local profile, so the create under Bob must read the defaults again.
    signIn([BOB]);
    continueAsSignedInAccount(page);

    await vi.waitFor(() => expect(createCalls()).toHaveLength(1));
    const defaultsReads = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.startsWith("/ui/api/create/form-defaults"));
    const cloudDefaultsUrl = (accountId: string): string =>
      `/ui/api/create/form-defaults?account=${accountId}&skip_local_probes=1`;
    expect(defaultsReads).toEqual([cloudDefaultsUrl(LOCAL_PROFILE_USER_ID), cloudDefaultsUrl(BOB.user_id)]);
  });

  /** The line under the transcript that says what the owed cloud create waits on, if it is there. */
  function pendingLine(page: { view: () => unknown }): AnyVnode | undefined {
    return byId(page.view(), "start-cloud-create-pending");
  }

  it("says it is checking the account during the check, and that the workspace is starting while the create is sent", async () => {
    const check = heldGate();
    const create = heldGate();
    interceptFetch(async (url: string) => {
      if (url.startsWith("/accounts/verification")) await check.gate;
      if (url === CREATE_URL) await create.gate;
      return undefined;
    });
    signIn([BOB]);
    const page = renderStartPage();
    pressLast(page.view(), "data-answer", "reporting-yes");
    pressLast(page.view(), "data-answer", "cloud");
    expect(pendingLine(page)).toBeUndefined();

    pressLast(page.view(), "data-answer", "continue");
    const checking = pendingLine(page);
    if (checking === undefined) throw new Error("no line says the account is being checked");
    expect(allText(checking)).toContain(CLOUD_CREATE_CHECKING_TEXT);
    expect(allText(page.view())).not.toContain(CLOUD_CREATE_STARTING_TEXT);
    // It waits for the agent's note to land, so a create sent sooner never shows it.
    expect(String(attrsOf(checking).style)).toMatch(/--start-chat-delay: [1-9]\d*ms/);

    // Bob is verified: the create goes out and is held in flight. A settled
    // promise does not repaint a mithril page, so the page asks for one.
    const redrawsBeforeVerdict = vi.mocked(m.redraw).mock.calls.length;
    check.release();
    await vi.waitFor(() => expect(createCalls()).toHaveLength(1));
    expect(vi.mocked(m.redraw).mock.calls.length).toBeGreaterThan(redrawsBeforeVerdict);
    const starting = pendingLine(page);
    if (starting === undefined) throw new Error("no line says the workspace is starting");
    expect(allText(starting)).toContain(CLOUD_CREATE_STARTING_TEXT);
    expect(allText(page.view())).not.toContain(CLOUD_CREATE_CHECKING_TEXT);

    create.release();
    await vi.waitFor(() => expect(startFlow.state.entries.at(-1)).toMatchObject({ id: "again" }));
    expect(pendingLine(page)).toBeUndefined();
  });

  it("never says the workspace is starting for an email the check finds unverified", async () => {
    const check = heldGate();
    interceptFetch(async (url: string) => {
      if (url.startsWith("/accounts/verification")) await check.gate;
      return undefined;
    });
    signIn([ALICE]);
    const page = renderStartPage();
    continueAsSignedInAccount(page);
    expect(allText(page.view())).toContain(CLOUD_CREATE_CHECKING_TEXT);
    expect(allText(page.view())).not.toContain(CLOUD_CREATE_STARTING_TEXT);

    check.release();
    await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(ALICE.email));
    expect(pendingLine(page)).toBeUndefined();
    expect(createCalls()).toHaveLength(0);
  });

  it("says nothing about the create while it waits on the emailed link", async () => {
    vi.useFakeTimers();
    const poll = heldGate();
    let checks = 0;
    interceptFetch(async (url: string) => {
      if (url.startsWith("/accounts/verification")) {
        checks += 1;
        if (checks > 1) await poll.gate;
        return jsonResponse({ verified: false });
      }
      return undefined;
    });
    signIn([ALICE]);
    const page = renderStartPage();
    continueAsSignedInAccount(page);
    await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(ALICE.email));

    // The next poll's check is held in flight.
    await vi.advanceTimersByTimeAsync(VERIFICATION_POLL_MS);
    expect(checks).toBe(2);
    expect(allText(page.view())).toContain("I verified it");
    expect(pendingLine(page)).toBeUndefined();

    poll.release();
  });

  /** The mocked sign-in module as the tests set its outcome; module-level, so a test puts it back. */
  function reportSignIn(state: string, email: string, userId = ""): void {
    Object.assign(webLogin, { state, email, userId });
  }

  function onboardingCompleteCalls(): unknown[][] {
    return fetchMock.mock.calls.filter(([url]) => url === "/ui/api/onboarding/complete");
  }

  it.each([
    [
      "the account the sign-in's poll named, before the accounts frame lists it",
      // The poll settled on Bob while the channel still lists Alice alone.
      [ALICE],
      BOB.email,
      BOB.user_id,
      BOB.user_id,
    ],
    [
      "the account the sign-in reported by email",
      // Alice was signed in all along; the sign-in that just finished is Bob's.
      [ALICE, BOB],
      BOB.email,
      "",
      BOB.user_id,
    ],
    ["the first signed-in account when the sign-in named none", [ALICE, BOB], "", "", ALICE.user_id],
  ])(
    "the existing-login answer leaves for the hub of %s when the account has workspaces",
    async (_name, signedIn, reportedEmail, reportedUserId, landedAccountId) => {
      signinLandingByAccount = { [landedAccountId]: { state: "done", workspace_count: 2 } };
      const routeSet = vi
        .spyOn(m.route, "set")
        .mockImplementation(() => undefined);
      const page = renderStartPage();
      pressLast(page.view(), "data-answer", "reporting-yes");
      pressTitlebarLogIn();
      signIn(signedIn);
      reportSignIn("done", reportedEmail, reportedUserId);
      try {
        page.update();
        await vi.waitFor(() =>
          expect(routeSet.mock.calls.map((call) => call[0])).toEqual([
            `/account/${landedAccountId}/`,
          ]),
        );
      } finally {
        reportSignIn("waiting", "");
      }

      expect(shell.accountId).toBe(landedAccountId);
      expect(onboardingCompleteCalls()).toHaveLength(1);
      expect(createCalls()).toEqual([]);
    },
  );

  it("registers the title bar's log-in on mount and redraws, since the title bar was drawn first", () => {
    const redraw = vi.mocked(m.redraw);
    redraw.mockClear();
    renderStartPage();
    expect(startFlow.logInExisting).not.toBeNull();
    expect(redraw).toHaveBeenCalled();
  });

  it("the title bar's log-in works before the reporting question is answered, leaving for the account's hub", async () => {
    signinLandingByAccount = { [BOB.user_id]: { state: "done", workspace_count: 2 } };
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const page = renderStartPage();
    pressTitlebarLogIn();
    signIn([BOB]);
    reportSignIn("done", BOB.email, BOB.user_id);
    try {
      page.update();
      await vi.waitFor(() =>
        expect(routeSet.mock.calls.map((call) => call[0])).toEqual([`/account/${BOB.user_id}/`]),
      );
    } finally {
      reportSignIn("waiting", "");
    }

    expect(shell.accountId).toBe(BOB.user_id);
    expect(onboardingCompleteCalls()).toHaveLength(1);
  });

  it.each([
    ["no workspaces", { state: "done", workspace_count: 0 }],
    ["a record fetch that failed", { state: "failed", workspace_count: 0 }],
  ])(
    "the existing-login answer continues the conversation for an account with %s",
    async (_name, landing) => {
      signinLandingByAccount = { [BOB.user_id]: landing };
      const routeSet = vi
        .spyOn(m.route, "set")
        .mockImplementation(() => undefined);
      const page = renderStartPage();
      pressLast(page.view(), "data-answer", "reporting-yes");
      pressTitlebarLogIn();
      signIn([BOB]);
      reportSignIn("done", BOB.email, BOB.user_id);
      try {
        page.update();
        await vi.waitFor(() =>
          expect(startFlow.state.entries.at(-1)).toMatchObject({
            id: "again",
            answer: null,
          }),
        );
      } finally {
        reportSignIn("waiting", "");
      }

      // The sign-in became the first question's answer; the flow stays, under the signed-in account.
      expect(startFlow.state.entries[1]).toMatchObject({
        id: "run",
        answer: "existing-login",
        said: `You've signed in as ${BOB.email}`,
      });
      expect(shell.accountId).toBe(BOB.user_id);
      expect(routeSet).not.toHaveBeenCalled();
      // Onboarding is not complete: only a create (or a workspace-holding sign-in) completes it.
      expect(onboardingCompleteCalls()).toHaveLength(0);
      expect(createCalls()).toEqual([]);
    },
  );

  it("the existing-login landing waits out a pending record fetch before deciding", async () => {
    let landingAsks = 0;
    signinLandingByAccount = { [BOB.user_id]: { state: "pending", workspace_count: 0 } };
    interceptFetch(async (url: string) => {
      if (url.startsWith("/ui/api/onboarding/signin-landing")) {
        landingAsks += 1;
        if (landingAsks >= 2) return jsonResponse({ state: "done", workspace_count: 1 });
      }
      return undefined;
    });
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const page = renderStartPage();
    pressLast(page.view(), "data-answer", "reporting-yes");
    pressTitlebarLogIn();
    signIn([BOB]);
    reportSignIn("done", BOB.email, BOB.user_id);
    try {
      page.update();
      await vi.waitFor(
        () =>
          expect(routeSet.mock.calls.map((call) => call[0])).toEqual([
            `/account/${BOB.user_id}/`,
          ]),
        { timeout: 4000 },
      );
    } finally {
      reportSignIn("waiting", "");
    }
    expect(landingAsks).toBe(2);
  });

  it("the title bar's log-in does nothing while the owed cloud create's verification check runs", () => {
    signIn([ALICE]);
    const page = renderStartPage();
    continueAsSignedInAccount(page);
    expect(startFlow.state.isCloudCreatePending).toBe(true);
    vi.mocked(webLogin.start).mockClear();

    pressTitlebarLogIn();
    expect(startFlow.state.pending).toBeNull();
    expect(webLogin.start).not.toHaveBeenCalled();
  });

  it("a verification that lands while the title bar's log-in is signing in does not send the create", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      signIn([ALICE]);
      const page = renderStartPage();
      continueAsSignedInAccount(page);
      await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(ALICE.email));

      pressTitlebarLogIn();
      expect(startFlow.state.pending).toBe("existing-login");
      VERIFIED_EMAILS.add(ALICE.email);
      const checksBefore = verificationChecks().length;
      await vi.advanceTimersByTimeAsync(VERIFICATION_POLL_MS * 2);
      expect(verificationChecks()).toHaveLength(checksBefore);
      expect(createCalls()).toHaveLength(0);

      // A dismissed sign-in hands the wait back to the poll.
      startFlow.state = dismissPendingModal(startFlow.state);
      await vi.advanceTimersByTimeAsync(VERIFICATION_POLL_MS);
      await vi.waitFor(() => expect(createCalls()).toHaveLength(1));
    } finally {
      VERIFIED_EMAILS.delete(ALICE.email);
      vi.useRealTimers();
    }
  });

  it("with an account already signed in, the cloud answer asks whether to keep it", () => {
    signIn([ALICE]);
    const page = renderStartPage();
    pressLast(page.view(), "data-answer", "reporting-yes");
    pressLast(page.view(), "data-answer", "cloud");

    const answers = collectVnodes(page.view())
      .map((node) => attrsOf(node)["data-answer"])
      .filter((answer) => answer !== undefined);
    expect(answers).toEqual(["signin", "continue"]);
    expect(verificationChecks()).toEqual([]);
  });
});
