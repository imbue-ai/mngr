// The start flow's conversation: the fixed manifesto exchange's schedule, and
// the first-run questions as an append-only transcript with a pure reducer
// for everything the user can do to it (answer, undo, back out of the custom
// form, sign in). Views stay thin; every timing number and every transition
// lives here so it is testable without a DOM.
//
// The transcript is held at module scope (like `webLogin`): the creation page
// renders it above its own turns when it is reached from the flow in the same
// session, and a reload simply starts over (nothing is persisted).

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
      "Your Mind works for you and nobody else. It is not tuned to sell you things, keep you scrolling, " +
      "or serve an advertiser. When your interests and someone else's differ, it takes your side.",
  },
  {
    id: "data",
    label: "never sells your data",
    detail:
      "What you tell your Mind stays between you and it. Your conversations, files and memory are never " +
      "sold, shared with advertisers, or used to train models for other people.",
  },
  {
    id: "transparent",
    label: "is fully transparent",
    detail:
      "You can see what your Mind is doing and why: every action it takes, every service it reaches, " +
      "every permission it uses. There is no hidden behavior and nothing you are not allowed to inspect.",
  },
  {
    id: "secure",
    label: "is safe and secure",
    detail:
      "Your Mind runs in its own workspace, apart from your other data, and only reaches the services you " +
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
export const CONTINUE_LABEL = "Sounds great, let's continue";

/** The user's bubble rises into place over this long. */
export const CHAT_BUBBLE_MS = 280;
/** The beat between the question landing and the answer starting: read as considering it. */
export const CHAT_THINK_MS = 1000;
/** The answer streams a character at a time, each fading in; even, like a machine emitting tokens. */
export const CHAT_STREAM_STEP_MS = 12;
export const CHAT_STREAM_FADE_MS = 70;
/** The pause before the first message, and between an answer landing and its button. */
export const CHAT_GAP_MS = 150;
export const CHAT_CONTINUE_GAP_MS = 1200;
/** The pause before an agent turn the user's action caused starts arriving. */
export const FLOW_THINK_MS = 500;
/** The pause between a question landing and its choices appearing under it. */
export const FLOW_OPTIONS_GAP_MS = 250;

/** How long a stream of this text takes from its first character to its last landing. */
export function streamDurationMs(text: string): number {
  return Math.max(0, [...text].length - 1) * CHAT_STREAM_STEP_MS + CHAT_STREAM_FADE_MS;
}

export interface ManifestoSchedule {
  questionAt: number;
  answerAt: number;
  continueAt: number;
}

export function manifestoSchedule(): ManifestoSchedule {
  const questionAt = CHAT_GAP_MS;
  const answerAt = questionAt + CHAT_BUBBLE_MS + CHAT_THINK_MS;
  const continueAt = answerAt + streamDurationMs(MANIFESTO_ANSWER) + CHAT_CONTINUE_GAP_MS;
  return { questionAt, answerAt, continueAt };
}

export type StepId = "run" | "auth" | "retry" | "again" | "verify" | "verify-again";
export type ChoiceId = "cloud" | "custom" | "signup" | "signin" | "verified";

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
  auth: {
    ask: "A cloud workspace runs on our machines, so it needs an Imbue account.",
    choices: [
      { id: "signin", label: "Sign in", said: "", ack: "Welcome back.", isEmphasized: false },
      { id: "signup", label: "Create an account", said: "", ack: "You're in.", isEmphasized: true },
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
  return `(click the link sent to ${email})`;
}

export function verificationEmailSentNote(email: string, isSent: boolean): string {
  return isSent
    ? `Sent another email to ${email}.`
    : `An email was sent to ${email} recently. Check your inbox and spam folder.`;
}

export const SIGN_IN_INTRO_BY_CHOICE: Record<"signup" | "signin", string> = {
  signup: "Create an account to run your workspace in Imbue Cloud.",
  signin: "Sign in to run your workspace in Imbue Cloud.",
};

/** What your side says once the account step is done. */
export function signedInSaid(email: string): string {
  return `You've signed in as ${email || "your account"}`;
}

/**
 * One entry, in order. A `step` is a question, with its answer once given; a
 * `said` is a user turn with no question behind it (the "Sounds great" press,
 * the sign-in receipt); a `note` is an agent line with nothing to answer.
 */
export type TranscriptEntry =
  | { kind: "step"; id: StepId; ack: string; answer: ChoiceId | null; said: string }
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
}

export function initialStartFlowState(): StartFlowState {
  return { isStarted: false, entries: [], pending: null, isCloudCreatePending: false, verificationEmail: null };
}

function stepEntry(id: StepId, ack: string): TranscriptEntry {
  return { kind: "step", id, ack, answer: null, said: "" };
}

/** The user pressed "Sounds great, let's continue". */
export function startQuestions(state: StartFlowState): StartFlowState {
  if (state.isStarted) return state;
  return {
    ...state,
    isStarted: true,
    entries: [{ kind: "said", text: CONTINUE_LABEL }, stepEntry("run", "")],
  };
}

function withAnswer(entries: TranscriptEntry[], at: number, answer: ChoiceId, said: string): TranscriptEntry[] {
  return entries.map((entry, index) =>
    index === at && entry.kind === "step" ? { ...entry, answer, said } : entry,
  );
}

/**
 * The user pressed one of a question's buttons. The answer lands on that
 * question; what follows depends on the choice: the cloud path asks for an
 * account (or, with one already signed in, goes straight to creating), and the
 * custom path and the sign-ins hand off to a modal and record nothing more
 * until it is done.
 */
export function answerStep(
  state: StartFlowState,
  at: number,
  choiceId: ChoiceId,
  context: { isSignedIn: boolean; signedInEmail: string },
): StartFlowState {
  const entry = state.entries[at];
  if (entry === undefined || entry.kind !== "step") return state;
  const choice = FLOW[entry.id].choices.find((candidate) => candidate.id === choiceId);
  if (choice === undefined) return state;
  // Pressing "I verified it" is a request to check, not an answer: the view
  // checks, then records the outcome with observeEmailVerified or
  // reaskEmailVerification.
  if (choiceId === "verified") return state;
  if (choiceId === "cloud") {
    const entries = withAnswer(state.entries, at, choiceId, choice.said);
    if (context.isSignedIn) {
      return {
        ...state,
        entries: [...entries, { kind: "note", text: `${choice.ack} ${signedInSaid(context.signedInEmail)}.` }],
        isCloudCreatePending: true,
      };
    }
    return { ...state, entries: [...entries, stepEntry("auth", choice.ack)] };
  }
  if (choiceId === "custom") {
    return {
      ...state,
      entries: [...withAnswer(state.entries, at, choiceId, choice.said), { kind: "note", text: choice.ack }],
      pending: "custom",
    };
  }
  // The sign-ins: nothing is recorded until the modal reports an account.
  return { ...state, pending: choiceId };
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
export function observeSignedIn(state: StartFlowState, email: string): StartFlowState {
  if (state.pending !== "signup" && state.pending !== "signin") return state;
  const at = lastAuthStepIndex(state.entries);
  if (at < 0) return { ...state, pending: null };
  const choice = FLOW.auth.choices.find((candidate) => candidate.id === state.pending);
  const entries = withAnswer(state.entries, at, state.pending, signedInSaid(email));
  return {
    ...state,
    pending: null,
    entries: [...entries, { kind: "note", text: choice?.ack ?? "" }],
    isCloudCreatePending: true,
  };
}

/**
 * The signed-in account's email is not verified, and the connector will
 * refuse the cloud create until it is. The create waits; the flow asks for
 * the click on the emailed link instead.
 */
export function requireEmailVerification(state: StartFlowState, email: string): StartFlowState {
  if (!state.isCloudCreatePending) return state;
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
export function noteVerificationEmailSent(state: StartFlowState, isSent: boolean): StartFlowState {
  if (state.verificationEmail === null) return state;
  return {
    ...state,
    entries: [...state.entries, { kind: "note", text: verificationEmailSentNote(state.verificationEmail, isSent) }],
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
    entries: [...state.entries.slice(0, at), { ...entry, answer: null, said: "" }],
  };
}

function lastAuthStepIndex(entries: TranscriptEntry[]): number {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "step" && entry.id === "auth") return index;
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
