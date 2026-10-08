// The start flow's conversation: the fixed manifesto exchange's schedule, and
// the first-run questions as an append-only transcript with a pure reducer
// for everything the user can do to it (answer, undo, back out of the custom
// form, sign in). Views stay thin; every timing number and every transition
// lives here so it is testable without a DOM.
//
// The transcript is held at module scope (like `webLogin`): the creation page
// renders it above its own turns when it is reached from the flow in the same
// session, and a reload simply starts over (nothing is persisted).

import type { ResendOutcome } from "./emailVerification";

/** The agent speaks first, and names the thing the rest of the exchange is about. */
export const MANIFESTO_OPENER = "Imbue Studio is honest software.";
/** The phrase in the opener the exchange is about: italic wherever the opener is drawn. */
export const MANIFESTO_OPENER_EMPHASIS = "honest software";
export const MANIFESTO_QUESTION = "Wait.. what is honest software?";
export const MANIFESTO_HEADING = "Honest Software:";

/**
 * A line behind a chevron toggle, and what opens under it: the manifesto's
 * points, and the creation page's reading material.
 */
export interface DisclosurePoint {
  id: string;
  label: string;
  detail: string;
}

export const MANIFESTO_POINTS: DisclosurePoint[] = [
  {
    id: "loyal",
    label: "is 100% loyal to you",
    detail:
      "Your agent works for you and nobody else. It is not tuned to sell you things, keep you scrolling, " +
      "or serve an advertiser. When your interests and someone else's differ, it takes your side.",
  },
  {
    id: "data",
    label: "never sells your data",
    detail:
      "What you tell your agent stays between you and it. Your conversations, files and memory are never " +
      "sold, shared with advertisers, or used to train models for other people.",
  },
  {
    id: "transparent",
    label: "is fully transparent",
    detail:
      "You can see what your agent is doing and why: every action it takes, every service it reaches, " +
      "every permission it uses. There is no hidden behavior and nothing you are not allowed to inspect.",
  },
  {
    id: "secure",
    label: "is safe and secure",
    detail:
      "Your agent runs in its own workspace, apart from your other data, and only reaches the services you " +
      "grant it. You decide what it may touch, and you can take a permission back at any time.",
  },
  {
    id: "portable",
    label: "doesn't lock you in",
    detail:
      "Your workspace is yours. Run it on Imbue Cloud, on your own computer, or on a provider you choose, " +
      "and move it later without losing anything. Your data leaves with you whenever you want it to.",
  },
];

/** The manifesto answer as one text, for the length the streaming schedule is timed on. */
export const MANIFESTO_ANSWER = [MANIFESTO_HEADING, "", ...MANIFESTO_POINTS.map((point) => point.label)].join("\n");

/**
 * The question that closes the manifesto. Answering it is what starts the
 * first-run questions, so every install answers it exactly once, before it is
 * asked anything else.
 */
export const REPORTING_ASK = "Is it ok if we report errors to help improve Studio?";
/** What the ask does not say, a click away. */
export const REPORTING_MORE_LABEL = "See more";
export const REPORTING_MORE_DETAIL =
  "The reports we collect include diagnostic details about the error and your setup, which can be identifying " +
  "at times (e.g. an email account). We generally try to anonymize data, and we try to ensure that error data " +
  "is not retained for more than 30 days. You can change this any time in Settings → Error reporting.";
export const REPORTING_DECLINE_LABEL = "No";
export const REPORTING_ACCEPT_LABEL = "Sounds great";

/** The user's bubble rises into place over this long. */
export const CHAT_BUBBLE_MS = 280;
/** The beat between the question landing and the answer starting: read as considering it. */
export const CHAT_THINK_MS = 1000;
/** The beat between the opener landing and the reply to it: read as taking it in. */
export const CHAT_REACT_MS = 2000;
/** The answer streams a character at a time, each fading in; even, like a machine emitting tokens. */
export const CHAT_STREAM_STEP_MS = 12;
export const CHAT_STREAM_FADE_MS = 70;
/** The pause before the first message. */
export const CHAT_GAP_MS = 150;
/** The beat between the manifesto's last point and the question that follows it: read as letting it land. */
export const CHAT_READ_MS = 1200;
/** The pause before an agent turn the user's action caused starts arriving. */
export const FLOW_THINK_MS = 500;
/** The pause between a question landing and its choices appearing under it. */
export const FLOW_OPTIONS_GAP_MS = 250;

