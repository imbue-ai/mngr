// The manifesto exchange every workspace's creation conversation opens with,
// and the chat timing its turns stream on: the creation page draws it and
// the welcome chat carries it into the new workspace.

/** The agent speaks first, and names the thing the rest of the exchange is about. */
export const MANIFESTO_OPENER =
  "Studio is a new kind of personal computer, built on honest software.";
/** The phrase in the opener the exchange is about: italic wherever the opener is drawn. */
export const MANIFESTO_OPENER_EMPHASIS = "honest software";
export const MANIFESTO_QUESTION = "Wait.. what is honest software?";
export const MANIFESTO_HEADING = "Honest Software:";

/**
 * A line behind a chevron toggle, and what opens under it: the manifesto's
 * points.
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
      "It isn’t built to sell you things, keep you hooked, or make money off your attention. Studio answers " +
      "to you, not to advertisers or big tech companies.",
  },
  {
    id: "data",
    label: "never sells your data",
    detail:
      "Everything stays between you and Studio. We never sell it, show it to advertisers, or use it to train " +
      "AI for anyone else.",
  },
  {
    id: "transparent",
    label: "is fully transparent",
    detail:
      "You can always see what Studio is doing and why: what it changed, which apps it connected to, and " +
      "what it has access to. Nothing happens behind your back.",
  },
  {
    id: "secure",
    label: "is safe and secure",
    detail:
      "Studio can only use the apps and accounts you connect. You can disconnect any of them, anytime.",
  },
  {
    id: "portable",
    label: "doesn't lock you in",
    detail:
      "Run Studio on our cloud, your own computer, or a provider you choose, and switch whenever you want " +
      "without losing a thing. Leave at any time and take everything with you.",
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
/** What error reports hold and how long they are kept: the part every surface that explains reporting shares. */
export const REPORTING_DETAIL_BODY =
  "Error reports include details about what went wrong and how " +
  "your Studio is set up. We try to remove anything personal, but sometimes something identifying, like an email " +
  "address, gets through. Your privacy matters to us, so we do our best to delete error data within 30 days. If " +
  "you change your mind, you can turn error reporting on or off in Settings → Error reporting.";
export const REPORTING_MORE_DETAIL = `We want you to know what you're saying yes to. ${REPORTING_DETAIL_BODY}`;
export const REPORTING_DECLINE_LABEL = "No thanks";
export const REPORTING_ACCEPT_LABEL = "Yes, send reports";

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
