// What a create held on the emailed verification link shows: why it is
// waiting, the resend, and the way out.

import m from "mithril";
import type { ResendOutcome } from "../../../models/emailVerification";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import type { VerificationWait } from "./verification-wait";

// Only an outcome the server confirmed may point the user at an inbox.
const RESEND_NOTICE_BY_OUTCOME: Record<ResendOutcome, string> = {
  sent: "Sent another email.",
  suppressed: "An email went out moments ago; check your inbox.",
  failed: "Could not send the email. Please try again.",
  already_verified: "This email is already verified.",
};

interface VerificationWaitNoticeAttrs {
  wait: VerificationWait;
  onCancel: () => void;
}

export const VerificationWaitNotice: m.Component<VerificationWaitNoticeAttrs> = {
  view({ attrs: { wait, onCancel } }) {
    const email = wait.email;
    if (email === null) return null;
    const outcome = wait.resendOutcome;
    const resendNotice = outcome === null ? null : RESEND_NOTICE_BY_OUTCOME[outcome];
    return m(Notice, { variant: "info", id: "verification-wait", role: "status", extra: "!mt-6 !mb-0 !px-5 !py-4 text-center" }, [
      m("p", { class: "type-body text-primary" }, [
        "Workspaces on Imbue Cloud require a verified email. Click the link emailed to ",
        m("strong", email),
        " when you created your account, and your workspace will be created.",
      ]),
      m("div", { class: "mt-3 flex justify-center gap-3" }, [
        outcome === "already_verified"
          ? null
          : m(Button, { variant: "secondary", id: "verification-resend", onclick: () => wait.resend() }, "Resend email"),
        m(Button, { variant: "ghost", id: "verification-cancel", onclick: onCancel }, "Cancel"),
      ]),
      resendNotice === null ? null : m("p", { class: "mt-2 type-helper text-secondary" }, resendNotice),
    ]);
  },
};
