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
  fetchSigninLanding,
  markOnboardingComplete,
  recordErrorReportingConsent,
} from "../../models/onboarding";
import { webLogin } from "../../models/webLogin";
import { accountHubRoute } from "../shell/classify";
import { createFormModal } from "./CreatePage";
import { CreateFormModel, createSubmitRefusal } from "./create/form-model";

export const SIGN_IN_LINE_LEAD = "Reimagine";
export const SIGN_IN_LINE_REST = " your ";
export const SIGN_IN_LINE_SECOND = "personal computer";
export const SIGN_IN_LABEL = "Sign in";
export const WITHOUT_ACCOUNT_LABEL = "Use without an account";
/**
 * What a first-run control means, on hover or keyboard focus of its group: a white card like the page's popups,
 * above the control after a short delay. The bridge under it keeps the pointer's way from the control to the tip hovered, so a
 * link in the tip can be reached.
 */
const FIRST_RUN_TIP_CLASS =
  "invisible absolute bottom-full z-[110] mb-2 rounded-2xl bg-white px-4 py-3 text-left " +
  "type-body text-primary opacity-0 shadow-overlay transition-[opacity,visibility] duration-100 " +
  "after:absolute after:inset-x-0 after:top-full after:h-2 after:content-[''] " +
  "group-hover:visible group-hover:opacity-100 group-hover:delay-150 " +
  "group-has-[:focus-visible]:visible group-has-[:focus-visible]:opacity-100 group-has-[:focus-visible]:delay-150";

export const WITHOUT_ACCOUNT_TIP =
  "Your workspace runs on this computer. You\u2019ll need an account to share it or use it from your phone.";
export const REPORTING_LABEL = "Help improve Studio";
export const REPORTING_TIP_HEADING = "Report data to help improve Studio";
const PRIVACY_POLICY_URL = "https://imbue.com/privacy/";
export const SIGN_IN_MESSAGE = "Sign in to Imbue Studio, or create an account.";
export const SETTING_UP_LINE = "Just a sec, we're setting up your workspace";
/** The setting-up line once the build is known to take minutes, when "just a sec" would not be true. */
export const SETTING_UP_LONG_LINE = "We're setting up your workspace";
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
      m("h1", { id: "start-line", class: "first-run-headline text-center text-[clamp(56px,5.5cqw,88px)]" }, [
        m("span", { class: "first-run-reimagine" }, SIGN_IN_LINE_LEAD),
        SIGN_IN_LINE_REST,
        m("span", { class: "block" }, SIGN_IN_LINE_SECOND),
      ]),
      m(
        "button",
        {
          id: "start-sign-in",
          type: "button",
          class: "first-run-button mt-14 type-heading",
          disabled: isBusy,
          onclick: signIn,
        },
        SIGN_IN_LABEL,
      ),
      // The page's quieter choices at its foot: the reporting answer centred, the way past sign-in at the right.
      m("div", { class: "absolute bottom-8" }, reportingChoice()),
      m("div", { class: "group absolute bottom-8 right-8" }, [
        m(
          "button",
          {
            id: "start-without-account",
            type: "button",
            class: "first-run-link first-run-link--plain type-body",
            "aria-describedby": "start-without-account-tip",
            onclick: () => {
              saveReportingAnswer();
              isFormOpen = true;
            },
          },
          WITHOUT_ACCOUNT_LABEL,
        ),
        m(
          "div",
          { id: "start-without-account-tip", role: "tooltip", class: `${FIRST_RUN_TIP_CLASS} right-0 w-[300px]` },
          WITHOUT_ACCOUNT_TIP,
        ),
      ]),
    ];
  }

  /**
   * The error-reporting answer, at the page's foot: a checkbox that starts checked and its label, which toggles it;
   * what the answer means shows on hover or focus, with the privacy policy a click away.
   */
  function reportingChoice(): m.Children {
    return m("div", { class: "group relative flex items-center justify-center gap-2.5" }, [
      m("input", {
        id: "start-reporting-checkbox",
        type: "checkbox",
        class: "first-run-checkbox",
        "aria-describedby": "start-reporting-tip",
        checked: isReportingAllowed,
        onchange: (event: Event) => {
          isReportingAllowed = (event.target as HTMLInputElement).checked;
        },
      }),
      m(
        "label",
        { id: "start-reporting-label", for: "start-reporting-checkbox", class: "first-run-link first-run-link--plain type-body" },
        REPORTING_LABEL,
      ),
      m("div", { id: "start-reporting-tip", role: "tooltip", class: `${FIRST_RUN_TIP_CLASS} left-1/2 w-[400px] -translate-x-1/2` }, [
        m("p", { class: "type-label" }, REPORTING_TIP_HEADING),
        m("p", { class: "mt-3" }, REPORTING_DETAIL_BODY),
        m("p", { class: "mt-3" }, [
          "Our privacy policy is ",
          m("a", { class: "underline", href: PRIVACY_POLICY_URL, target: "_blank", rel: "noopener" }, "here"),
          ".",
        ]),
      ]),
    ]);
  }

  /** Signed in: the welcome the creation page carries on, with what the page is waiting on under it. */
  function signedInView(account: FirstRunAccount): m.Children {
    const heading = m(
      "h1",
      { id: "start-welcome", class: "first-run-headline text-center text-[40px]" },
      welcomeHeading(firstNameOf(account)),
    );
    if (stage.kind === "verify") {
      return [
        heading,
        m("p", { id: "start-verify", class: "mt-6 text-center type-heading" }, VERIFY_LINE),
        m(
          "p",
          { class: "mt-2 max-w-[480px] text-center type-heading" },
          `Open the link we sent to ${account.email} and your workspace will start.`,
        ),
        stage.lastResend === null
          ? null
          : m(
              "p",
              { id: "start-verify-note", class: "mt-3 text-center type-body text-[color:var(--first-run-ink-muted)]" },
              verificationEmailSentNote(account.email, stage.lastResend),
            ),
        m("button", { id: "start-resend", type: "button", class: "first-run-button first-run-button--secondary mt-6 type-label", onclick: resend }, RESEND_EMAIL_LABEL),
      ];
    }
    if (stage.kind === "refused") {
      return [
        heading,
        m("p", { id: "start-refused", class: "mt-6 max-w-[520px] text-center type-heading" }, stage.message),
        m(
          "button",
          { id: "start-retry", type: "button", class: "first-run-button mt-6 type-heading", onclick: retry },
          TRY_AGAIN_LABEL,
        ),
      ];
    }
    return [heading, m("p", { id: "start-setting-up", class: "mt-6 text-center type-heading" }, SETTING_UP_LINE)];
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
          class: "first-run relative flex min-h-full w-full flex-col [container-type:inline-size] items-center justify-center px-6 py-16",
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
                backdrop: "wash",
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
