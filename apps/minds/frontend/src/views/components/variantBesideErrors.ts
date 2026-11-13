// A panel's own buttons beside an error shown on the same panel: the error's
// sure fix is the one dark button there, so the panel's primary steps down.

import type { ErrorAction } from "../../models/errors";
import type { ButtonVariant } from "./constants";
import { isFollowUpLed } from "./errorDesign";

/** An error drawn on the panel, as the rule weighs it: its sentence decides
 * whether its lead button is a fix or only a follow-up to an ask the sentence
 * makes. */
export interface ShownError {
  message: string;
  actions?: readonly ErrorAction[];
}

/** A panel's own button variant, given the errors shown on the panel: while
 * one of those errors leads its row with a main button, a primary is drawn
 * secondary. */
export function variantBesideErrors(
  variant: ButtonVariant,
  shownErrors: readonly ShownError[],
): ButtonVariant {
  return variant === "primary" && shownErrors.some(isLedByMainButton)
    ? "secondary"
    : variant;
}

/** The same lead errorActionButtons draws primary: the first fix (a way out is
 * a ghost after the fixes, and a destructive fix is drawn in the danger
 * colour), unless the sentence asks for something that fix only confirms. */
function isLedByMainButton(error: ShownError): boolean {
  const lead = (error.actions ?? []).find(
    (action) => action.isDismissal !== true,
  );
  return (
    lead !== undefined &&
    lead.isDestructive !== true &&
    !isFollowUpLed(error.message, lead.label)
  );
}
