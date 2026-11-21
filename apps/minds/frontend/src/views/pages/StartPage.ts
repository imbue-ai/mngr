// The first run's sign-in page (/start, specs/ftue-onboarding-v2): one line,
// Sign in, and "Use without an account". Sign in opens the browser sign-in;
// the account that lands either has workspaces (its home page) or gets its
// first cloud workspace started at once, after the email-verification gate
// the connector requires, and the page hands over to the creation page, which
// welcomes the user while the workspace is built. "Use without an account"
// opens the create form on its run-on-this-computer preset.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { electronBridge } from "../../electron-bridge";
import type { CreateFormDefaults, CreateSubmitResult } from "../../models/create";
import { fetchCreateFormDefaults, submitCreateRequest } from "../../models/create";
import {
  RESEND_EMAIL_LABEL,
  VERIFICATION_POLL_MS,
  fetchIsEmailVerified,
  resendVerificationEmail,
  verificationEmailSentNote,
} from "../../models/emailVerification";
import { fallbackErrorSentence } from "../../models/errors";
import type { FirstRunAccount, FirstRunStage } from "../../models/firstRun";
import {
  SIGN_IN_STAGE,
  accountOf,
  cancelSignIn,
  firstNameOf,
  firstRun,
  landAccount,
  noteResend,
  refuseCreate,
  retryCreate,
  settleLanding,
  settleVerification,
  startSignIn,
  welcomeHeading,
} from "../../models/firstRun";
import { REPORTING_DETAIL_BODY } from "../../models/manifesto";
import {
  REPORTING_CONSENT_QUESTION,
  fetchSigninLanding,
  markOnboardingComplete,
  recordErrorReportingConsent,
} from "../../models/onboarding";
import { webLogin } from "../../models/webLogin";
import { accountHubRoute } from "../shell/classify";
import { createFormModal } from "./CreatePage";
import { CreateFormModel, createSubmitRefusal } from "./create/form-model";

export const SIGN_IN_LINE_LEAD = "Reimagine";
export const SIGN_IN_LINE_REST = " your personal computer";
export const SIGN_IN_LABEL = "Sign in";
export const WITHOUT_ACCOUNT_LABEL = "Use without an account";
export const REPORTING_LABEL = "Help improve Imbue Studio";
const PRIVACY_POLICY_URL = "https://imbue.com/privacy/";
export const SIGN_IN_MESSAGE = "Sign in to Imbue Studio, or create an account.";
export const SETTING_UP_LINE = "Just a sec, we're setting up your workspace";
export const VERIFY_LINE = "Check your email";
export const TRY_AGAIN_LABEL = "Try again";

/** How long the landing waits on the first record fetch before treating the account as new. */
export const SIGNIN_LANDING_WAIT_MS = 15000;
export const SIGNIN_LANDING_POLL_MS = 1000;

/**
 * Whether a cached defaults payload can still back a cloud create under the
 * account. The page reads the defaults when it mounts, before anyone has
 * signed in; a payload read under another account (or none) cannot name this
 * one for the create to run under.
 */
export function areDefaultsStale(defaults: CreateFormDefaults | null, accountId: string): boolean {
  return defaults === null || defaults.account?.user_id !== accountId;
}

/**
 * What the page says when the cloud create is refused: the refusal's own
 * sentence, which the create form shows too, or the fallback when it carries
 * none. A request that got no answer may still have been taken, so it claims
 * nothing about what was created.
 */
export function cloudCreateRefusal(result: CreateSubmitResult): string {
  if (result.status === 0) return "Imbue Studio didn't respond.";
  const reason = createSubmitRefusal(result, "").message;
  if (reason === "") return fallbackErrorSentence("Imbue Studio couldn't set up your workspace");
  return `Imbue Studio couldn't set up your workspace: ${/[.!?]$/.test(reason) ? reason : `${reason}.`}`;
}

/**
 * The first workspace's create under the account, submitted without the form, as an onboarding create: the
 * create form's remote preset, or its local one when the defaults say the first run is local.
 */
export function cloudCreateBody(defaults: CreateFormDefaults, accountId: string): Record<string, unknown> {
  const model = new CreateFormModel();
  model.applyDefaults(defaults);
  model.accountId = accountId;
  model.applyPreset(defaults.is_first_run_local === true ? "local" : "remote");
  model.isOnboardingDrivenByAgent = true;
  return { ...model.submitBody() };
}

