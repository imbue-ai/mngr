import { describe, expect, it } from "vitest";
import type { ErrorAction } from "../../models/errors";
import { variantBesideErrors } from "./variantBesideErrors";

const FAILED = "Couldn't turn sharing on.";
/** A sentence that asks the user to do something a Retry only confirms. */
const ASKS_FIRST =
  "Couldn't create my-project. To fix it, *start Docker, then select Retry*.";

const tryAgain: ErrorAction = { label: "Try again", onclick: () => undefined };
const retry: ErrorAction = { label: "Retry", onclick: () => undefined };
const deny: ErrorAction = {
  label: "Deny",
  onclick: () => undefined,
  isDestructive: true,
};
const dismiss: ErrorAction = {
  label: "Dismiss",
  onclick: () => undefined,
  isDismissal: true,
};

describe("variantBesideErrors", () => {
  it("steps a primary down only while a shown error leads with a main button", () => {
    expect(variantBesideErrors("primary", [])).toBe("primary");
    expect(variantBesideErrors("primary", [{ message: FAILED }])).toBe(
      "primary",
    );
    // A row led by a Deny draws no primary, so the panel keeps its own.
    expect(
      variantBesideErrors("primary", [
        { message: FAILED, actions: [deny, tryAgain] },
      ]),
    ).toBe("primary");
    expect(
      variantBesideErrors("primary", [
        { message: FAILED, actions: [deny] },
        { message: FAILED, actions: [tryAgain] },
      ]),
    ).toBe("secondary");
    expect(
      variantBesideErrors("danger", [{ message: FAILED, actions: [tryAgain] }]),
    ).toBe("danger");
  });

  it("keeps the panel's primary when the error's own lead waits on an ask", () => {
    // Retry is drawn secondary there, so stepping the panel's button down
    // would leave the panel with no dark button at all.
    expect(
      variantBesideErrors("primary", [
        { message: ASKS_FIRST, actions: [retry] },
      ]),
    ).toBe("primary");
  });

  it("reads past a way out, which is drawn as a ghost rather than as the lead", () => {
    // Only a Dismiss: the error's row has no main button for the panel's own
    // to step aside for.
    expect(
      variantBesideErrors("primary", [{ message: FAILED, actions: [dismiss] }]),
    ).toBe("primary");
    expect(
      variantBesideErrors("primary", [
        { message: FAILED, actions: [dismiss, tryAgain] },
      ]),
    ).toBe("secondary");
    expect(
      variantBesideErrors("primary", [
        { message: FAILED, actions: [dismiss, deny] },
      ]),
    ).toBe("primary");
  });
});