/** How long a stream of this text takes from its first character to its last landing. */
export function streamDurationMs(text: string): number {
  return Math.max(0, [...text].length - 1) * CHAT_STREAM_STEP_MS + CHAT_STREAM_FADE_MS;
}

export interface ManifestoSchedule {
  openerAt: number;
  questionAt: number;
  answerAt: number;
  reportingAt: number;
  answersAt: number;
}

export function manifestoSchedule(): ManifestoSchedule {
  const openerAt = CHAT_GAP_MS;
  // The reply waits out the whole opener, not just its first character.
  const questionAt = openerAt + streamDurationMs(MANIFESTO_OPENER) + CHAT_REACT_MS;
  const answerAt = questionAt + CHAT_BUBBLE_MS + CHAT_THINK_MS;
  const reportingAt = answerAt + streamDurationMs(MANIFESTO_ANSWER) + CHAT_READ_MS;
  // The buttons follow their question the same beat later every other question's do.
  const answersAt = reportingAt + streamDurationMs(REPORTING_ASK) + FLOW_OPTIONS_GAP_MS;
  return { openerAt, questionAt, answerAt, reportingAt, answersAt };
}

export type StepId = "run" | "auth" | "account" | "retry" | "again" | "verify" | "verify-again";
export type ChoiceId = "cloud" | "custom" | "signup" | "signin" | "continue" | "verified";

export interface FlowChoice {
  id: ChoiceId;
  /** The button's label. */
  label: string;
  /** The user turn the press becomes. */
  said: string;
  /** What the agent says on hearing it, prepended to the next question. */
  ack: string;
  /** The louder of the pair: filled green rather than a ghost. */
  isEmphasized: boolean;
}

export interface TableColumn {
  title: string;
  badge?: string;
  isEmphasized: boolean;
  points: string[];
}

export interface FlowStep {
  ask: string;
  /** A bold first line over the ask, for the one question that is a requirement rather than a choice. */
  lead?: string;
  /** False for a question whose answer is a fact the user cannot take back (a verified email). */
  isUndoable?: boolean;
  /** The two-column comparison, only on the question that is one. */
  table?: TableColumn[];
  /** The one-line prompt directly over the buttons, when a table sits between them and the ask. */
  prompt?: string;
  choices: FlowChoice[];
  /** The quieter, agent-side way out under the question, when there is one. */
  aside?: { label: string };
}

const CLOUD_CHOICE: FlowChoice = {
  id: "cloud",
  label: "On Imbue Cloud",
  said: "On Imbue Cloud",
  ack: "Imbue Cloud it is.",
  isEmphasized: true,
};

const CUSTOM_CHOICE: FlowChoice = {
  id: "custom",
  label: "Custom setup",
  said: "Custom setup",
  ack: "Your own platform it is.",
  isEmphasized: false,
};

export const EXISTING_LOGIN_LABEL = "I already have a workspace (log in)";
export const RESEND_EMAIL_LABEL = "Send the email again";
export const VERIFIED_LABEL = "I verified it";

const SIGNUP_CHOICE: FlowChoice = {
  id: "signup",
  label: "Create an account",
  said: "",
  ack: "You're in.",
  isEmphasized: false,
};

const SIGNIN_ACK = "Welcome back.";

const VERIFIED_CHOICE: FlowChoice = {
  id: "verified",
  label: VERIFIED_LABEL,
  said: VERIFIED_LABEL,
  ack: "Your email is verified.",
  isEmphasized: true,
};

