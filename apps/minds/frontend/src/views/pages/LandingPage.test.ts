import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import { errorDialogs } from "../../models/errorDialog";
import { onboardingProgress } from "../../models/onboarding";
import { webLogin } from "../../models/webLogin";
import type { UiAccountEntry } from "../../channel/messages";
import type { UiConnectionEntry } from "../../generated/ui";
import type { AnyVnode } from "../../testing";
import {
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  byId,
  classesOf,
  collectVnodes,
  jsonResponse,
  recordingFetch,
  renderDeep,
  updatesShell,
  withAttr,
  workspacesMessage,
} from "../../testing";
import {
  LandingPage,
  isUnlockBannerDue,
  otherAccountsWithWorkspaces,
  shouldRedirectToStartFlow,
} from "./LandingPage";
import { BTN_VARIANTS } from "../components/constants";

const settled = { is_discovery_complete: true, has_restorable_workspaces: false, destroying_status_by_agent_id: {}, orphaned_failed_destroys: [], locked_account_emails: [] };

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

/** The page constructed and initialized as the router would, `fetchImpl` answering its fetches. */
function initLandingPage(fetchImpl: (url: string) => Promise<Response>) {
  vi.stubGlobal("fetch", fetchImpl);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  const redraw = vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const vnode = m(LandingPage, {}) as m.Vnode;
  const page = (LandingPage as unknown as (v: m.Vnode) => m.Component)(vnode);
  page.oninit?.call(page, vnode as m.VnodeDOM);
  const view = () => (page.view as unknown as (v: m.Vnode) => m.Vnode).call(page, vnode);
  return { page, vnode, routeSet, redraw, view };
}

describe("shouldRedirectToStartFlow", () => {

  it("sends an install that never finished onboarding to the start flow once discovery finds nothing", () => {
    expect(shouldRedirectToStartFlow(false, false, settled)).toBe(true);
  });

  it("never redirects a completed install, an install with a workspace under any account, or an unfinished discovery", () => {
    expect(shouldRedirectToStartFlow(true, false, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, true, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, null)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, { ...settled, is_discovery_complete: false })).toBe(false);
  });
});

describe("otherAccountsWithWorkspaces", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
  const BOB = accountEntry({ user_id: "user-bob", email: "bob@example.com", display_name: "Bob" });
  const GONE = accountEntry({ user_id: "user-gone", email: "gone@example.com" });

  it("lists every other account with workspaces, signed in, signed out, then the device, and skips the window's own", () => {
    const counts = { "user-alice": 4, "user-bob": 0, "user-gone": 1, local: 2 };

    expect(otherAccountsWithWorkspaces("user-bob", counts, [ALICE, BOB], [GONE])).toEqual([
      { accountId: "user-alice", label: "alice@example.com", workspaceCount: 4 },
      { accountId: "user-gone", label: "gone@example.com", workspaceCount: 1 },
      { accountId: "local", label: "This device", workspaceCount: 2 },
    ]);
    expect(otherAccountsWithWorkspaces("local", counts, [ALICE, BOB], [GONE]).map((other) => other.accountId)).toEqual([
      "user-alice",
      "user-gone",
    ]);
  });
});

describe("isUnlockBannerDue", () => {
  const aliceLocked = { ...settled, locked_account_emails: ["alice@example.com"] };

  it("is due exactly when the loaded extras are the window's account's answer and name it locked", () => {
    expect(isUnlockBannerDue(aliceLocked, "user-alice", "user-alice")).toBe(true);
  });

  it("stays down for another window's answer, an unlocked answer, or no answer yet", () => {
    expect(isUnlockBannerDue(aliceLocked, "user-alice", "local")).toBe(false);
    expect(isUnlockBannerDue(settled, "local", "local")).toBe(false);
    expect(isUnlockBannerDue(null, null, "user-alice")).toBe(false);
  });
});

