// The start flow (/start): the first thing a new install shows once the
// backend is up. The manifesto exchange plays on a fixed clock; pressing its
// button starts the first-run questions, asked one at a time as a chat (see
// models/startFlow.ts for the transcript and its transitions). A cloud answer
// submits the create form's remote preset directly; the custom answer opens
// the create form itself as a modal; "I already have one (log in)" signs in and
// leaves for the home page. A cloud create waits for the account's email to be
// verified (the connector refuses it otherwise), polling and offering a resend
// meanwhile. Submitting a create routes to the creation page, which renders
// this transcript above its own turns.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { electronBridge } from "../../electron-bridge";
import type { CreateFormDefaults } from "../../models/create";
import { fetchCreateFormDefaults, submitCreateRequest } from "../../models/create";
import { fetchIsEmailVerified, resendVerificationEmail } from "../../models/emailVerification";
import { markOnboardingComplete } from "../../models/onboarding";
import {
  CHAT_STREAM_STEP_MS,
  CONTINUE_LABEL,
  FLOW,
  FLOW_OPTIONS_GAP_MS,
  FLOW_THINK_MS,
  MANIFESTO_HEADING,
  MANIFESTO_POINTS,
  MANIFESTO_QUESTION,
  SIGN_IN_INTRO_BY_CHOICE,
  answerStep,
  chooseExistingLogin,
  dismissPendingModal,
  finishPendingModal,
  manifestoSchedule,
  noteVerificationEmailSent,
  observeEmailVerified,
  observeSignedIn,
  reaskEmailVerification,
  requireEmailVerification,
  settleCloudCreate,
  startFlow,
  startQuestions,
  stepText,
  streamDurationMs,
  undoAnswer,
} from "../../models/startFlow";
import type { ChoiceId, StartFlowModel, StepId, TranscriptEntry } from "../../models/startFlow";
import { webLogin } from "../../models/webLogin";
import { createFormModal } from "./CreatePage";
import { CreateFormModel, normalizeCreateApiError } from "./create/form-model";
import {
  TRANSCRIPT_COLUMN_CLASS,
  agentTurn,
  answerRow,
  choiceTable,
  disclosureList,
  scrollAnchor,
  userTurn,
} from "./start/transcript";

/** How often the flow asks whether the email is verified while it waits on the link. */
export const VERIFICATION_POLL_MS = 3000;
/** How long "I verified it" keeps checking before the agent says it is not verified yet. */
export const VERIFICATION_PRESS_WAIT_MS = 10000;
export const VERIFICATION_PRESS_STEP_MS = 2000;

/**
 * The turns of a start-flow transcript, rendered. Shared with the creation
 * page, which shows the conversation that led to its create above its own
 * turns; there every turn is already on the page (`isInstant`) and nothing is
 * pressable, so the handlers are optional.
 *
 * A question re-opened by an undo (`reopenedStepIndex`) has already been read:
 * its buttons come back at once rather than after the ask's streaming time.
 */
export function transcriptTurns(
  entries: TranscriptEntry[],
  options: {
    isInstant: boolean;
    isPressable: boolean;
    reopenedStepIndex?: number | null;
    onAnswer?: (at: number, choiceId: ChoiceId) => void;
    onUndo?: (at: number) => void;
    onAside?: (stepId: StepId) => void;
  },
): m.Children[] {
  const turns: m.Children[] = [];
  entries.forEach((entry, at) => {
    const key = `entry-${at}`;
    if (entry.kind === "said") {
      turns.push(userTurn({ key, delayMs: 0, isInstant: options.isInstant, text: entry.text }));
      return;
    }
    if (entry.kind === "note") {
      turns.push(agentTurn({ key, text: entry.text, startAtMs: FLOW_THINK_MS, isInstant: options.isInstant }));
      return;
    }
    const step = FLOW[entry.id];
    const { lead, body: ask } = stepText(step, entry.ack);
    turns.push(
      agentTurn({ key: `${key}-ask`, lead, text: ask, startAtMs: FLOW_THINK_MS, isInstant: options.isInstant }),
    );
    let optionsAt = FLOW_THINK_MS + streamDurationMs(`${lead}${ask}`) + FLOW_OPTIONS_GAP_MS;
    if (step.table) {
      turns.push(choiceTable({ key: `${key}-table`, delayMs: optionsAt, isInstant: options.isInstant, columns: step.table }));
      optionsAt += FLOW_OPTIONS_GAP_MS * 2;
    }
    if (step.prompt) {
      turns.push(
        agentTurn({ key: `${key}-prompt`, text: step.prompt, startAtMs: optionsAt, isInstant: options.isInstant }),
      );
      optionsAt += streamDurationMs(step.prompt) + FLOW_OPTIONS_GAP_MS;
    }
    if (entry.answer !== null) {
      turns.push(
        userTurn({
          key: `${key}-answer`,
          delayMs: 0,
          isInstant: options.isInstant,
          text: entry.said,
          onUndo:
            options.isPressable && options.onUndo && step.isUndoable !== false ? () => options.onUndo?.(at) : undefined,
        }),
      );
      return;
    }
    if (!options.isPressable) return;
    const buttonsAt = at === options.reopenedStepIndex ? 0 : optionsAt;
    const aside = step.aside;
    turns.push(
      answerRow({
        key: `${key}-buttons`,
        delayMs: buttonsAt,
        buttons: step.choices.map((choice) => ({
          id: choice.id,
          label: choice.label,
          isEmphasized: choice.isEmphasized,
          onPress: () => options.onAnswer?.(at, choice.id),
        })),
        aside: aside ? { label: aside.label, onPress: () => options.onAside?.(entry.id) } : undefined,
      }),
    );
  });
  return turns;
}