export const FLOW: Record<StepId, FlowStep> = {
  run: {
    ask:
      "Let's create your first workspace. A workspace is your own virtual computer. " +
      "You can run it on Imbue Cloud (recommended) or bring your own platform (custom setup).",
    table: [
      {
        title: "Imbue Cloud",
        badge: "Recommended",
        isEmphasized: true,
        points: [
          "30 second setup",
          "Runs even if your computer is off",
          "Accessible from mobile",
          "Shareable with other people",
        ],
      },
      {
        title: "Custom setup",
        badge: "Advanced",
        isEmphasized: false,
        points: [
          "Runs on your computer, or in your own cloud",
          "Docker or Lima here; AWS, GCP, Azure or Vultr there",
          "You manage uptime, backups and cost",
        ],
      },
    ],
    prompt: "How do you want to run it?",
    choices: [CUSTOM_CHOICE, CLOUD_CHOICE],
    aside: { label: EXISTING_LOGIN_LABEL },
  },
  // Sign in leads: the app is downloaded from a page that already required an
  // Imbue account, so the account step is a sign-in for nearly everyone. The
  // quieter create-account button stays for dev and CI runs.
  auth: {
    ask:
      "A cloud workspace runs on our machines, so it needs an Imbue account. " +
      "Sign in with the account you downloaded Imbue Studio with.",
    choices: [SIGNUP_CHOICE, { id: "signin", label: "Sign in", said: "", ack: SIGNIN_ACK, isEmphasized: true }],
  },
  // The account step when an account is already signed in: the entry's ack
  // names it, and the user may keep it or switch. Switching opens the browser
  // sign-in, whose page offers both signing in and creating an account, so the
  // ack cannot say which happened.
  account: {
    ask: "Continue with this account or use a different one?",
    choices: [
      { id: "signin", label: "Use a different account", said: "", ack: SIGNUP_CHOICE.ack, isEmphasized: false },
      { id: "continue", label: "Continue", said: "", ack: SIGNIN_ACK, isEmphasized: true },
    ],
  },
  retry: {
    ask:
      "Looks like you closed the Custom setup dialog without finishing. No problem — would you like your " +
      "workspace on Imbue Cloud instead? You can always move it later.",
    choices: [CUSTOM_CHOICE, CLOUD_CHOICE],
  },
  // Asked again after a refused cloud create, with the refusal as its ack.
  again: {
    ask: "How do you want to run it?",
    choices: [CUSTOM_CHOICE, CLOUD_CHOICE],
  },
  // A cloud workspace needs a verified email; the entry's ack names the address.
  verify: {
    lead: "You must verify your email",
    ask: "",
    choices: [VERIFIED_CHOICE],
    aside: { label: RESEND_EMAIL_LABEL },
    isUndoable: false,
  },
  "verify-again": {
    ask: "Not verified yet. Click the link in the email, then press the button again.",
    choices: [VERIFIED_CHOICE],
    aside: { label: RESEND_EMAIL_LABEL },
    isUndoable: false,
  },
};

/** The agent's text for a question: its bold lead, then the ack the previous answer earned, then the ask. */
export function stepText(step: FlowStep, ack: string): { lead: string; body: string } {
  return { lead: step.lead ?? "", body: [ack, step.ask].filter((part) => part !== "").join(" ") };
}

export function verificationAsk(email: string): string {
  return `(click the link emailed to ${email} when you created your account)`;
}

/** Only an outcome the server confirmed may point the user at an inbox. */
export function verificationEmailSentNote(email: string, outcome: ResendOutcome): string {
  switch (outcome) {
    case "sent":
      return `Sent another email to ${email}.`;
    case "suppressed":
      return `An email went out to ${email} moments ago. Check your inbox and spam folder.`;
    case "failed":
      return `Could not send the email to ${email}. Please try again.`;
  }
}

export const SIGN_IN_INTRO_BY_CHOICE: Record<"signup" | "signin", string> = {
  signup: "Create an account to run your workspace in Imbue Cloud.",
  signin: "Sign in to run your workspace in Imbue Cloud.",
};

/** What your side says once the account step is done. */
export function signedInSaid(email: string): string {
  return `You've signed in as ${email || "your account"}`;
}

/** What your side says on keeping the account that was already signed in. */
export function continueAsSaid(email: string): string {
  return `Continue as ${email}`;
}

/** What the agent says on hearing the cloud answer with an account already signed in. */
export function signedInAck(email: string): string {
  return `${CLOUD_CHOICE.ack} You're signed in as ${email}.`;
}

/** A signed-in account, as the flow names the one its cloud create runs under. */
export interface FlowAccount {
  userId: string;
  email: string;
}

/**
 * One entry, in order. A `step` is a question, with its answer once given; a
 * `said` is a user turn with no question behind it (the reporting answer that
 * starts the questions, the sign-in receipt); a `note` is an agent line with
 * nothing to answer.
 */
export type TranscriptEntry =
  | {
      kind: "step";
      id: StepId;
      ack: string;
      answer: ChoiceId | null;
      said: string;
      /** The signed-in account's email the question names, drawn in bold in its ack. */
      email?: string;
    }
  | { kind: "said"; text: string }
  | { kind: "note"; text: string };

/** Which modal, if any, the flow is waiting on before it can record an answer. */
export type PendingModal = "custom" | "signup" | "signin" | "existing-login" | null;