describe("the landing extras fetch", () => {
  /** The page initialized in a window on Alice's hub, with every URL it fetches recorded. */
  function initPageOnAlicesHub() {
    const alice = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
    const shell = updatesShell({}, false);
    shell.stores.accounts.applyAccountsMessage(accountsMessage([alice], alice.user_id));
    shell.accountId = alice.user_id;
    const recorder = recordingFetch();
    const { page, vnode } = initLandingPage(recorder.fetchImpl);
    return { page, vnode, shell, recorder };
  }

  it("asks for the window's account", () => {
    const { recorder } = initPageOnAlicesHub();

    expect(recorder.requests).toContain(
      "/ui/api/create/landing-extras?account=user-alice",
    );
  });

  it("re-asks for the new account when the window's account changes, and only then", () => {
    const { page, vnode, shell, recorder } = initPageOnAlicesHub();

    shell.accountId = "local";
    page.onupdate?.call(page, vnode as m.VnodeDOM);
    // A redraw without an account change must not re-ask.
    page.onupdate?.call(page, vnode as m.VnodeDOM);

    expect(
      recorder.requests.filter((url) => url.includes("landing-extras")),
    ).toEqual([
      "/ui/api/create/landing-extras?account=user-alice",
      "/ui/api/create/landing-extras?account=local",
    ]);
  });

  it("says a failed fetch in place, with Retry re-asking and a report offered", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    updatesShell({}, false);
    const recorder = recordingFetch((url) =>
      url.includes("landing-extras")
        ? Promise.reject(new TypeError("Failed to fetch"))
        : Promise.resolve(jsonResponse({})),
    );
    const { view } = initLandingPage(recorder.fetchImpl);
    await vi.waitFor(() =>
      expect(byId(view(), "landing-extras-error")).toBeDefined(),
    );
    const notice = renderDeep(byId(view(), "landing-extras-error"));

    expect(allText(notice)).toContain("Could not load workspace details.");
    expect(allText(notice)).toContain("Report a bug");
    const retry = collectVnodes(notice).find(
      (vnode) => vnode.tag === "button" && allText(vnode) === "Retry",
    );
    (attrsOf(retry!).onclick as () => void)();
    expect(
      recorder.requests.filter((url) => url.includes("landing-extras")),
    ).toHaveLength(2);
  });
});

describe("the hub's create control", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });

  /** The page drawn in a window on `accountId`, with Alice the one account signed in. */
  function draw(accountId: string): m.Children {
    const shell = updatesShell({}, false);
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], accountId));
    shell.accountId = accountId;
    return initLandingPage(recordingFetch().fetchImpl).view();
  }

  it.each([
    ["a signed-in account", ALICE.user_id],
    ["the device", "local"],
  ])("offers Create on %s's hub, linking to that account's create page", (_name, accountId) => {
    const root = draw(accountId);

    expect(byId(root, "landing-sign-in")).toBeUndefined();
    const create = byId(root, "landing-create");
    expect(create).toBeDefined();
    expect(attrsOf(create!).href).toBe(`/account/${accountId}/create`);
  });

  it("offers to sign in again, in place of Create, on a signed-out account's hub", () => {
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const root = draw("user-gone");

    expect(byId(root, "landing-create")).toBeUndefined();
    const signIn = byId(root, "landing-sign-in");
    expect(signIn).toBeDefined();
    (attrsOf(signIn!).onclick as () => void)();
    expect(start).toHaveBeenCalledTimes(1);
  });
});

/** The device's home page once discovery settled with none of its own, `countByAccountId` install-wide. */
async function initSettledDeviceHub(options: {
  signedIn: UiAccountEntry[];
  countByAccountId: Record<string, number>;
  restorableIds?: string[];
}) {
  const shell = updatesShell({}, false);
  shell.stores.accounts.applyAccountsMessage(accountsMessage(options.signedIn, "local"));
  shell.stores.workspaces.applyWorkspacesMessage(
    workspacesMessage({
      workspaces: [],
      workspace_count_by_account_id: options.countByAccountId,
      restorable_workspace_ids: options.restorableIds ?? [],
    }),
  );
  shell.accountId = "local";
  const { page, vnode, routeSet, redraw, view } = initLandingPage(
    recordingFetch(() => Promise.resolve(jsonResponse(settled))).fetchImpl,
  );
  await vi.waitFor(() => expect(redraw).toHaveBeenCalled());
  return { shell, page, vnode, routeSet, view };
}

