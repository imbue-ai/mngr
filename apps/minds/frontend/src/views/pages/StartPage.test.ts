import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import { firstRun } from "../../models/firstRun";
import { REPORTING_DETAIL_BODY } from "../../models/manifesto";
import { REPORTING_CONSENT_QUESTION } from "../../models/onboarding";
import { webLogin } from "../../models/webLogin";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, byId, collectText, createFormDefaults, jsonResponse, renderDeep } from "../../testing";
import {
  SIGN_IN_LINE_LEAD,
  SIGN_IN_LINE_REST,
  SIGN_IN_MESSAGE,
  StartPage,
  WITHOUT_ACCOUNT_LABEL,
  areDefaultsStale,
  cloudCreateBody,
  cloudCreateRefusal,
} from "./StartPage";

describe("cloudCreateBody", () => {
  it("is the create form's remote preset under the flow's account, with its region", () => {
    const body = cloudCreateBody(createFormDefaults(), "user-1");
    expect(body).toMatchObject({
      launch_mode: "IMBUE_CLOUD",
      backup_provider: "IMBUE_CLOUD",
      account_id: "user-1",
      region: "US-WEST-OR",
      git_url: "https://github.com/imbue-ai/default-workspace-template.git",
      branch: "minds-v9.9.9",
      color: "#0b292b",
      host_name: "",
      // The first workspace's creation page shows the onboarding examples.
      is_onboarding_driven_by_agent: true,
    });
  });

  it("runs under the flow's account", () => {
    const withBob = createFormDefaults({
      account: {
        user_id: "user-2",
        email: "bob@example.com",
        state: "SIGNED_IN",
        is_connection_owner_eligible: true,
      },
    });
    expect(cloudCreateBody(withBob, "user-2")).toMatchObject({
      account_id: "user-2",
    });
  });
});

describe("cloudCreateRefusal", () => {
  it("passes the create form's own sentence through, and words a refusal with none itself", () => {
    const refused = { status: 400, data: { error: "Sharing needs a signed-in account, so turn it off to create this workspace.", field: "enable_web_access" } };
    expect(cloudCreateRefusal(refused)).toBe("Imbue Studio couldn't set up your workspace: Sharing needs a signed-in account, so turn it off to create this workspace.");
    // A 422 lists fields in a shape that carries no sentence of its own.
    const structural = { status: 422, data: { errors: [{ field: "git_url", message: "field required" }] } };
    expect(cloudCreateRefusal(structural)).toBe(
      "Imbue Studio couldn't set up your workspace. Try again. If this persists, report a bug.",
    );
  });

  it("asks for a restart, not the auth layer's words, when the create is refused for this window's lapsed login", () => {
    expect(cloudCreateRefusal({ status: 401, data: { error: "Not authenticated" } })).toBe(
      "Imbue Studio couldn't set up your workspace: Imbue Studio needs to restart to reconnect this window.",
    );
  });

  it("claims nothing about what was created when Imbue Studio did not answer", () => {
    expect(cloudCreateRefusal({ status: 0, data: {} })).toBe("Imbue Studio didn't respond.");
  });
});

describe("areDefaultsStale", () => {
  it("re-reads defaults that do not list the flow's account", () => {
    // The route reads the defaults when it mounts, which on a first run is
    // before the account step; a create built from those could not name the
    // account that signed in since.
    expect(
      areDefaultsStale(createFormDefaults({ account: null }), "user-1"),
    ).toBe(true);
    expect(areDefaultsStale(createFormDefaults(), "user-2")).toBe(true);
  });

  it("keeps defaults that already list the flow's account", () => {
    expect(areDefaultsStale(createFormDefaults(), "user-1")).toBe(false);
  });

  it("has nothing to keep when none were read", () => {
    expect(areDefaultsStale(null, "user-1")).toBe(true);
  });
});