export interface StartFlowState {
  /** The manifesto's button has been pressed; the questions are live. */
  isStarted: boolean;
  entries: TranscriptEntry[];
  pending: PendingModal;
  /** A cloud create was asked for and is waiting on the account step. */
  isCloudCreatePending: boolean;
  /** The address whose verification the cloud create is waiting on, or null when none is. */
  verificationEmail: string | null;
  /** The account the account step settled on: the one the cloud create runs under and whose email is verified. */
  account: FlowAccount | null;
  /** The signed-in account the open account step offers to continue as. */
  offeredAccount: FlowAccount | null;
  /**
   * The accounts already signed in when a sign-in button was pressed. Only an
   * account outside this set, or the one the sign-in itself reports, answers
   * that press: an account that was there before is not the one just added.
   */
  knownAccountIds: readonly string[];
}

export function initialStartFlowState(): StartFlowState {
  return {
    isStarted: false,
    entries: [],
    pending: null,
    isCloudCreatePending: false,
    verificationEmail: null,
    account: null,
    offeredAccount: null,
    knownAccountIds: [],
  };
}

function stepEntry(id: StepId, ack: string): TranscriptEntry {
  return { kind: "step", id, ack, answer: null, said: "" };
}

/** The account step after the cloud answer: asking for an account, or offering to continue as the one signed in. */
function accountStepEntry(signedInAccount: FlowAccount | null): TranscriptEntry {
  if (signedInAccount === null) return stepEntry("auth", CLOUD_CHOICE.ack);
  const email = signedInAccount.email;
  return { kind: "step", id: "account", ack: signedInAck(email), answer: null, said: "", email };
}

/** The user answered the reporting question, whichever way; `said` is the button they pressed. */
export function startQuestions(state: StartFlowState, said: string): StartFlowState {
  if (state.isStarted) return state;
  return {
    ...state,
    isStarted: true,
    entries: [{ kind: "said", text: said }, stepEntry("run", "")],
  };
}

function withAnswer(entries: TranscriptEntry[], at: number, answer: ChoiceId, said: string): TranscriptEntry[] {
  return entries.map((entry, index) =>
    index === at && entry.kind === "step" ? { ...entry, answer, said } : entry,
  );
}

/** What the page knows about the accounts when a button is pressed. */
export interface AnswerContext {
  /** The default signed-in account, or null when none is signed in. */
  signedInAccount: FlowAccount | null;
  /** Every account signed in right now. */
  knownAccountIds: readonly string[];
}

/**
 * The user pressed one of a question's buttons. The answer lands on that
 * question; what follows depends on the choice: the cloud path asks for an
 * account (offering to continue as one already signed in), continuing owes the
 * create, and the custom path and the sign-ins hand off to a modal and record
 * nothing more until it is done.
 */
export function answerStep(
  state: StartFlowState,
  at: number,
  choiceId: ChoiceId,
  context: AnswerContext,
): StartFlowState {
  const entry = state.entries[at];
  if (entry === undefined || entry.kind !== "step") return state;
  const choice = FLOW[entry.id].choices.find((candidate) => candidate.id === choiceId);
  if (choice === undefined) return state;
  // Pressing "I verified it" is a request to check, not an answer: the view
  // checks, then records the outcome with observeEmailVerified or
  // reaskEmailVerification.
  if (choiceId === "verified") return state;
  // Any answer replaces a sign-in the flow was still waiting on: one that lands later must not answer again.
  if (choiceId === "cloud") {
    const entries = withAnswer(state.entries, at, choiceId, choice.said);
    const offered = context.signedInAccount;
    return {
      ...state,
      entries: [...entries, accountStepEntry(offered)],
      pending: null,
      account: null,
      offeredAccount: offered,
    };
  }
  if (choiceId === "continue") {
    const offered = state.offeredAccount;
    if (offered === null || !context.knownAccountIds.includes(offered.userId)) {
      return reofferAccountStep(state, at, context.signedInAccount);
    }
    return {
      ...state,
      entries: [
        ...withAnswer(state.entries, at, choiceId, continueAsSaid(offered.email)),
        { kind: "note", text: choice.ack },
      ],
      pending: null,
      account: offered,
      isCloudCreatePending: true,
    };
  }
  if (choiceId === "custom") {
    return {
      ...state,
      entries: [...withAnswer(state.entries, at, choiceId, choice.said), { kind: "note", text: choice.ack }],
      pending: "custom",
    };
  }
  // The sign-ins: nothing is recorded until the modal reports an account.
  return { ...state, pending: choiceId, knownAccountIds: context.knownAccountIds };
}