describe("the other-accounts notice", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });

  async function drawSettledDeviceHub(countByAccountId: Record<string, number>) {
    const { shell, view } = await initSettledDeviceHub({ signedIn: [ALICE], countByAccountId });
    return { shell, root: view() };
  }

  it("offers the account that has workspaces, and choosing it switches the window", async () => {
    const { shell, root } = await drawSettledDeviceHub({ "user-alice": 4 });
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);

    const notice = byId(root, "landing-other-accounts");
    expect(notice).toBeDefined();
    const offers = withAttr(notice, "data-account-id");
    expect(offers.map((offer) => attrsOf(offer)["data-account-id"])).toEqual(["user-alice"]);
    expect(allText(offers[0])).toBe("alice@example.com · 4 workspaces");
    (attrsOf(offers[0]).onclick as () => void)();
    expect(switchAccount).toHaveBeenCalledWith("user-alice");
  });

  it("is absent when no other account has workspaces", async () => {
    const { root } = await drawSettledDeviceHub({});

    expect(byId(root, "landing-other-accounts")).toBeUndefined();
  });

  it("waits for the new account's extras after a hub switch instead of reading the device's", async () => {
    const { shell, view } = await initSettledDeviceHub({ signedIn: [ALICE], countByAccountId: { local: 1 } });

    shell.accountId = ALICE.user_id;
    const root = view();

    expect(allText(root)).toContain("Discovering workspaces...");
    expect(byId(root, "landing-other-accounts")).toBeUndefined();
  });
});

describe("the start-flow hand-over", () => {
  afterEach(() => {
    onboardingProgress.seed(true);
  });

  /** Where the device's empty home page sends an install that never finished onboarding. */
  async function routeAfterSettledDiscovery(countByAccountId: Record<string, number>, restorableIds: string[]) {
    onboardingProgress.seed(false);
    const { page, vnode, routeSet } = await initSettledDeviceHub({ signedIn: [], countByAccountId, restorableIds });
    page.onupdate?.call(page, vnode as m.VnodeDOM);
    return routeSet.mock.calls.map((call) => call[0]);
  }

  it("hands an install with no workspace anywhere over to the start flow", async () => {
    expect(await routeAfterSettledDiscovery({}, [])).toEqual(["/start"]);
  });

  it("keeps the page when another account has rows", async () => {
    expect(await routeAfterSettledDiscovery({ "user-alice": 2 }, [])).toEqual([]);
  });

  it("keeps the page while a workspace is not re-listed yet", async () => {
    expect(await routeAfterSettledDiscovery({}, ["agent-0a1b"])).toEqual([]);
  });
});

/** The raised error dialog, with a host attached so the queue keeps it rather than alerting. */
function withDialogHost(): { current: () => typeof errorDialogs.current; release: () => void } {
  errorDialogs.attachHost();
  return {
    current: () => errorDialogs.current,
    release: () => {
      while (errorDialogs.current !== null) errorDialogs.dismiss();
      errorDialogs.detachHost();
    },
  };
}

describe("a remote row's access chip", () => {
  const REMOTE = {
    id: "agent-remote1",
    name: "far away",
    accent: "#112233",
    host_id: "host-remote1",
    is_backend_unreachable: false,
    supports_shutdown: true,
    liveness: "",
    account: "",
    create_attempt_state: "",
    is_remote: true,
    remote_kind: "cloud",
    location: "team aws (AWS)",
  };

  function drawRemoteRow(state: string, detail: string | null) {
    const shell = updatesShell({}, false);
    shell.stores.workspaces.applyWorkspacesMessage(
      workspacesMessage({ workspaces: [REMOTE], remote_workspace_states: { [REMOTE.id]: { state, detail } } }),
    );
    shell.accountId = "local";
    const root = initLandingPage(recordingFetch(() => Promise.resolve(jsonResponse(settled))).fetchImpl).view();
    return withAttr(root, "data-remote-state");
  }

  it("opens the styled error dialog for a failed setup, with the server's detail behind See details", () => {
    const dialogs = withDialogHost();
    try {
      const detail = "Could not write the host access material: [Errno 28] No space left on device";
      const [chip] = drawRemoteRow("error", detail);

      expect(chip.tag).toBe("button");
      expect(allText(chip)).not.toContain("Errno");
      expect(String(attrsOf(chip)["data-tooltip"])).not.toContain("Errno");
      (attrsOf(chip).onclick as () => void)();
      expect(dialogs.current()?.error.message).toContain("Setting up this computer's access to this workspace failed.");
      expect(dialogs.current()?.error.details).toBe(detail);
    } finally {
      dialogs.release();
    }
  });

  it("explains a state with nothing to show behind it on hover only", () => {
    const [chip] = drawRemoteRow("unreachable", null);

    expect(chip.tag).toBe("span");
    expect(attrsOf(chip).onclick).toBeUndefined();
    expect(String(attrsOf(chip)["data-tooltip"])).toContain("can't reach this workspace from this computer");
  });
});