/** The account a finished browser sign-in reported, else the one the accounts frame lists; null if neither names one. */
function landedAccount(): FirstRunAccount | null {
  const entries = getAppContext().stores.accounts.accounts;
  const reportedEmail = webLogin.email;
  const listed = entries.find((entry) => reportedEmail !== "" && entry.email === reportedEmail) ?? entries[0];
  const userId = webLogin.userId !== "" ? webLogin.userId : (listed?.user_id ?? "");
  if (userId === "") return null;
  const entry = entries.find((candidate) => candidate.user_id === userId) ?? listed;
  return {
    userId,
    email: reportedEmail !== "" ? reportedEmail : (entry?.email ?? ""),
    displayName: entry?.display_name ?? "",
  };
}

export const StartPage: m.ClosureComponent = () => {
  let stage: FirstRunStage = SIGN_IN_STAGE;
  let defaults: CreateFormDefaults | null = null;
  let isFormOpen = false;
  // The account ids signed in when Sign in was pressed: one appearing later is the sign-in landing.
  let accountIdsAtSignIn = new Set<string>();
  let landingTimer: ReturnType<typeof setTimeout> | null = null;
  let verificationTimer: ReturnType<typeof setInterval> | null = null;
  let isCheckingVerification = false;
  let isRemoved = false;
  let isReportingAllowed = true;
  let isReportingDetailOpen = false;

  function setStage(next: FirstRunStage): void {
    stage = next;
    m.redraw();
  }

  function stopTimers(): void {
    if (landingTimer !== null) clearTimeout(landingTimer);
    if (verificationTimer !== null) clearInterval(verificationTimer);
    landingTimer = null;
    verificationTimer = null;
  }

  function loadDefaults(): void {
    fetchCreateFormDefaults(null, getAppContext().shell.accountId, true)
      .then((loaded) => {
        defaults = loaded;
      })
      .catch(() => undefined);
  }

  /** Either way on from this page records the checkbox as it stands, before anything else happens. */
  function saveReportingAnswer(): void {
    void recordErrorReportingConsent(isReportingAllowed).then((isRecorded) => {
      if (!isRecorded) console.warn("The error-reporting answer could not be saved");
    });
  }

  function signIn(): void {
    saveReportingAnswer();
    accountIdsAtSignIn = new Set(getAppContext().stores.accounts.accounts.map((entry) => entry.user_id));
    setStage(startSignIn(stage));
    void webLogin.start(SIGN_IN_MESSAGE);
  }

  /** While the browser sign-in is open, an account landing moves the page on; the modal closing without one goes back. */
  function syncSignIn(): void {
    if (stage.kind !== "signing-in") return;
    const accounts = getAppContext().stores.accounts.accounts;
    const isNewAccountListed = accounts.some((entry) => !accountIdsAtSignIn.has(entry.user_id));
    if (webLogin.state === "done" || (isNewAccountListed && !webLogin.isOpen)) {
      const account = landedAccount();
      webLogin.noteSignedIn();
      webLogin.dismiss();
      if (account === null) {
        // Nothing names the account that landed, so no create can be owed under it.
        void markOnboardingComplete();
        getAppContext().shell.routeToHub();
        return;
      }
      getAppContext().shell.adoptAccount(account.userId);
      stage = landAccount(stage, account);
      beginLanding(account);
      return;
    }
    if (!webLogin.isOpen) stage = cancelSignIn(stage);
  }

  function beginLanding(account: FirstRunAccount): void {
    const deadline = Date.now() + SIGNIN_LANDING_WAIT_MS;
    const poll = async (): Promise<void> => {
      landingTimer = null;
      let verdict: Awaited<ReturnType<typeof fetchSigninLanding>> | null = null;
      try {
        verdict = await fetchSigninLanding(account.userId);
      } catch {
        // An unreachable backend is asked again until the deadline.
      }
      if (isRemoved || stage.kind !== "landing") return;
      if (verdict !== null && verdict.state === "done") {
        settleLandingWith(verdict.workspace_count > 0);
        return;
      }
      // A failed record fetch and a wait that runs out both read as a new
      // account: a first workspace strands nobody, while the bare home page
      // would skip the setup.
      if ((verdict !== null && verdict.state === "failed") || Date.now() >= deadline) {
        settleLandingWith(false);
        return;
      }
      landingTimer = setTimeout(() => void poll(), SIGNIN_LANDING_POLL_MS);
    };
    void poll();
  }

  function settleLandingWith(hasWorkspaces: boolean): void {
    if (stage.kind !== "landing") return;
    if (hasWorkspaces) {
      void markOnboardingComplete();
      m.route.set(accountHubRoute(stage.account.userId));
      return;
    }
    setStage(settleLanding(stage, false));
    checkVerification();
  }

  /**
   * The connector refuses a cloud create for an unverified email, so the page
   * checks first and, when the answer is no, waits on the emailed link. A
   * check that cannot be made does not block: the create itself will say what
   * is wrong.
   */
  function checkVerification(): void {
    const account = accountOf(stage);
    if (account === null || isCheckingVerification) return;
    isCheckingVerification = true;
    fetchIsEmailVerified(account.email).then(
      (isVerified) => {
        isCheckingVerification = false;
        applyVerification(isVerified);
      },
      () => {
        isCheckingVerification = false;
        applyVerification(true);
      },
    );
  }

  function applyVerification(isVerified: boolean): void {
    if (isRemoved) return;
    const wasWaiting = stage.kind === "verify";
    setStage(settleVerification(stage, isVerified));
    if (stage.kind === "verify" && verificationTimer === null) {
      verificationTimer = setInterval(() => {
        if (stage.kind === "verify") checkVerification();
      }, VERIFICATION_POLL_MS);
    }
    if (stage.kind === "creating") {
      if (verificationTimer !== null) clearInterval(verificationTimer);
      verificationTimer = null;
      // The link is clicked in a browser, which took OS focus with it.
      if (wasWaiting) electronBridge.bringAppToFront();
      submitCloudCreate(stage.account);
    }
  }

  function resend(): void {
    if (stage.kind !== "verify") return;
    resendVerificationEmail(stage.account.email).then((outcome) => setStage(noteResend(stage, outcome)));
  }

  function submitCloudCreate(account: FirstRunAccount): void {
    const fail = (message: string): void => {
      if (!isRemoved) setStage(refuseCreate(stage, message));
    };
    // A local first run needs the local probes the cloud-only read leaves out.
    const ready = (
      defaults !== null && !areDefaultsStale(defaults, account.userId)
        ? Promise.resolve(defaults)
        : fetchCreateFormDefaults(null, account.userId, true)
    ).then((loaded) =>
      loaded.is_first_run_local === true ? fetchCreateFormDefaults(null, account.userId, false) : loaded,
    );
    // The rejection handler belongs to the read it names: a submit that throws is caught as its own failure.
    void ready.then(
      async (loaded) => {
        defaults = loaded;
        try {
          const result = await submitCreateRequest(cloudCreateBody(loaded, account.userId));
          if (result.status === 202 && typeof result.data.operation_id === "string") {
            firstRun.noteCreate(result.data.operation_id, account);
            m.route.set(`/creating/${result.data.operation_id}`);
            return;
          }
          fail(cloudCreateRefusal(result));
        } catch {
          fail(fallbackErrorSentence("Imbue Studio couldn't set up your workspace"));
        }
      },
      () => fail(fallbackErrorSentence("Imbue Studio couldn't load the options for your workspace")),
    );
  }

  function retry(): void {
    setStage(retryCreate(stage));
    checkVerification();
  }

  function signInView(): m.Children {
    const isBusy = stage.kind === "signing-in";
    return [
      m("h1", { id: "start-line", class: "first-run-headline text-center text-[clamp(44px,6.4vw,88px)]" }, [
        m("span", { class: "first-run-reimagine" }, SIGN_IN_LINE_LEAD),
        SIGN_IN_LINE_REST,
      ]),
      m(
        "button",
        {
          id: "start-sign-in",
          type: "button",
          class: "first-run-button mt-14 rounded-full px-8 py-3.5 text-[18px] font-semibold",
          disabled: isBusy,
          onclick: signIn,
        },
        SIGN_IN_LABEL,
      ),
      // The page's quieter choices, together at its foot.
      m("div", { class: "absolute bottom-8 flex flex-col items-center gap-4" }, [
        reportingChoice(),
        m(
          "button",
          {
            id: "start-without-account",
            type: "button",
            class: "first-run-link text-[16px]",
            onclick: () => {
              saveReportingAnswer();
              isFormOpen = true;
            },
          },
          WITHOUT_ACCOUNT_LABEL,
        ),
      ]),
    ];
  }

  function closeReportingDetailOnOutsidePress(event: Event): void {
    const target = event.target;
    if (!(target instanceof Element)) return;
    // The label toggles it itself, and a press inside it (the privacy link) leaves it up.
    if (target.closest("#start-reporting-detail, #start-reporting-label") !== null) return;
    isReportingDetailOpen = false;
    m.redraw();
  }

  /**
   * The error-reporting answer, at the page's foot: a checkbox that starts checked, and its label, which opens what
   * the answer means rather than toggling it, so reading about it never changes it.
   */
  function reportingChoice(): m.Children {
    return m("div", { class: "relative flex items-center justify-center gap-2.5" }, [
      m("input", {
        id: "start-reporting-checkbox",
        type: "checkbox",
        class: "first-run-checkbox",
        "aria-label": REPORTING_LABEL,
        checked: isReportingAllowed,
        onchange: (event: Event) => {
          isReportingAllowed = (event.target as HTMLInputElement).checked;
        },
      }),
      m(
        "button",
        {
          id: "start-reporting-label",
          type: "button",
          class: "first-run-link text-[15px]",
          "aria-expanded": isReportingDetailOpen ? "true" : "false",
          "aria-controls": "start-reporting-detail",
          onclick: () => {
            isReportingDetailOpen = !isReportingDetailOpen;
          },
        },
        REPORTING_LABEL,
      ),
      isReportingDetailOpen
        ? [
            m(
              "div",
              {
                id: "start-reporting-detail",
                role: "dialog",
                "aria-label": REPORTING_LABEL,
                class: "first-run-popover absolute bottom-full left-1/2 z-20 mb-3 w-[min(720px,92vw)] -translate-x-1/2",
                // A press anywhere else closes it without being swallowed, so Sign in still signs in.
                oncreate: (vnode: m.VnodeDOM) => {
                  (vnode.dom as HTMLElement).focus();
                  document.addEventListener("pointerdown", closeReportingDetailOnOutsidePress, true);
                },
                onremove: () => document.removeEventListener("pointerdown", closeReportingDetailOnOutsidePress, true),
                tabindex: -1,
                onkeydown: (event: KeyboardEvent) => {
                  if (event.key === "Escape") isReportingDetailOpen = false;
                },
              },
              [
                m("p", `${REPORTING_CONSENT_QUESTION} ${REPORTING_DETAIL_BODY}`),
                m("p", { class: "mt-3" }, [
                  "Our privacy policy is ",
                  m(
                    "a",
                    { class: "underline", href: PRIVACY_POLICY_URL, target: "_blank", rel: "noopener" },
                    "here",
                  ),
                  ".",
                ]),
              ],
            ),
          ]
        : null,
    ]);
  }

  /** Signed in: the welcome the creation page carries on, with what the page is waiting on under it. */
  function signedInView(account: FirstRunAccount): m.Children {
    const heading = m(
      "h1",
      { id: "start-welcome", class: "first-run-headline text-center text-[clamp(40px,4.6vw,64px)]" },
      welcomeHeading(firstNameOf(account)),
    );
    if (stage.kind === "verify") {
      return [
        heading,
        m("p", { id: "start-verify", class: "mt-6 text-center text-[18px] font-semibold" }, VERIFY_LINE),
        m(
          "p",
          { class: "mt-2 max-w-[480px] text-center text-[16px]" },
          `Open the link we sent to ${account.email} and your workspace will start.`,
        ),
        stage.lastResend === null
          ? null
          : m(
              "p",
              { id: "start-verify-note", class: "mt-3 text-center text-[14px] text-[color:var(--first-run-ink-muted)]" },
              verificationEmailSentNote(account.email, stage.lastResend),
            ),
        m("button", { id: "start-resend", type: "button", class: "first-run-link mt-6 text-[15px]", onclick: resend }, RESEND_EMAIL_LABEL),
      ];
    }
    if (stage.kind === "refused") {
      return [
        heading,
        m("p", { id: "start-refused", class: "mt-6 max-w-[520px] text-center text-[16px]" }, stage.message),
        m(
          "button",
          { id: "start-retry", type: "button", class: "first-run-button mt-6 rounded-full px-6 py-2.5 text-[16px] font-semibold", onclick: retry },
          TRY_AGAIN_LABEL,
        ),
      ];
    }
    return [heading, m("p", { id: "start-setting-up", class: "mt-6 text-center text-[16px]" }, SETTING_UP_LINE)];
  }

  return {
    oninit() {
      loadDefaults();
    },
    onupdate() {
      syncSignIn();
    },
    onremove() {
      isRemoved = true;
      stopTimers();
    },
    view() {
      const account = accountOf(stage);
      return m(
        "div",
        {
          id: "start-flow",
          "data-stage": stage.kind,
          class: "first-run relative flex min-h-full w-full flex-col items-center justify-center px-6 py-16",
        },
        [
          m(
            "div",
            { key: "start-content", class: "contents" },
            account === null ? signInView() : signedInView(account),
          ),
          ...(isFormOpen
            ? [createFormModal({
                key: "start-create-form",
                id: "start-create-form",
                onClose: () => {
                  isFormOpen = false;
                },
                initialPreset: "local",
                isOnboardingDrivenByAgent: true,
                onSubmitted: (operationId: string) => {
                  isFormOpen = false;
                  firstRun.noteCreate(operationId, null);
                  m.route.set(`/creating/${operationId}`);
                },
              })]
            : []),
        ],
      );
    },
  };
};
