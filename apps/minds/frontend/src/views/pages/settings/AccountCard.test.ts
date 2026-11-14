// The Accounts card's plan switcher: switching TO explorer is the analytics
// consent, so the Switch plan button must stay refused until the explicit
// agreement box is checked -- and a new dropdown pick must invalidate a
// previously-checked agreement.

import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  registerAppContext,
} from "../../../app-context";
import type { UiAccountEntry } from "../../../channel/messages";
import type { AccountReadFailure } from "../../../models/accountsDetail";
import { AccountsDetailModel } from "../../../models/accountsDetail";
import { createEmptyStores } from "../../../models/boot";
import { webLogin } from "../../../models/webLogin";
import { ShellState } from "../../shell/shell-state";
import type { AnyVnode } from "../../../testing";
import {
  accountEntry,
  attrsOf,
  collectText,
  collectVnodes,
  renderDeep,
  withMindsNative,
} from "../../../testing";
import { Spinner } from "../../components/Spinner";
import { AccountCard } from "./AccountCard";

const ACCOUNT = accountEntry({ email: "person@example.com" });

const PRIVACY_POLICY_URL = "https://accounts.example.com/privacy-policy";

function modelOnPlan(planName: string): AccountsDetailModel {
  const model = new AccountsDetailModel(undefined, () => {});
  const state = model.planStateFor(ACCOUNT.user_id);
  state.isLoaded = true;
  state.planView = {
    plan_name: planName,
    plan_display_name: planName.charAt(0).toUpperCase() + planName.slice(1),
    available_plans: ["free", "explorer"],
    usage_rows: [],
    is_over_storage_quota: false,
    is_at_bucket_quota: false,
    is_sharing_workspaces: false,
  };
  state.privacyPolicyUrl = PRIVACY_POLICY_URL;
  return model;
}

/** One mounted AccountCard whose re-renders keep the closure's draft state
 * (the picked plan and the checked agreement), the way mithril redraws do --
 * `renderRoot` would instantiate a fresh closure per call and lose it. */
function mountCard(
  model: AccountsDetailModel,
  account: UiAccountEntry = ACCOUNT,
): () => m.Vnode {
  const instance = AccountCard() as unknown as m.Component;
  return () =>
    (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
      instance,
      m(instance, { model, account } as m.Attributes) as m.Vnode,
    );
}

afterEach(() => {
  clearAppContextForTests();
  vi.restoreAllMocks();
});

function switchPlanButton(root: m.Vnode): AnyVnode {
  const button = collectVnodes(root).find(
    (node) => collectText(node).join("").trim() === "Switch plan",
  );
  expect(button).toBeDefined();
  return button as AnyVnode;
}

function pickPlan(root: m.Vnode, planName: string): void {
  const select = collectVnodes(root).find(
    (node) => attrsOf(node).name === "plan",
  );
  expect(select).toBeDefined();
  const onchange = attrsOf(select as AnyVnode).onchange as (
    event: Event,
  ) => void;
  onchange({ target: { value: planName } } as unknown as Event);
}

function agreementCheckbox(root: m.Vnode): AnyVnode | undefined {
  return collectVnodes(root).find(
    (node) => attrsOf(node).id === "explorer-agreement-checkbox",
  );
}

function checkAgreement(root: m.Vnode): void {
  const checkbox = agreementCheckbox(root);
  expect(checkbox).toBeDefined();
  const onchange = attrsOf(checkbox as AnyVnode).onchange as (
    event: Event,
  ) => void;
  onchange({ target: { checked: true } } as unknown as Event);
}

