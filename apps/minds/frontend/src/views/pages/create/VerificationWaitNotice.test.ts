// What a create held on the emailed link says, and what its controls do.

import { describe, expect, it, vi } from "vitest";
import type { ResendOutcome } from "../../../models/emailVerification";
import { allText, attrsOf, collectVnodes, renderRoot, renderedText } from "../../../testing";
import { VerificationWaitNotice } from "./VerificationWaitNotice";
import { VerificationWait } from "./verification-wait";

const EMAIL = "a@b.com";

function heldWait(resend: (email: string) => Promise<ResendOutcome> = async () => "sent"): VerificationWait {
  const wait = new VerificationWait({
    redraw: () => undefined,
    bringAppToFront: () => undefined,
    resendVerificationEmail: resend,
  });
  wait.email = EMAIL;
  return wait;
}

function renderNotice(wait: VerificationWait, onCancel: () => void = () => undefined) {
  return renderRoot(() => VerificationWaitNotice, { wait, onCancel });
}

describe("VerificationWaitNotice", () => {
  it("draws nothing while no create is held", () => {
    const wait = heldWait();
    wait.email = null;
    expect(renderNotice(wait)).toBeNull();
  });

  it("names the address and reports no resend before one is pressed", () => {
    const text = renderedText(renderNotice(heldWait()));
    expect(text).toContain(EMAIL);
    expect(text).not.toContain("Sent another email.");
    expect(text).not.toContain("moments ago");
  });

  it.each<[ResendOutcome, string]>([
    ["sent", "Sent another email."],
    ["suppressed", "An email went out moments ago; check your inbox."],
    ["failed", "Could not send the email. Please try again."],
    ["already_verified", "This email is already verified."],
  ])("says what became of a resend that ended %s, offering another unless the address is verified", (outcome, sentence) => {
    const wait = heldWait();
    wait.resendOutcome = outcome;
    const text = renderedText(renderNotice(wait));
    expect(text).toContain(sentence);
    expect(text.includes("Resend email")).toBe(outcome !== "already_verified");
    expect(text).toContain("Cancel");
  });

  it("sends through the wait on Resend and hands Cancel to the page", () => {
    const resend = vi.fn(async (): Promise<ResendOutcome> => "sent");
    const onCancel = vi.fn();
    const controls = new Map(
      collectVnodes(renderNotice(heldWait(resend), onCancel))
        .filter((vnode) => typeof attrsOf(vnode).onclick === "function")
        .map((vnode) => [allText(vnode.children).trim(), attrsOf(vnode).onclick as () => void]),
    );

    controls.get("Resend email")?.();
    expect(resend).toHaveBeenCalledWith(EMAIL);
    expect(onCancel).not.toHaveBeenCalled();

    controls.get("Cancel")?.();
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
