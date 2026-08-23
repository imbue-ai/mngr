// The Accounts card's plan switcher: switching TO explorer is the analytics
// consent, so the Switch plan button must stay refused until the explicit
// agreement box is checked -- and a new dropdown pick must invalidate a
// previously-checked agreement.

import m from "mithril";
import { describe, expect, it } from "vitest";
import type { AccountEntry } from "../../../models/accountsDetail";
import { AccountsDetailModel } from "../../../models/accountsDetail";
import type { AnyVnode } from "../../../testing";
import { attrsOf, collectText, collectVnodes } from "../../../testing";
import { AccountCard } from "./AccountCard";

const ACCOUNT: AccountEntry = {
  user_id: "user-1",
  email: "person@example.com",
  workspace_count: 0,
  is_default: true,
  is_enabled: true,
};

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
  };
  state.privacyPolicyUrl = PRIVACY_POLICY_URL;
  return model;
}

/** One mounted AccountCard whose re-renders keep the closure's draft state
 * (the picked plan and the checked agreement), the way mithril redraws do --
 * `renderRoot` would instantiate a fresh closure per call and lose it. */
function mountCard(model: AccountsDetailModel): () => m.Vnode {
  const instance = AccountCard() as unknown as m.Component;
  return () =>
    (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
      instance,
      m(instance, { model, account: ACCOUNT } as m.Attributes) as m.Vnode,
    );
}

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