/**
 * The account offered at the account step is no longer signed in: the same
 * question is asked again in place, about whichever account is signed in now.
 */
function reofferAccountStep(state: StartFlowState, at: number, signedInAccount: FlowAccount | null): StartFlowState {
  const reasked = accountStepEntry(signedInAccount);
  return {
    ...state,
    entries: state.entries.map((entry, index) => (index === at ? reasked : entry)),
    pending: null,
    offeredAccount: signedInAccount,
  };
}

/**
 * The account that answers the sign-in the flow is waiting on, or null when
 * none has landed yet: the one the sign-in itself reports (it may be an account
 * that was already signed in), else one that was not signed in when the button
 * was pressed.
 */
export function landedAccount(
  state: StartFlowState,
  accounts: readonly FlowAccount[],
  completedSignInEmail: string,
): FlowAccount | null {
  if (state.pending !== "signup" && state.pending !== "signin") return null;
  const reported = accounts.find((account) => completedSignInEmail !== "" && account.email === completedSignInEmail);
  if (reported !== undefined) return reported;
  return accounts.find((account) => !state.knownAccountIds.includes(account.userId)) ?? null;
}

/** The user pressed the quieter "I already have one (log in)" under the first question. */
export function chooseExistingLogin(state: StartFlowState): StartFlowState {
  return { ...state, pending: "existing-login" };
}

/** A modal the flow was waiting on closed without finishing. */
export function dismissPendingModal(state: StartFlowState): StartFlowState {
  if (state.pending === null) return state;
  if (state.pending === "custom") {
    return { ...state, pending: null, entries: [...state.entries, stepEntry("retry", "")] };
  }
  return { ...state, pending: null };
}

/** A modal the flow was waiting on finished its job (the custom form submitted). */
export function finishPendingModal(state: StartFlowState): StartFlowState {
  return { ...state, pending: null };
}

/**
 * An account appeared while the account step was waiting on a sign-in modal.
 * The buttons become the receipt, the agent acknowledges, and the cloud create
 * is owed. Ignored when the flow was not waiting on an account.
 */
export function observeSignedIn(state: StartFlowState, account: FlowAccount): StartFlowState {
  if (state.pending !== "signup" && state.pending !== "signin") return state;
  const at = lastAccountStepIndex(state.entries);
  if (at < 0) return { ...state, pending: null };
  const step = state.entries[at];
  const pending = state.pending;
  const choice = step.kind === "step" ? FLOW[step.id].choices.find((candidate) => candidate.id === pending) : undefined;
  if (choice === undefined) throw new Error(`The account step does not offer the awaited "${pending}" choice`);
  const entries = withAnswer(state.entries, at, state.pending, signedInSaid(account.email));
  return {
    ...state,
    pending: null,
    entries: [...entries, { kind: "note", text: choice.ack }],
    account,
    isCloudCreatePending: true,
  };
}

/**
 * Let a landed sign-in answer the account step the flow is waiting on: the
 * account that answers it (see landedAccount) becomes the settled one and the
 * cloud create is owed. Unchanged while nothing has landed or nothing waits.
 */
export function settleAwaitedSignIn(
  state: StartFlowState,
  accounts: readonly FlowAccount[],
  completedSignInEmail: string,
): StartFlowState {
  const landed = landedAccount(state, accounts, completedSignInEmail);
  return landed === null ? state : observeSignedIn(state, landed);
}

/**
 * The settled account's email is not verified, and the connector will
 * refuse the cloud create until it is. The create waits; the flow asks for
 * the click on the emailed link instead.
 */
export function requireEmailVerification(state: StartFlowState): StartFlowState {
  if (!state.isCloudCreatePending || state.account === null) return state;
  const email = state.account.email;
  return {
    ...state,
    isCloudCreatePending: false,
    verificationEmail: email,
    entries: [...state.entries, stepEntry("verify", verificationAsk(email))],
  };
}

/** The email came back verified: the open verification question is answered and the create is owed again. */
export function observeEmailVerified(state: StartFlowState): StartFlowState {
  const at = openVerificationStepIndex(state.entries);
  if (state.verificationEmail === null || at < 0) return state;
  return {
    ...state,
    verificationEmail: null,
    isCloudCreatePending: true,
    entries: [...withAnswer(state.entries, at, "verified", VERIFIED_CHOICE.said), { kind: "note", text: VERIFIED_CHOICE.ack }],
  };
}

