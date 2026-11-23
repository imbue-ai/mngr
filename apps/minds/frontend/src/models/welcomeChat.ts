// The creation conversation as the workspace's first chat. The creation page hands
// everything said so far (the manifesto exchange, the settings, the setup line, the
// recording an onboarding create shows, the welcome) to the desktop client, which seeds a
// chat inside the new workspace with it, so the conversation simply continues there. This
// module turns the page's transcript into that chat's turns, as markdown the chat app
// renders; the toggles become <details> blocks, which it allows.
//
// A first-run create (specs/ftue-onboarding-v2) sends no turns: the workspace plays its own
// arrival in the chat (specs/chat-suggestions), and the body only says that it is one.

import type { CreateAttemptRequestSummary } from "./create";
import {
  SETUP_LINE,
  SHOWCASE_APP,
  readyTurnMarkdown,
  showcaseMarkdown,
  summaryLines,
} from "./creationTranscript";
import type { DisclosurePoint } from "./manifesto";
import {
  MANIFESTO_HEADING,
  MANIFESTO_OPENER,
  MANIFESTO_OPENER_EMPHASIS,
  MANIFESTO_POINTS,
  MANIFESTO_QUESTION,
  REPORTING_ASK,
  REPORTING_MORE_DETAIL,
  REPORTING_MORE_LABEL,
} from "./manifesto";
import type { WelcomeChatLanding } from "./welcomeChatArrival";

export interface WelcomeChatTurn {
  role: "user" | "assistant";
  text: string;
}

export interface WelcomeChatBody {
  title: string;
  turns: WelcomeChatTurn[];
  /** Whether the workspace plays its first-run arrival in the chat instead of showing turns. */
  is_first_run?: boolean;
}

/** What the seeded chat is called in the workspace. */
export const WELCOME_CHAT_TITLE = "Welcome";

function user(text: string): WelcomeChatTurn {
  return { role: "user", text };
}

function assistant(text: string): WelcomeChatTurn {
  return { role: "assistant", text };
}

/** A point behind a chevron, as a toggle the chat app opens the same way. */
export function disclosureMarkdown(point: DisclosurePoint): string {
  return `<details><summary>${point.label}</summary>\n\n${point.detail}\n\n</details>`;
}

/** The opener with the phrase it is about emphasised, as the start flow draws it. */
export function manifestoOpenerMarkdown(): string {
  return MANIFESTO_OPENER.replace(MANIFESTO_OPENER_EMPHASIS, `*${MANIFESTO_OPENER_EMPHASIS}*`);
}

/** The manifesto exchange every workspace's conversation opens with, whichever page it started on. */
export function manifestoTurns(): WelcomeChatTurn[] {
  return [
    assistant(manifestoOpenerMarkdown()),
    user(MANIFESTO_QUESTION),
    assistant(
      [
        `**${MANIFESTO_HEADING}**`,
        "",
        ...MANIFESTO_POINTS.map((point) => disclosureMarkdown(point)),
      ].join("\n"),
    ),
    assistant(
      [
        REPORTING_ASK,
        "",
        `<details><summary>${REPORTING_MORE_LABEL}</summary>\n\n*${REPORTING_MORE_DETAIL}*\n\n</details>`,
      ].join("\n"),
    ),
  ];
}

/**
 * The creation page's own turns: the settings, the setup line, the recording when the page showed one
 * (``isShowcaseShown``, the attempt's ``is_onboarding_driven_by_agent``), and the welcome.
 */
export function creationTurns(request: CreateAttemptRequestSummary, isShowcaseShown: boolean): WelcomeChatTurn[] {
  return [
    user(summaryLines(request).join("\n")),
    assistant(SETUP_LINE),
    ...(isShowcaseShown ? [assistant(showcaseMarkdown(SHOWCASE_APP))] : []),
    assistant(readyTurnMarkdown()),
  ];
}

/** The whole conversation so far, as the chat the workspace opens on. */
export function welcomeChatBody(request: CreateAttemptRequestSummary, isShowcaseShown: boolean): WelcomeChatBody {
  return {
    title: WELCOME_CHAT_TITLE,
    turns: [...manifestoTurns(), ...creationTurns(request, isShowcaseShown)],
  };
}

/** A first-run create's chat: no turns, and the arrival flag. */
export function firstRunWelcomeChatBody(): WelcomeChatBody {
  return { title: WELCOME_CHAT_TITLE, turns: [], is_first_run: true };
}

/**
 * What a refusal's body says, as a suffix for the error: the route's message and, on a
 * 502, the seeding script's own verdict under `detail`. Empty when the body carries neither.
 */
async function refusalReason(response: Response): Promise<string> {
  const body = (await response.json().catch(() => ({}))) as {
    error?: unknown;
    detail?: unknown;
  };
  const parts = [body.error, body.detail].filter(
    (part): part is string => typeof part === "string" && part !== "",
  );
  return parts.length === 0 ? "" : `: ${parts.join(" -- ")}`;
}

/**
 * Hand the conversation to the attempt's workspace. Resolves with the seeded chat's id once
 * the attempt is done, or with "" while it is still being built: the desktop client holds the
 * conversation and seeds it when the attempt finishes.
 */
export async function postWelcomeChat(
  createAttemptId: string,
  body: WelcomeChatBody,
  fetcher: (url: string, init?: RequestInit) => Promise<Response> = (
    url,
    init,
  ) => fetch(url, init),
): Promise<string> {
  const url = `/ui/api/create/attempts/${encodeURIComponent(createAttemptId)}/welcome-chat`;
  const response = await fetcher(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(
      `POST ${url} failed (${response.status})${await refusalReason(response)}`,
    );
  }
  const payload = (await response.json()) as { chat_id?: unknown };
  if (typeof payload.chat_id !== "string")
    throw new Error(`POST ${url} answered without a chat id`);
  return payload.chat_id;
}

/** Tell the desktop client how the wash over the new workspace came to lift, for its log. */
export async function postWelcomeChatLanding(
  createAttemptId: string,
  landing: WelcomeChatLanding,
  waitedMs: number,
): Promise<void> {
  const url = `/ui/api/create/attempts/${encodeURIComponent(createAttemptId)}/welcome-chat/landing`;
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ landing, waited_ms: waitedMs }),
  });
  if (!response.ok) throw new Error(`POST ${url} failed (${response.status})`);
}