describe("StartPage", () => {
  const SIGNED_IN = { user_id: "user-1", email: "gleb@example.com", display_name: "Gleb Denisov" };

  afterEach(() => {
    webLogin.state = "idle";
    webLogin.userId = "";
    webLogin.email = "";
    firstRun.noteCreate("", null);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAppContextForTests();
  });

  /** The page mounted with no account signed in; `workspaceCount` is what the sign-in landing reports. */
  function mountPage(workspaceCount: number, isVerified = true) {
    const accounts = { accounts: [] as (typeof SIGNED_IN)[], hasAccounts: false, signedInAccount: () => null };
    const routes: string[] = [];
    const adopted: string[] = [];
    const posted: Record<string, unknown>[] = [];
    const consentAnswers: { report_unexpected_errors: boolean }[] = [];
    registerAppContext({
      stores: { accounts },
      shell: { accountId: "", adoptAccount: (id: string) => adopted.push(id), routeToHub: () => routes.push("hub") },
    } as unknown as AppContext);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.spyOn(m.route, "set").mockImplementation((route: string) => {
      routes.push(route);
    });
    // The real model redraws through Mithril's mounted renderer, which a unit test has none of.
    vi.spyOn(webLogin, "dismiss").mockImplementation(() => {
      webLogin.state = "idle";
    });
    vi.spyOn(webLogin, "noteSignedIn").mockImplementation(() => undefined);
    const starts: string[] = [];
    vi.spyOn(webLogin, "start").mockImplementation(async (message = "") => {
      starts.push(message);
    });
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (url.startsWith("/ui/api/onboarding/signin-landing")) {
        return Promise.resolve(jsonResponse({ state: "done", workspace_count: workspaceCount }));
      }
      if (url.startsWith("/ui/api/onboarding/complete")) return Promise.resolve(jsonResponse({}));
      if (url.startsWith("/ui/api/onboarding/consent")) {
        consentAnswers.push(JSON.parse(init?.body as string) as { report_unexpected_errors: boolean });
        return Promise.resolve(jsonResponse({}));
      }
      if (url.startsWith("/accounts/verification")) return Promise.resolve(jsonResponse({ verified: isVerified }));
      if (url.startsWith("/ui/api/create/form-defaults")) return Promise.resolve(jsonResponse(createFormDefaults()));
      if (url === "/api/v1/workspaces") {
        posted.push(JSON.parse(init?.body as string) as Record<string, unknown>);
        return Promise.resolve(jsonResponse({ operation_id: "attempt-1" }, 202));
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });
    const page = StartPage({ attrs: {} } as m.Vnode) as m.Component;
    (page.oninit as () => void).call(page);
    const render = (): AnyVnode => renderDeep((page.view as () => m.Vnode).call(page)) as AnyVnode;
    /** The browser sign-in reports the account and the channel lists it. */
    const landSignIn = (): void => {
      webLogin.state = "done";
      webLogin.userId = SIGNED_IN.user_id;
      webLogin.email = SIGNED_IN.email;
      accounts.accounts = [SIGNED_IN];
      (page.onupdate as () => void).call(page);
    };
    return { page, render, routes, adopted, posted, starts, landSignIn, consentAnswers };
  }

  function press(view: AnyVnode, id: string): void {
    (attrsOf(byId(view, id) as AnyVnode).onclick as () => void)();
  }

  it("signs in through the browser and starts a new account's first cloud workspace at once", async () => {
    const { page, render, routes, adopted, posted, starts, landSignIn } = mountPage(0);
    const signInPage = render();
    expect(collectText(byId(signInPage, "start-line")).join("")).toBe(`${SIGN_IN_LINE_LEAD}${SIGN_IN_LINE_REST}`);
    expect(allText(byId(signInPage, "start-without-account"))).toBe(WITHOUT_ACCOUNT_LABEL);
    press(signInPage, "start-sign-in");
    expect(starts).toEqual([SIGN_IN_MESSAGE]);

    landSignIn();
    expect(allText(byId(render(), "start-welcome"))).toBe("Welcome, Gleb!");
    await vi.waitFor(() => expect(routes).toEqual(["/creating/attempt-1"]));
    expect(adopted).toEqual(["user-1"]);
    expect(posted[0]).toMatchObject({ account_id: "user-1", launch_mode: "IMBUE_CLOUD", is_onboarding_driven_by_agent: true });
    expect(firstRun.isFirstRunCreate("attempt-1")).toBe(true);
    expect(firstRun.account?.displayName).toBe("Gleb Denisov");
    (page.onremove as () => void).call(page);
  });

  it("records the error-reporting checkbox as it stands when Sign in is pressed: on by default, off when unchecked", () => {
    const kept = mountPage(0);
    press(kept.render(), "start-sign-in");
    expect(kept.consentAnswers).toEqual([{ report_unexpected_errors: true }]);
    (kept.page.onremove as () => void).call(kept.page);

    const declined = mountPage(0);
    const checkbox = byId(declined.render(), "start-reporting-checkbox") as AnyVnode;
    expect(attrsOf(checkbox).checked).toBe(true);
    (attrsOf(checkbox).onchange as (event: unknown) => void)({ target: { checked: false } });
    press(declined.render(), "start-sign-in");
    expect(declined.consentAnswers).toEqual([{ report_unexpected_errors: false }]);
    (declined.page.onremove as () => void).call(declined.page);
  });

  it("opens what the checkbox means from its label, without changing the answer", () => {
    const { page, render } = mountPage(0);
    expect(byId(render(), "start-reporting-detail")).toBeUndefined();

    press(render(), "start-reporting-label");

    const open = render();
    const detail = allText(byId(open, "start-reporting-detail"));
    expect(detail.startsWith(`${REPORTING_CONSENT_QUESTION} ${REPORTING_DETAIL_BODY}`)).toBe(true);
    expect(detail).not.toContain("We want you to know");
    expect(attrsOf(byId(open, "start-reporting-checkbox") as AnyVnode).checked).toBe(true);
    press(open, "start-reporting-label");
    expect(byId(render(), "start-reporting-detail")).toBeUndefined();
    (page.onremove as () => void).call(page);
  });

  it("takes an account that already has workspaces to its home page, creating nothing", async () => {
    const { page, render, routes, posted, landSignIn } = mountPage(2);
    press(render(), "start-sign-in");
    landSignIn();
    await vi.waitFor(() => expect(routes).toEqual(["/account/user-1/"]));
    expect(posted).toEqual([]);
    (page.onremove as () => void).call(page);
  });

  it("waits on the emailed link before an unverified account's create", async () => {
    const { page, render, routes, posted, landSignIn } = mountPage(0, false);
    press(render(), "start-sign-in");
    landSignIn();
    await vi.waitFor(() => expect(byId(render(), "start-verify")).toBeDefined());
    expect(posted).toEqual([]);
    expect(routes).toEqual([]);
    (page.onremove as () => void).call(page);
  });

  it("goes back to Sign in when the browser sign-in is closed without an account", () => {
    const { page, render } = mountPage(0);
    press(render(), "start-sign-in");
    expect(attrsOf(render())["data-stage"]).toBe("signing-in");
    webLogin.state = "idle";
    (page.onupdate as () => void).call(page);
    expect(attrsOf(render())["data-stage"]).toBe("sign-in");
    (page.onremove as () => void).call(page);
  });
});

describe("cloudCreateBody on an operator's local launch", () => {
  it("runs the first workspace on this computer when the defaults say the first run is local", () => {
    const body = cloudCreateBody(createFormDefaults({ is_first_run_local: true, local_launch_mode: "LIMA" }), "user-1");
    expect(body).toMatchObject({ launch_mode: "LIMA", is_onboarding_driven_by_agent: true });
  });
});