describe("a Start that fails", () => {
  const STOPPED = {
    id: "agent-stopped1",
    name: "Fox",
    accent: "#112233",
    host_id: "host-stopped1",
    is_backend_unreachable: false,
    supports_shutdown: true,
    liveness: "STOPPED",
    account: "",
    create_attempt_state: "",
  };

  it("offers Try again in its dialog, which starts the workspace again", async () => {
    const dialogs = withDialogHost();
    try {
      const shell = updatesShell({}, false);
      shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces: [STOPPED] }));
      shell.accountId = "local";
      const fetches = recordingFetch((url) =>
        Promise.resolve(
          url.endsWith("/start")
            ? jsonResponse({ error: "Could not start the workspace host: container exited (137)" }, 502)
            : jsonResponse(settled),
        ),
      );
      const { view } = initLandingPage(fetches.fetchImpl);
      const startButton = collectVnodes(view()).find((vnode) => attrsOf(vnode)["aria-label"] === "Start workspace");
      (attrsOf(startButton as AnyVnode).onclick as (event: { stopPropagation: () => void }) => void)({
        stopPropagation: () => undefined,
      });

      await vi.waitFor(() => expect(dialogs.current()).not.toBeNull());
      // The title names what failed, so the sentence under it says only what
      // the user can do about it.
      expect(dialogs.current()?.title).toBe('Couldn\'t start "Fox"');
      expect(dialogs.current()?.error.message).toBe("Try again. If this persists, report a bug.");
      expect(dialogs.current()?.error.details).toContain("container exited (137)");
      const tryAgain = dialogs.current()?.actions?.[0];
      expect(tryAgain?.label).toBe("Try again");
      // The dialog closes itself before running a button's action.
      errorDialogs.dismiss();
      tryAgain?.onclick();

      await vi.waitFor(() => expect(dialogs.current()).not.toBeNull());
      expect(fetches.requests.filter((url) => url.endsWith("/start"))).toHaveLength(2);
    } finally {
      dialogs.release();
    }
  });
});