describe("the plan switcher's explorer agreement", () => {
  it("refuses Switch plan until the agreement is checked, then allows it", () => {
    const render = mountCard(modelOnPlan("free"));
    pickPlan(render(), "explorer");

    const pending = render();
    expect(collectText(pending).join(" ")).toContain("share product data");
    expect(
      collectVnodes(pending).some(
        (node) => attrsOf(node).href === PRIVACY_POLICY_URL,
      ),
    ).toBe(true);
    expect(attrsOf(switchPlanButton(pending)).disabled).toBe(true);

    checkAgreement(pending);
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(false);
  });

  it("invalidates a checked agreement when the pick changes", () => {
    const render = mountCard(modelOnPlan("free"));
    pickPlan(render(), "explorer");
    checkAgreement(render());
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(false);

    pickPlan(render(), "free");
    pickPlan(render(), "explorer");
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(true);
  });

  it("needs no agreement for a non-explorer pick", () => {
    const render = mountCard(modelOnPlan("explorer"));
    pickPlan(render(), "free");

    const pending = render();
    expect(collectText(pending).join(" ")).toContain("1 free cloud workspace");
    expect(agreementCheckbox(pending)).toBeUndefined();
    expect(attrsOf(switchPlanButton(pending)).disabled).toBe(false);
  });

  it("refuses Switch plan while the pick equals the current plan", () => {
    const render = mountCard(modelOnPlan("free"));
    // The resting state (no pick yet) is the current plan.
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(true);

    // Picking away enables; picking back to the current plan disables again.
    pickPlan(render(), "explorer");
    checkAgreement(render());
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(false);
    pickPlan(render(), "free");
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(true);
  });

  it("shows a busy spinner and refuses clicks while a switch is in flight", () => {
    const model = modelOnPlan("explorer");
    const render = mountCard(model);
    pickPlan(render(), "free");
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(false);

    model.switchingPlanUserIds.add(ACCOUNT.user_id);
    const busy = collectVnodes(render()).find(
      (node) => collectText(node).join("").trim() === "Switching…",
    );
    expect(busy).toBeDefined();
    expect(attrsOf(busy as AnyVnode).disabled).toBe(true);

    model.switchingPlanUserIds.delete(ACCOUNT.user_id);
    expect(attrsOf(switchPlanButton(render())).disabled).toBe(false);
  });
});

describe("a cached plan being refreshed", () => {
  function refreshIndicator(root: m.Vnode): AnyVnode | undefined {
    return collectVnodes(root).find(
      (node) => attrsOf(node)["aria-label"] === "Refreshing plan and usage",
    );
  }

  it("shows a quiet spinner beside the plan while the fresh read is in flight", () => {
    const model = modelOnPlan("free");
    model.planStateFor(ACCOUNT.user_id).isRefreshing = true;
    const render = mountCard(model);

    const root = render();
    expect(refreshIndicator(root)).toBeDefined();
    expect(collectText(root).join(" ")).toContain("Limited");

    model.planStateFor(ACCOUNT.user_id).isRefreshing = false;
    expect(refreshIndicator(render())).toBeUndefined();
  });

  it("says the figures may be out of date when the refresh failed, with a Try again that re-reads", () => {
    const model = modelOnPlan("free");
    model.planStateFor(ACCOUNT.user_id).isRefreshFailed = true;
    const reads: string[] = [];
    vi.spyOn(model, "loadPlan").mockImplementation(async (userId) => {
      reads.push(userId);
    });

    const root = mountCard(model)();
    const text = collectText(root).join(" ");
    vnodeButtonLabelled(root, "Try again").click();

    expect(text).toContain("Couldn't refresh -- these figures may be out of date.");
    expect(text).toContain("Limited");
    expect(reads).toEqual([ACCOUNT.user_id]);
  });
});

/** The button in a rendered vnode tree whose text is exactly `label`. */
function vnodeButtonLabelled(root: m.Vnode, label: string): { click: () => void } {
  const button = collectVnodes(root).find(
    (node) =>
      (node.tag === "button" || attrsOf(node).onclick !== undefined) &&
      collectText(node).join("").trim() === label,
  );
  expect(button).toBeDefined();
  return { click: () => (attrsOf(button as AnyVnode).onclick as () => void)() };
}

