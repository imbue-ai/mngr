// The account step on the live page: which account a sign-in settles on, and which one the cloud create then runs
// under. A file of its own because it drives the live page, whose sign-in module is replaced here.
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { UiAccountEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import type { CreateFormDefaults } from "../../models/create";
import { startFlow } from "../../models/startFlow";
import {
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  collectVnodes,
  createFormDefaults,
  jsonResponse,
  secondAccountEntry,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { StartPage } from "./StartPage";

vi.mock("../../models/webLogin", () => ({
  webLogin: {
    start: vi.fn(async () => {}),
    dismiss: vi.fn(),
    noteSignedIn: vi.fn(),
    state: "waiting",
    email: "",
    isOpen: true,
  },
}));

const ALICE = accountEntry();
const BOB = secondAccountEntry();
const VERIFIED_EMAILS = new Set([BOB.email]);
const CREATE_URL = "/api/v1/workspaces";

function renderPage(): { view: () => unknown; update: () => void } {
  const component = (StartPage as unknown as (vnode: unknown) => m.Component)({});
  return {
    view: (): unknown => (component.view as () => unknown)(),
    update: (): void => (component.onupdate as () => void)(),
  };
}

function press(tree: unknown, name: string, value: string): void {
  const matches = collectVnodes(tree).filter((candidate) => attrsOf(candidate)[name] === value);
  const vnode = matches[matches.length - 1];
  if (vnode === undefined) throw new Error(`nothing with ${name}=${value}`);
  (attrsOf(vnode).onclick as () => void)();
}

function formDefaults(accounts: UiAccountEntry[]): CreateFormDefaults {
  return createFormDefaults({ accounts: accounts.map((account) => ({ user_id: account.user_id, email: account.email })) });
}

/** The address a verification check asks about. */
function emailOf(url: string): string {
  return decodeURIComponent(url.split("email=")[1] ?? "");
}

describe("the account step on the live page", () => {
  let shell: ShellState;
  let fetchMock: ReturnType<typeof vi.fn>;

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

  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.startsWith("/accounts/verification")) {
        const email = emailOf(url);
        return jsonResponse({ verified: VERIFIED_EMAILS.has(email) });
      }
      if (url.startsWith("/ui/api/create/form-defaults")) {
        return jsonResponse(formDefaults(shell.stores.accounts.accounts.slice()));
      }
      if (url === CREATE_URL) return jsonResponse({ error: "stopped here" }, 400);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("a second account created after undoing an unverified first one is the one the create runs under", async () => {
    const page = renderPage();
    press(page.view(), "data-answer", "reporting-yes");
    press(page.view(), "data-answer", "cloud");

    // Create Alice, who never verifies: the flow waits on her email.
    press(page.view(), "data-answer", "signup");
    signIn([ALICE]);
    page.update();
    await vi.waitFor(() => expect(allText(page.view())).toContain("I verified it"));
    expect(verificationChecks()).toEqual([ALICE.email]);

    // Take the account answer back and create Bob instead.
    press(page.view(), "aria-label", "Change answer");
    press(page.view(), "data-answer", "signup");
    page.update();
    // Alice, signed in all along, is not the account this press is waiting on.
    expect(allText(page.view())).not.toContain("I verified it");
    expect(verificationChecks()).toEqual([ALICE.email]);

    signIn([ALICE, BOB]);
    page.update();
    await vi.waitFor(() => expect(createCalls()).toHaveLength(1));
    expect(verificationChecks()).toEqual([ALICE.email, BOB.email]);
    const createInit = createCalls()[0]?.[1] as RequestInit;
    expect(JSON.parse(String(createInit.body))).toMatchObject({ account_id: BOB.user_id });
  });

  it("a verification verdict that lands after switching accounts is not applied to the new account", async () => {
    // Alice is verified, but her check is held until after the flow has moved on to Bob, who is not.
    let releaseAliceCheck: () => void = () => undefined;
    const aliceCheck = new Promise<void>((resolve) => {
      releaseAliceCheck = resolve;
    });
    const defaultFetch = fetchMock.getMockImplementation() as (url: string) => Promise<Response>;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/accounts/verification")) {
        const email = emailOf(url);
        if (email === ALICE.email) await aliceCheck;
        return jsonResponse({ verified: email === ALICE.email });
      }
      return defaultFetch(url);
    });
    signIn([ALICE]);
    const page = renderPage();
    press(page.view(), "data-answer", "reporting-yes");
    press(page.view(), "data-answer", "cloud");
    press(page.view(), "data-answer", "continue");
    expect(verificationChecks()).toEqual([ALICE.email]);

    press(page.view(), "aria-label", "Change answer");
    press(page.view(), "data-answer", "signin");
    signIn([ALICE, BOB]);
    page.update();
    releaseAliceCheck();

    await vi.waitFor(() => expect(allText(page.view())).toContain("I verified it"));
    expect(verificationChecks()).toEqual([ALICE.email, BOB.email]);
    expect(startFlow.state.verificationEmail).toBe(BOB.email);
    expect(createCalls()).toHaveLength(0);
  });

  it("an 'I verified it' press that lands after switching accounts does not answer the new account's question", async () => {
    // Alice's first check says unverified; her press check is held, then says verified once the flow waits on Bob.
    let releaseAlicePress: () => void = () => undefined;
    const alicePress = new Promise<void>((resolve) => {
      releaseAlicePress = resolve;
    });
    let aliceChecks = 0;
    const defaultFetch = fetchMock.getMockImplementation() as (url: string) => Promise<Response>;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/accounts/verification")) {
        const email = emailOf(url);
        if (email !== ALICE.email) return jsonResponse({ verified: false });
        aliceChecks += 1;
        if (aliceChecks === 1) return jsonResponse({ verified: false });
        await alicePress;
        return jsonResponse({ verified: true });
      }
      return defaultFetch(url);
    });
    signIn([ALICE]);
    const page = renderPage();
    press(page.view(), "data-answer", "reporting-yes");
    press(page.view(), "data-answer", "cloud");
    press(page.view(), "data-answer", "continue");
    await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(ALICE.email));
    press(page.view(), "data-answer", "verified");
    await vi.waitFor(() => expect(aliceChecks).toBe(2));

    press(page.view(), "aria-label", "Change answer");
    press(page.view(), "data-answer", "signin");
    signIn([ALICE, BOB]);
    page.update();
    await vi.waitFor(() => expect(startFlow.state.verificationEmail).toBe(BOB.email));
    releaseAlicePress();
    await alicePress;
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(startFlow.state.verificationEmail).toBe(BOB.email);
    expect(startFlow.state.entries.at(-1)).toMatchObject({ id: "verify", answer: null });
    expect(createCalls()).toHaveLength(0);
  });

  it("a create request that fails to build is reported, and the answers can be changed again", async () => {
    // Without a prefill field the create form model cannot apply the defaults, so the body cannot be built.
    const { prefill: _prefill, ...malformed } = formDefaults([BOB]);
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/accounts/verification")) return jsonResponse({ verified: true });
      if (url.startsWith("/ui/api/create/form-defaults")) return jsonResponse(malformed);
      return jsonResponse({});
    });
    signIn([{ ...BOB, is_default: true }]);
    const page = renderPage();
    press(page.view(), "data-answer", "reporting-yes");
    press(page.view(), "data-answer", "cloud");
    press(page.view(), "data-answer", "continue");

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

  it("with an account already signed in, the cloud answer asks whether to keep it", () => {
    signIn([ALICE]);
    const page = renderPage();
    press(page.view(), "data-answer", "reporting-yes");
    press(page.view(), "data-answer", "cloud");

    const answers = collectVnodes(page.view())
      .map((node) => attrsOf(node)["data-answer"])
      .filter((answer) => answer !== undefined);
    expect(answers).toEqual(["signin", "continue"]);
    expect(verificationChecks()).toEqual([]);
  });
});