describe("the providers panel", () => {
  const ERRORED_CONNECTION = {
    id: "conn-1",
    instance: "aws_" + "1".repeat(32),
    backend: "aws",
    backend_label: "AWS",
    alias: "team aws",
    identifier: "work",
    placement: {},
    status: "ERROR" as const,
    error_type: "ProviderNotAuthorizedError",
    error_message: "The SSO session for profile 'work' has expired",
    remediation: "run `aws sso login --profile work`",
    workspace_count: 0,
  };

  const SPARE_CONNECTION = {
    ...ERRORED_CONNECTION,
    id: "conn-2",
    instance: "aws_" + "2".repeat(32),
    status: "OK" as const,
    error_type: null,
    error_message: null,
    remediation: null,
    alias: "spare",
  };

  function drawOpenPanel(
    fetchImpl: (url: string, init?: RequestInit) => Promise<Response>,
    connections: UiConnectionEntry[] = [ERRORED_CONNECTION, SPARE_CONNECTION],
  ) {
    const shell = updatesShell({}, false);
    shell.stores.providers.applyProvidersMessage({
      type: "providers",
      accounts: [
        {
          user_id: "local",
          email: "",
          state: "SIGNED_IN",
          is_connection_owner_eligible: true,
          imbue_cloud: null,
          connections,
        },
      ],
      device_providers: [
        { name: "lima", backend: null, status: "ERROR", error_type: "DiscoveryError", error_message: "limactl list timed out" },
      ],
      last_event_at: null,
      last_full_snapshot_at: null,
    });
    shell.accountId = "local";
    const { view } = initLandingPage(fetchImpl as (url: string) => Promise<Response>);
    const header = collectVnodes(byId(view(), "landing-providers")).find((vnode) => vnode.tag === "button");
    (attrsOf(header!).onclick as () => void)();
    return { view, header: () => collectVnodes(byId(view(), "landing-providers")).find((vnode) => vnode.tag === "button") };
  }

  /** The connection row for `instance`, drawn the way the reader sees it. */
  function connectionRowFor(root: m.Children, instance: string): unknown {
    return renderDeep(
      withAttr(root, "data-connection-instance").find(
        (row) => attrsOf(row)["data-connection-instance"] === instance,
      ),
    );
  }

  function deviceRowFor(root: m.Children, name: string): unknown {
    return renderDeep(
      withAttr(root, "data-provider-name").find(
        (row) => attrsOf(row)["data-provider-name"] === name,
      ),
    );
  }

  const PROFILE_MISSING_CONNECTION = {
    ...ERRORED_CONNECTION,
    id: "conn-3",
    instance: "aws_" + "3".repeat(32),
    status: "PROFILE_MISSING" as const,
    error_type: "ProviderCredentialSourceMissingError",
    error_message: "AWS profile 'home' is not configured on this machine",
    remediation: "run `aws configure --profile home`",
  };

  it("offers a report on a failed check's row, but not on a login to set up, whose own Check again is the next step", () => {
    const profileMissing = PROFILE_MISSING_CONNECTION;
    const { view } = drawOpenPanel(
      recordingFetch(() => Promise.resolve(jsonResponse(settled))).fetchImpl,
      [ERRORED_CONNECTION, profileMissing],
    );
    const root = view();

    expect(
      allText(connectionRowFor(root, ERRORED_CONNECTION.instance)),
    ).toContain("Report a bug");
    expect(allText(deviceRowFor(root, "lima"))).toContain("Report a bug");
    const setupRow = connectionRowFor(root, profileMissing.instance);
    const setupNotice = allText(
      byId(setupRow, `connection-problem-${profileMissing.instance}`),
    );
    expect(setupNotice).toContain("not set up on this computer");
    expect(setupNotice).toContain("Check again");
    expect(allText(setupRow)).not.toContain("Report a bug");
  });

  it.each([
    ["a login to set up", PROFILE_MISSING_CONNECTION, ["Check again"]],
    ["a failed check", ERRORED_CONNECTION, ["Check again", "Report a bug"]],
  ])("offers %s a Check again that waits on the login, and checks the connection again", (_, connection, labels) => {
    const { instance } = connection;
    const { fetchImpl, requests } = recordingFetch(() => Promise.resolve(jsonResponse(settled)));
    const { view } = drawOpenPanel(fetchImpl, [connection]);
    const noticeButtons = () =>
      collectVnodes(byId(connectionRowFor(view(), instance), `connection-problem-${instance}`)).filter(
        (vnode) => vnode.tag === "button",
      );

    expect(
      noticeButtons()
        .map(allText)
        .filter((label) => !label.includes("See details")),
    ).toEqual(labels);
    // Both rows ask the user to run a login first; Check again only confirms
    // it, so nothing here is the dark button.
    expect(
      noticeButtons()
        .filter((button) => classesOf(button).includes(BTN_VARIANTS.primary))
        .map(allText),
    ).toEqual([]);

    const checkAgain = byId(noticeButtons(), `connection-recheck-${instance}`);
    (attrsOf(checkAgain!).onclick as () => void)();

    expect(requests).toContain(`/api/v1/desktop/cloud-accounts/${instance}/recheck`);
    expect(attrsOf(byId(noticeButtons(), `connection-recheck-${instance}`)!).disabled).toBe(true);
  });

  it("counts the rows needing attention in the header, leads each row with the provider's reason, and keeps the raw error behind See details", () => {
    const { view, header } = drawOpenPanel(recordingFetch(() => Promise.resolve(jsonResponse(settled))).fetchImpl);
    const root = view();

    expect(allText(header())).toContain("3 providers, 2 need attention");
    const connectionRow = allText(
      connectionRowFor(root, ERRORED_CONNECTION.instance),
    );
    expect(connectionRow).toContain(
      "The SSO session for profile 'work' has expired.",
    );
    // The fix is set apart and the command it names is set as code, so
    // neither the asterisks nor the backticks are text on screen.
    expect(connectionRow).toContain("To fix it,");
    expect(connectionRow).toContain("aws sso login --profile work");
    expect(connectionRow).not.toContain("`aws sso login");
    expect(connectionRow).not.toContain("*run");
    expect(connectionRow).not.toContain("ProviderNotAuthorizedError");
    const deviceRow = allText(deviceRowFor(root, "lima"));
    expect(deviceRow).toContain("limactl list timed out.");
    expect(deviceRow).not.toContain("DiscoveryError");
    const notices = withAttr(byId(root, "landing-providers"), "error").map(
      (vnode) => attrsOf(vnode).error as { details: string },
    );
    expect(notices.map((error) => error.details)).toEqual([
      "ProviderNotAuthorizedError: The SSO session for profile 'work' has expired",
      "DiscoveryError: limactl list timed out",
    ]);
  });

  it("shows a refused removal's own sentence in the styled dialog and lets the row be removed again", async () => {
    vi.stubGlobal("window", { confirm: () => true });
    const refusal = "This connection still has 1 active workspace. Remove it first, then remove the connection.";
    const fetchImpl = (url: string, init?: RequestInit): Promise<Response> =>
      Promise.resolve(
        init?.method === "DELETE" ? jsonResponse({ error: refusal, code: "connection_in_use" }, 409) : jsonResponse(settled),
      );
    const dialogs = withDialogHost();
    try {
      const { view } = drawOpenPanel(fetchImpl);
      const removeButton = () =>
        collectVnodes(connectionRowFor(view(), ERRORED_CONNECTION.instance)).find(
          (vnode) => vnode.tag === "button" && allText(vnode) === "Remove",
        );

      (attrsOf(removeButton()!).onclick as () => void)();
      expect(attrsOf(removeButton()!).disabled).toBe(true);
      await vi.waitFor(() => expect(dialogs.current()).not.toBeNull());

      expect(dialogs.current()?.error.message).toBe(refusal);
      expect(dialogs.current()?.error.details).toContain("HTTP 409");
      expect(attrsOf(removeButton()!).disabled).toBe(false);
    } finally {
      dialogs.release();
    }
  });

  it("says in the styled dialog that the connection form couldn't open, with the failed request behind See details", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const fetchImpl = (url: string): Promise<Response> =>
      Promise.resolve(url.includes("form-defaults") ? jsonResponse({ error: "boom" }, 500) : jsonResponse(settled));
    const dialogs = withDialogHost();
    try {
      const { view } = drawOpenPanel(fetchImpl);
      const addButton = collectVnodes(byId(view(), "landing-providers")).find(
        (vnode) => allText(vnode) === "Add connection" && typeof attrsOf(vnode).onclick === "function",
      );

      (attrsOf(addButton!).onclick as () => void)();
      await vi.waitFor(() => expect(dialogs.current()).not.toBeNull());

      expect(dialogs.current()?.title).toBe("Couldn't open the connection form");
      expect(dialogs.current()?.error.message).toBe(
        "Try again. If this persists, report a bug.",
      );
      expect(dialogs.current()?.error.details).toContain("failed (500)");
    } finally {
      dialogs.release();
    }
  });
});