describe("a plan that could not be read", () => {
  function modelWithPlanFailure(
    failure: AccountReadFailure,
  ): AccountsDetailModel {
    const model = new AccountsDetailModel(undefined, () => {});
    const state = model.planStateFor(ACCOUNT.user_id);
    state.isLoaded = true;
    state.isUnavailable = true;
    state.readFailure = failure;
    return model;
  }

  it("says so with a Try again that re-runs the read, and keeps the reason behind See details", () => {
    const model = modelWithPlanFailure({
      kind: "FAILED",
      details: "account show failed (exit 1)",
    });
    const reads: string[] = [];
    vi.spyOn(model, "loadPlan").mockImplementation(async (userId) => {
      reads.push(userId);
    });

    const root = renderDeep(mountCard(model)()) as m.Vnode;
    const text = collectText(root).join(" ");
    vnodeButtonLabelled(root, "Try again").click();

    expect(text).toContain(
      "Couldn't load your plan and usage. Try again. If this persists, report a bug.",
    );
    expect(text).not.toContain("could not reach Imbue Cloud");
    expect(text).toContain("Report a bug");
    expect(reads).toEqual([ACCOUNT.user_id]);
  });

  it("asks for a fresh sign-in when Imbue Cloud no longer accepts the account's", () => {
    const start = vi
      .spyOn(webLogin, "start")
      .mockImplementation(() => Promise.resolve());
    const root = renderDeep(
      mountCard(
        modelWithPlanFailure({ kind: "SESSION_REJECTED", details: "" }),
      )(),
    ) as m.Vnode;

    vnodeButtonLabelled(root, "Sign in").click();

    expect(collectText(root).join(" ")).toContain(
      "Your Imbue Cloud sign-in has expired.  Sign in again  to see your plan.",
    );
    expect(start).toHaveBeenCalledWith("", { isClosedOnSignIn: true });
  });
});

describe("plan and notification reads that failed", () => {
  function modelWithFailures(
    planFailure: AccountReadFailure,
    notificationFailure: AccountReadFailure,
  ): AccountsDetailModel {
    const model = new AccountsDetailModel(undefined, () => {});
    const plan = model.planStateFor(ACCOUNT.user_id);
    plan.isLoaded = true;
    plan.isUnavailable = true;
    plan.readFailure = planFailure;
    const preferences = model.notificationPreferencesFor(ACCOUNT.user_id);
    preferences.isLoaded = true;
    preferences.readFailure = notificationFailure;
    return model;
  }

  it("says a reason both share once, and Try again re-reads both", () => {
    const model = modelWithFailures(
      { kind: "FAILED", details: "account show failed (exit 1)" },
      { kind: "FAILED", details: "notification read failed (exit 1)" },
    );
    const reads: string[] = [];
    vi.spyOn(model, "loadPlan").mockImplementation(async () => {
      reads.push("plan");
    });
    vi.spyOn(model, "loadNotificationPreferences").mockImplementation(async () => {
      reads.push("notifications");
    });

    const root = renderDeep(mountCard(model)()) as m.Vnode;
    const text = collectText(root).join(" ");
    vnodeButtonLabelled(root, "Try again").click();

    const sentence =
      "Couldn't load your plan, usage and notification settings. Try again. If this persists, report a bug.";
    expect(text.split(sentence)).toHaveLength(2);
    expect(text).not.toContain("Couldn't load your plan and usage.");
    expect(text).not.toContain("Couldn't load your notification settings.");
    expect(reads).toEqual(["plan", "notifications"]);
  });

  it("keeps two notices when the reads failed for different reasons", () => {
    const text = collectText(
      renderDeep(
        mountCard(
          modelWithFailures({ kind: "SESSION_REJECTED", details: "" }, { kind: "FAILED", details: "" }),
        )(),
      ),
    ).join(" ");

    expect(text).toContain("Your Imbue Cloud sign-in has expired.  Sign in again  to see your plan.");
    expect(text).toContain("Couldn't load your notification settings.");
  });

  it("offers Update Imbue Studio in the desktop app when this version is too old for both", async () => {
    const shell = new ShellState(createEmptyStores());
    shell.accountId = "user-cd34";
    registerAppContext({ stores: shell.stores, shell });
    const routed = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await withMindsNative({}, async () => {
      const root = renderDeep(
        mountCard(
          modelWithFailures({ kind: "CLIENT_TOO_OLD", details: "" }, { kind: "CLIENT_TOO_OLD", details: "" }),
        )(),
      ) as m.Vnode;

      expect(collectText(root).join(" ")).toContain(
        "This version of Imbue Studio is too old to show your plan and notification settings.  Update Imbue Studio. ",
      );
      vnodeButtonLabelled(root, "Update Imbue Studio").click();
    });
    expect(routed).toHaveBeenCalledWith("/settings", { section: "updates", account: "user-cd34" });
  });
});