/** "I verified it" was pressed but the email is still unverified: the press is recorded and the question re-asked. */
export function reaskEmailVerification(state: StartFlowState): StartFlowState {
  const at = openVerificationStepIndex(state.entries);
  if (state.verificationEmail === null || at < 0) return state;
  return {
    ...state,
    entries: [...withAnswer(state.entries, at, "verified", VERIFIED_CHOICE.said), stepEntry("verify-again", "")],
  };
}

/** The verification email was (or was not) re-sent; the agent says which. */
export function noteVerificationEmailSent(state: StartFlowState, outcome: ResendOutcome): StartFlowState {
  if (state.verificationEmail === null) return state;
  return {
    ...state,
    entries: [...state.entries, { kind: "note", text: verificationEmailSentNote(state.verificationEmail, outcome) }],
  };
}

function openVerificationStepIndex(entries: TranscriptEntry[]): number {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "step" && (entry.id === "verify" || entry.id === "verify-again")) {
      return entry.answer === null ? index : -1;
    }
  }
  return -1;
}

/**
 * The cloud create was submitted (or refused); either way the flow no longer
 * owes it. A refusal is said as the ack of the re-asked where-to-run question,
 * so the error and the question arrive as one agent turn.
 */
export function settleCloudCreate(state: StartFlowState, refusal: string | null): StartFlowState {
  const settled = { ...state, isCloudCreatePending: false };
  if (refusal === null) return settled;
  return { ...settled, entries: [...settled.entries, stepEntry("again", refusal)] };
}

/**
 * A cloud create is about to go out. If the account it is owed under has been
 * signed out since the account step settled on it, it is refused here instead,
 * so where to run is asked again and a fresh account step offers whoever is
 * signed in now.
 */
export function refuseCreateForSignedOutAccount(
  state: StartFlowState,
  knownAccountIds: readonly string[],
): StartFlowState {
  const account = state.account;
  if (!state.isCloudCreatePending || account === null || knownAccountIds.includes(account.userId)) return state;
  return settleCloudCreate(state, `That did not work: ${account.email} is no longer signed in.`);
}

/**
 * Take the reporting answer back: the whole conversation that followed it goes,
 * and the reporting question is asked again.
 */
export function undoReporting(state: StartFlowState): StartFlowState {
  return state.isStarted ? initialStartFlowState() : state;
}

/**
 * Take a question back: its answer goes, and so does everything said after it.
 * The one place the transcript does not only grow, because every later turn
 * followed from the answer being undone.
 */
export function undoAnswer(state: StartFlowState, at: number): StartFlowState {
  const entry = state.entries[at];
  if (entry === undefined || entry.kind !== "step") return state;
  return {
    ...state,
    pending: null,
    isCloudCreatePending: false,
    verificationEmail: null,
    account: null,
    entries: [...state.entries.slice(0, at), { ...entry, answer: null, said: "" }],
  };
}

function lastAccountStepIndex(entries: TranscriptEntry[]): number {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "step" && (entry.id === "auth" || entry.id === "account")) return index;
  }
  return -1;
}

/** The question the user is being asked right now, or null when none is open. */
export function openStepIndex(state: StartFlowState): number | null {
  const last = state.entries[state.entries.length - 1];
  if (last === undefined || last.kind !== "step" || last.answer !== null) return null;
  return state.entries.length - 1;
}

/**
 * The flow model one window holds. Mutable by design (it is what the views
 * redraw from), but every transition goes through the pure functions above.
 */
export class StartFlowModel {
  state: StartFlowState = initialStartFlowState();
  /** The create attempt the flow submitted, so the creation page knows this transcript is its prelude. */
  submittedCreateAttemptId: string | null = null;
  /**
   * Whether that create was the Imbue Cloud answer, which submits the form's
   * remote preset without ever showing the form. The creation page restates
   * such a create as the single choice it was; opening the form and picking
   * Imbue Cloud there is not this, because there the settings are the
   * reader's own.
   */
  isSubmittedCreateCloudPreset = false;

  reset(): void {
    this.state = initialStartFlowState();
    this.submittedCreateAttemptId = null;
    this.isSubmittedCreateCloudPreset = false;
  }
}

/** One conversation per window. */
export const startFlow = new StartFlowModel();