describe("the providers panel's connection actions", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
  const CONNECTION: UiConnectionEntry = {
    id: "conn-1",
    instance: "modal_" + "0".repeat(32),
    backend: "modal",
    backend_label: "Modal",
    alias: "team modal",
    identifier: "ak…1234",
    placement: {},
    status: "PROFILE_MISSING",
    workspace_count: 0,
  };

  afterEach(() => {
    while (errorDialogs.current !== null) errorDialogs.dismiss();
    errorDialogs.detachHost();
  });

  /** Alice's hub with her one connection listed and the providers panel open; `answer` serves the connection routes. */
  function openPanel(answer: () => Response): () => m.Vnode {
    errorDialogs.attachHost();
    const shell = updatesShell({}, false);
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], ALICE.user_id));
    shell.accountId = ALICE.user_id;
    shell.stores.providers.applyProvidersMessage({
      type: "providers",
      accounts: [
        {
          user_id: ALICE.user_id,
          email: ALICE.email,
          state: "SIGNED_IN",
          is_connection_owner_eligible: true,
          imbue_cloud: null,
          connections: [CONNECTION],
        },
      ],
      device_providers: [],
      last_event_at: null,
      last_full_snapshot_at: null,
    });
    const { view } = initLandingPage(() => Promise.resolve(jsonResponse(settled)));
    vi.stubGlobal("fetch", (url: string) =>
      Promise.resolve(url.includes("/cloud-accounts/") ? answer() : jsonResponse(settled)),
    );
    const toggle = collectVnodes(byId(view(), "landing-providers")).find((node) => node.tag === "button");
    (attrsOf(toggle!).onclick as () => void)();
    return view;
  }

  /** Press the row's button labelled `label`; the row's notice draws its actions only once rendered. */
  function press(view: () => m.Vnode, label: string): void {
    const button = collectVnodes(renderDeep(view())).find(
      (node) => node.tag === "button" && allText(node) === label && attrsOf(node).onclick,
    );
    (attrsOf(button!).onclick as () => void)();
  }

  it("tells a re-check the app refused in the styled dialog, with the answer behind See details", async () => {
    const alert = vi.fn();
    vi.stubGlobal("window", { alert });
    const view = openPanel(() => jsonResponse({ error: `Unknown cloud account '${CONNECTION.instance}'.` }, 404));

    press(view, "Check again");

    await vi.waitFor(() => expect(errorDialogs.current).not.toBeNull());
    expect(errorDialogs.current?.title).toBe("Couldn't check team modal again");
    expect(errorDialogs.current?.error).toEqual({
      message: "Try again. If this persists, report a bug.",
      details: `HTTP 404\nUnknown cloud account '${CONNECTION.instance}'.`,
    });
    expect(alert).not.toHaveBeenCalled();
  });

  it("says why a connection still in use can't be removed, in the route's words", async () => {
    vi.stubGlobal("window", { confirm: () => true });
    const inUse = "A workspace is still on this connection. Remove it first, then try again.";
    const view = openPanel(() => jsonResponse({ error: inUse, code: "connection_in_use" }, 409));

    press(view, "Remove");

    await vi.waitFor(() => expect(errorDialogs.current).not.toBeNull());
    expect(errorDialogs.current?.error.message).toBe(inUse);
    expect(errorDialogs.current?.error.details).toContain("HTTP 409");
  });

  it("says to update Imbue Studio when a newer version wrote the connection", async () => {
    vi.stubGlobal("window", { confirm: () => true });
    const tooNew = "This connection was added by a newer version of the app; update the app to manage it.";
    const view = openPanel(() => jsonResponse({ error: tooNew, code: "connection_too_new" }, 409));

    press(view, "Remove");

    await vi.waitFor(() => expect(errorDialogs.current).not.toBeNull());
    expect(errorDialogs.current?.title).toBe("Couldn't remove team modal");
    expect(errorDialogs.current?.error.message).toBe(
      "A newer version of Imbue Studio added team modal, so this version can't remove it. " +
        "*Update Imbue Studio, then try again.*",
    );
    expect(errorDialogs.current?.error.details).toContain(tooNew);
  });

  it("names the connection for any other refused removal, keeping the server's text behind See details", async () => {
    vi.stubGlobal("window", { confirm: () => true });
    const view = openPanel(() => jsonResponse({ error: "Could not write the provider settings." }, 500));

    press(view, "Remove");

    await vi.waitFor(() => expect(errorDialogs.current).not.toBeNull());
    expect(errorDialogs.current?.title).toBe("Couldn't remove team modal");
    expect(errorDialogs.current?.error).toEqual({
      message: "Try again. If this persists, report a bug.",
      details: "HTTP 500\nCould not write the provider settings.",
    });
  });

  it.each(["Check again", "Remove"])(
    "keeps the app-wide 500's plain text behind See details when %s fails",
    async (label) => {
      vi.stubGlobal("window", { confirm: () => true });
      const view = openPanel(() => new Response("Internal Server Error: OSError('disk full')", { status: 500 }));

      press(view, label);

      await vi.waitFor(() => expect(errorDialogs.current).not.toBeNull());
      expect(errorDialogs.current?.title).toMatch(/^Couldn't (check|remove) team modal/);
      expect(errorDialogs.current?.error.message).toBe(
        "Try again. If this persists, report a bug.",
      );
      expect(errorDialogs.current?.error.details).toBe("HTTP 500\nInternal Server Error: OSError('disk full')");
    },
  );
});