describe("the plan switcher's plan names", () => {
  it("offers the free plan as Limited and keeps its stored name", () => {
    const render = mountCard(modelOnPlan("explorer"));
    const options = collectVnodes(render()).filter(
      (node) => node.tag === "option",
    );
    const freeOption = options.find((node) => attrsOf(node).value === "free");
    expect(freeOption).toBeDefined();
    expect(collectText(freeOption).join("").trim()).toBe("Limited");
    expect(
      options.map((node) => collectText(node).join("").trim()),
    ).not.toContain("Free");
  });
});

describe("the email notifications switch", () => {
  function modelWithChannels(
    channels: { email_enabled: boolean; in_app_enabled: boolean } | null,
    isLoaded = true,
  ): AccountsDetailModel {
    const model = new AccountsDetailModel(undefined, () => {});
    const state = model.notificationPreferencesFor(ACCOUNT.user_id);
    state.isLoaded = isLoaded;
    state.channels = channels;
    return model;
  }

  function emailSwitch(root: m.Vnode): AnyVnode | undefined {
    return collectVnodes(root).find(
      (node) => attrsOf(node)["data-email-notifications"] !== undefined,
    );
  }

  it("reads the account's email channel, and flips it when pressed", () => {
    const model = modelWithChannels({
      email_enabled: true,
      in_app_enabled: true,
    });
    const flips: boolean[] = [];
    vi.spyOn(model, "setEmailNotifications").mockImplementation(
      async (_userId, isEnabled) => {
        flips.push(isEnabled);
      },
    );
    const root = mountCard(model)();

    const toggle = emailSwitch(root);
    expect(toggle).toBeDefined();
    expect(attrsOf(toggle as AnyVnode)["aria-checked"]).toBe("true");
    expect(attrsOf(toggle as AnyVnode)["aria-label"]).toBe(
      "Receive email notifications?",
    );
    expect(collectText(root)).toContain("Receive email notifications?");
    expect(collectText(root)).toContain("Yes");
    expect(collectText(root)).not.toContain("On");
    (attrsOf(toggle as AnyVnode).onclick as () => void)();

    expect(flips).toEqual([false]);
  });

  it("reads No beside a switch that is off, and asks for it on when pressed", () => {
    const model = modelWithChannels({
      email_enabled: false,
      in_app_enabled: true,
    });
    const flips: boolean[] = [];
    vi.spyOn(model, "setEmailNotifications").mockImplementation(
      async (_userId, isEnabled) => {
        flips.push(isEnabled);
      },
    );
    const root = mountCard(model)();

    const toggle = emailSwitch(root) as AnyVnode;
    expect(attrsOf(toggle)["aria-checked"]).toBe("false");
    expect(collectText(root)).toContain("No");
    expect(collectText(root)).not.toContain("Off");
    (attrsOf(toggle).onclick as () => void)();

    expect(flips).toEqual([true]);
  });

  it("spins in the switch's knob while the preference is written, and holds it", () => {
    const model = modelWithChannels({
      email_enabled: true,
      in_app_enabled: true,
    });
    const spinnersBefore = collectVnodes(mountCard(model)()).filter(
      (node) => node.tag === Spinner,
    ).length;
    model.notificationPreferencesFor(ACCOUNT.user_id).isWriting = true;

    const root = mountCard(model)();

    const toggle = emailSwitch(root) as AnyVnode;
    expect(attrsOf(toggle)["aria-busy"]).toBe("true");
    expect(attrsOf(toggle).disabled).toBe(true);
    expect(
      String(attrsOf(toggle).className ?? attrsOf(toggle).class),
    ).toContain("is-settling");
    expect(
      collectVnodes(root).filter((node) => node.tag === Spinner),
    ).toHaveLength(spinnersBefore);
  });

  it("says the settings could not be loaded rather than drawing a switch, and re-reads on Try again", () => {
    const model = modelWithChannels(null);
    const reads: string[] = [];
    vi.spyOn(model, "loadNotificationPreferences").mockImplementation(
      async (userId) => {
        reads.push(userId);
      },
    );
    const root = renderDeep(mountCard(model)()) as m.Vnode;

    vnodeButtonLabelled(root, "Try again").click();

    expect(emailSwitch(root)).toBeUndefined();
    expect(collectText(root).join(" ")).toContain(
      "Couldn't load your notification settings. Try again. If this persists, report a bug.",
    );
    expect(reads).toEqual([ACCOUNT.user_id]);
  });

  it("waits in words before the channels are known", () => {
    const root = mountCard(modelWithChannels(null, false))();

    expect(emailSwitch(root)).toBeUndefined();
    expect(collectText(root).join(" ")).toContain(
      "Loading notification settings",
    );
  });
});