/**
 * Whether a cached defaults payload can still back a cloud create. The route
 * reads the defaults when it mounts, which on a first run is before the user
 * has an account; a payload that lists none while one exists names no account
 * for the create to run under, and the front door refuses such a create.
 */
export function areDefaultsStale(defaults: CreateFormDefaults | null, isSignedIn: boolean): boolean {
  return defaults === null || (isSignedIn && defaults.accounts.length === 0);
}

/** The create form's remote preset, submitted without the form. */
export function cloudCreateBody(defaults: CreateFormDefaults): Record<string, unknown> {
  const model = new CreateFormModel();
  model.applyDefaults(defaults);
  model.applyPreset("remote");
  return { ...model.submitBody() };
}

export const StartPage: m.ClosureComponent = () => {
  const flow: StartFlowModel = startFlow;
  const schedule = manifestoSchedule();
  let defaults: CreateFormDefaults | null = null;
  let isCustomFormOpen = false;
  let isSubmittingCloud = false;
  // The manifesto points the reader has opened.
  const openManifestoIds = new Set<string>();
  // The verification gate: one check may be in flight, and a poll runs while
  // the flow waits on the emailed link.
  let isCheckingVerification = false;
  let verificationPollTimer: ReturnType<typeof setInterval> | null = null;
  let verificationPressTimer: ReturnType<typeof setTimeout> | null = null;
  // The question most recently re-opened by an undo, whose buttons come back
  // without the arrival delay. Never cleared: an answered question renders no
  // buttons, and every later question is a new, higher index.
  let reopenedStepIndex: number | null = null;
  // A window that already reached the creation page from this flow and came
  // back here starts the conversation afresh.
  flow.reset();

  function redraw(): void {
    m.redraw();
  }

  function loadDefaults(): void {
    if (defaults !== null) return;
    fetchCreateFormDefaults(null)
      .then((loaded) => {
        defaults = loaded;
      })
      .catch(() => undefined);
  }

  function enterCreation(operationId: string, isCloudPreset = false): void {
    flow.submittedCreateAttemptId = operationId;
    flow.isSubmittedCreateCloudPreset = isCloudPreset;
    m.route.set(`/creating/${operationId}`);
  }

  function signedInEmail(): string {
    return getAppContext().stores.accounts.accountEmail || webLogin.email;
  }

  function stopVerificationTimers(): void {
    if (verificationPollTimer !== null) clearInterval(verificationPollTimer);
    if (verificationPressTimer !== null) clearTimeout(verificationPressTimer);
    verificationPollTimer = null;
    verificationPressTimer = null;
  }

  /** The email is verified: the question is answered and the create goes out. */
  function finishVerification(): void {
    stopVerificationTimers();
    // The link is clicked in a browser, which took OS focus with it, so the
    // answer lands while the user is looking at something else. A no-op when
    // this window already has focus -- which is the "I verified it" path,
    // unless the user went back to the browser while that one was retrying.
    electronBridge.bringAppToFront();
    flow.state = observeEmailVerified(flow.state);
    if (flow.state.isCloudCreatePending) submitCloudCreate();
    redraw();
  }

  /**
   * A cloud create is owed. The connector refuses one for an unverified
   * email, so the flow asks first and, when the answer is no, waits on the
   * emailed link instead of letting the create fail. A check that cannot be
   * made does not block: the create itself will say what is wrong. An undo
   * while the check runs takes the create back, and the verdict is dropped.
   */
  function startCloudCreate(): void {
    if (isSubmittingCloud || isCheckingVerification || !flow.state.isCloudCreatePending) return;
    const email = signedInEmail();
    isCheckingVerification = true;
    fetchIsEmailVerified(email).then(
      (isVerified) => {
        isCheckingVerification = false;
        if (!flow.state.isCloudCreatePending) return;
        if (isVerified) {
          submitCloudCreate();
          return;
        }
        flow.state = requireEmailVerification(flow.state, email);
        // Signing up sends no verification email: the connector sends the first
        // one when it refuses a verification-gated action, and this check
        // pre-empts that refusal, so the question sends the email it tells the
        // user to look for. The verdict is dropped; the question's resend
        // reports its own.
        void resendVerificationEmail(email);
        stopVerificationTimers();
        verificationPollTimer = setInterval(() => void pollVerification(), VERIFICATION_POLL_MS);
        redraw();
      },
      () => {
        isCheckingVerification = false;
        if (flow.state.isCloudCreatePending) submitCloudCreate();
      },
    );
  }

  /** One quiet poll while the flow waits on the link; a failed check simply waits for the next. */
  async function pollVerification(): Promise<void> {
    if (flow.state.verificationEmail === null || isCheckingVerification) return;
    const email = flow.state.verificationEmail;
    isCheckingVerification = true;
    try {
      if (await fetchIsEmailVerified(email)) finishVerification();
    } catch {
      // Left for the next poll.
    } finally {
      isCheckingVerification = false;
    }
  }

  /**
   * "I verified it" was pressed: check for a while (the click on the link
   * takes a moment to land), and only if it still is not verified, say so and
   * ask again.
   */
  function pressVerified(): void {
    if (verificationPressTimer !== null || flow.state.verificationEmail === null) return;
    const email = flow.state.verificationEmail;
    const deadline = Date.now() + VERIFICATION_PRESS_WAIT_MS;
    const attempt = (): void => {
      fetchIsEmailVerified(email).then(
        (isVerified) => {
          if (flow.state.verificationEmail === null) return;
          if (isVerified) {
            finishVerification();
            return;
          }
          if (Date.now() < deadline) {
            verificationPressTimer = setTimeout(attempt, VERIFICATION_PRESS_STEP_MS);
            return;
          }
          verificationPressTimer = null;
          flow.state = reaskEmailVerification(flow.state);
          redraw();
        },
        () => {
          verificationPressTimer = null;
          flow.state = reaskEmailVerification(flow.state);
          redraw();
        },
      );
    };
    verificationPressTimer = setTimeout(attempt, 0);
  }

  function resendVerification(): void {
    const email = flow.state.verificationEmail;
    if (email === null) return;
    resendVerificationEmail(email).then((isSent) => {
      flow.state = noteVerificationEmailSent(flow.state, isSent);
      redraw();
    });
  }

  function submitCloudCreate(): void {
    if (isSubmittingCloud) return;
    isSubmittingCloud = true;
    const submit = (loaded: CreateFormDefaults): Promise<void> =>
      submitCreateRequest(cloudCreateBody(loaded)).then((result) => {
        isSubmittingCloud = false;
        if (result.status === 202 && typeof result.data.operation_id === "string") {
          flow.state = settleCloudCreate(flow.state, null);
          enterCreation(result.data.operation_id, true);
          return;
        }
        const message =
          result.status === 0 ? "Could not reach the app backend." : normalizeCreateApiError(result.data).message;
        flow.state = settleCloudCreate(flow.state, `That did not work: ${message}`);
        redraw();
      });
    const cached = defaults;
    const isSignedIn = getAppContext().stores.accounts.hasAccounts;
    const ready =
      cached !== null && !areDefaultsStale(cached, isSignedIn)
        ? Promise.resolve(cached)
        : fetchCreateFormDefaults(null);
    // The rejection handler belongs to the read it names, not to the whole
    // chain: a submit that throws must not be reported as a failed read of the
    // settings, on top of the outcome it already recorded.
    void ready.then(
      (loaded) => {
        defaults = loaded;
        return submit(loaded);
      },
      () => {
        isSubmittingCloud = false;
        flow.state = settleCloudCreate(flow.state, "That did not work: could not load the create settings.");
        redraw();
      },
    );
  }

  function onAnswer(at: number, choiceId: ChoiceId): void {
    if (choiceId === "verified") {
      pressVerified();
      return;
    }
    const accounts = getAppContext().stores.accounts;
    flow.state = answerStep(flow.state, at, choiceId, {
      isSignedIn: accounts.hasAccounts,
      signedInEmail: accounts.accountEmail,
    });
    if (flow.state.pending === "custom") {
      isCustomFormOpen = true;
    } else if (flow.state.pending === "signup" || flow.state.pending === "signin") {
      void webLogin.start(SIGN_IN_INTRO_BY_CHOICE[flow.state.pending]);
    }
    if (flow.state.isCloudCreatePending) startCloudCreate();
  }

  function onAside(stepId: StepId): void {
    if (stepId === "verify" || stepId === "verify-again") {
      resendVerification();
      return;
    }
    flow.state = chooseExistingLogin(flow.state);
    void webLogin.start("Sign in to see your existing workspaces.");
  }

  // An answer that is being acted on cannot be taken back: the POST it caused
  // is already on its way, and a create that lands must find it in place.
  function onUndo(at: number): void {
    if (isSubmittingCloud) return;
    stopVerificationTimers();
    flow.state = undoAnswer(flow.state, at);
    reopenedStepIndex = at;
    isCustomFormOpen = false;
  }

  function toggleManifestoPoint(id: string): void {
    if (openManifestoIds.has(id)) openManifestoIds.delete(id);
    else openManifestoIds.add(id);
  }

  function closeCustomForm(): void {
    isCustomFormOpen = false;
    flow.state = dismissPendingModal(flow.state);
  }

  // The sign-in modals report through the accounts store: an account appearing
  // while the flow waits on one advances it, whether or not the modal is still
  // up. The existing-login answer leaves for the home page instead.
  function syncAccounts(): void {
    const accounts = getAppContext().stores.accounts;
    const pending = flow.state.pending;
    if (pending === "existing-login") {
      if (webLogin.state === "done" || (accounts.hasAccounts && !webLogin.isOpen)) {
        flow.state = dismissPendingModal(flow.state);
        void markOnboardingComplete();
        webLogin.noteSignedIn();
        webLogin.dismiss();
        m.route.set("/");
      } else if (!webLogin.isOpen) {
        flow.state = dismissPendingModal(flow.state);
      }
      return;
    }
    if (pending === "signup" || pending === "signin") {
      // Either signal settles the question: the sign-in flow's own verdict, or
      // the account landing on the channel. A closed modal does not end the
      // wait: the sign-in keeps listening after a dismiss, and an account that
      // lands later advances the flow as if the modal had reported it. Undo,
      // or pressing a button again, is what replaces the wait.
      if (accounts.hasAccounts || webLogin.state === "done") {
        // The cached defaults were read before this account existed, so they
        // name none for the create that follows; the accounts store can be a
        // beat behind the sign-in, so areDefaultsStale cannot see it yet.
        defaults = null;
        flow.state = observeSignedIn(flow.state, signedInEmail());
        // Dismissing stops the poll, so tell the flow the sign-in landed
        // first -- on this branch the channel may well have got there before
        // any poll did, and the raise is the poll's job otherwise.
        webLogin.noteSignedIn();
        webLogin.dismiss();
        if (flow.state.isCloudCreatePending) startCloudCreate();
      }
    }
  }

  return {
    oninit() {
      loadDefaults();
    },
    onupdate() {
      syncAccounts();
    },
    onremove() {
      stopVerificationTimers();
    },
    view() {
      const state = flow.state;
      const pointsAt = schedule.answerAt + streamDurationMs(MANIFESTO_HEADING) + CHAT_STREAM_STEP_MS;
      // Every child is keyed: Mithril rejects a fragment that mixes keyed
      // vnodes with holes, so optional pieces are appended rather than nulled.
      const children: m.Children[] = [
        userTurn({ key: "manifesto-question", delayMs: schedule.questionAt, text: MANIFESTO_QUESTION }),
        agentTurn({ key: "manifesto-answer", text: MANIFESTO_HEADING, startAtMs: schedule.answerAt }),
        disclosureList({
          key: "manifesto-points",
          startAtMs: pointsAt,
          points: MANIFESTO_POINTS,
          openIds: openManifestoIds,
          onToggle: toggleManifestoPoint,
        }),
      ];
      if (!state.isStarted) {
        children.push(
          answerRow({
            key: "manifesto-continue",
            delayMs: schedule.continueAt,
            buttons: [
              {
                id: "continue",
                label: CONTINUE_LABEL,
                isEmphasized: true,
                onPress: () => {
                  flow.state = startQuestions(flow.state);
                },
              },
            ],
          }),
        );
      }
      children.push(
        ...transcriptTurns(state.entries, {
          isInstant: false,
          isPressable: true,
          reopenedStepIndex,
          onAnswer,
          onUndo: isSubmittingCloud ? undefined : onUndo,
          onAside,
        }),
        scrollAnchor(state.entries.length),
      );
      if (isCustomFormOpen) {
        children.push(
          createFormModal({
            key: "start-custom-form",
            id: "start-custom-form",
            onClose: closeCustomForm,
            initialPreset: "local",
            isAdvancedOpen: true,
            onSubmitted: (operationId: string) => {
              isCustomFormOpen = false;
              flow.state = finishPendingModal(flow.state);
              enterCreation(operationId);
            },
          }),
        );
      }
      return m("div", { id: "start-flow", class: TRANSCRIPT_COLUMN_CLASS }, children);
    },
  };
};