describe("AccountCard backup cleanup", () => {
  it("says an unfinished cleanup in our words and offers it again", () => {
    // An over-quota plan links its destroyed-workspace backups through the registered shell.
    const stores = createEmptyStores();
    registerAppContext({ stores, shell: new ShellState(stores) });
    const model = modelOnPlan("free");
    const state = model.planStateFor(ACCOUNT.user_id);
    state.planView = { ...state.planView!, is_over_storage_quota: true };
    state.trimStatus = {
      is_running: false,
      detail: "Backup cleanup didn't finish. Try again. If this persists, report a bug.",
      error_detail: "restic forget failed (exit 1): repository is locked",
    };
    const trimmed: string[] = [];
    vi.spyOn(model, "trimBackups").mockImplementation(async (userId: string) => {
      trimmed.push(userId);
    });

    const notice = collectVnodes(mountCard(model)()).find(
      (node) => attrsOf(node).id === `trim-error-${ACCOUNT.user_id}`,
    );
    expect(notice).toBeDefined();
    const noticeAttrs = attrsOf(notice!) as {
      error: { message: string; details: string };
      actions: Array<{ label: string; onclick: () => void }>;
    };
    expect(noticeAttrs.error.details).toContain("repository is locked");
    const [tryAgain] = noticeAttrs.actions;
    expect(tryAgain.label).toBe("Try again");
    tryAgain.onclick();
    expect(trimmed).toEqual([ACCOUNT.user_id]);
  });
});

describe("the shared-workspaces link", () => {
  it("is offered only while the account holds a share", () => {
    // The link is the window's account's hub page, read off the registered shell.
    const stores = createEmptyStores();
    registerAppContext({ stores, shell: new ShellState(stores) });
    const model = modelOnPlan("free");
    const state = model.planStateFor(ACCOUNT.user_id);
    expect(
      collectVnodes(mountCard(model)()).some(
        (node) => attrsOf(node).id === "account-shared-workspaces-link",
      ),
    ).toBe(false);

    state.planView = { ...state.planView!, is_sharing_workspaces: true };
    const link = collectVnodes(mountCard(model)()).find(
      (node) => attrsOf(node).id === "account-shared-workspaces-link",
    );
    expect(link).toBeDefined();
    expect(attrsOf(link as AnyVnode).href).toBe(
      "/account/local/workspaces/shared",
    );
  });
});
