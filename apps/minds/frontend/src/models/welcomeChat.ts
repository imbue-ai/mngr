// The onboarding conversation as the workspace's first chat. Once a create attempt is
// done, the creation page hands everything said so far (the manifesto exchange, the
// questions and their answers, the settings, the setup and ready lines) to the desktop
// client, which seeds a chat inside the new workspace with it, so the conversation simply
// continues there. This module turns the page's transcript into that chat's turns, as
// markdown the chat app renders; the toggles become <details> blocks, which it allows.

import type { CreateAttemptRequestSummary } from "./create";
import {
  SETUP_LINE,
  SETUP_SECTIONS,
  readyTurnMarkdown,
  summaryLines,
} from "./creationTranscript";
import type {
  DisclosurePoint,
  TableColumn,
  TranscriptEntry,
} from "./startFlow";
import {
  FLOW,
  MANIFESTO_HEADING,
  MANIFESTO_POINTS,
  MANIFESTO_QUESTION,
  stepText,
} from "./startFlow";

export interface WelcomeChatTurn {
  role: "user" | "assistant";
  text: string;
}

export interface WelcomeChatBody {
  title: string;
  turns: WelcomeChatTurn[];
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

/** A comparison column as a titled bullet list. */
export function tableColumnMarkdown(column: TableColumn): string {
  const title = column.badge
    ? `**${column.title}** (${column.badge})`
    : `**${column.title}**`;
  return [title, ...column.points.map((point) => `- ${point}`)].join("\n");
}

/** The manifesto exchange every workspace's conversation opens with, whichever page it started on. */
export function manifestoTurns(): WelcomeChatTurn[] {
  return [
    user(MANIFESTO_QUESTION),
    assistant(
      [
        `**${MANIFESTO_HEADING}**`,
        "",
        ...MANIFESTO_POINTS.map((point) => disclosureMarkdown(point)),
      ].join("\n"),
    ),
  ];
}

/** The start flow's questions and answers, in order, one turn per side. */
export function flowTurns(entries: TranscriptEntry[]): WelcomeChatTurn[] {
  const turns: WelcomeChatTurn[] = [];
  for (const entry of entries) {
    if (entry.kind === "said") {
      turns.push(user(entry.text));
      continue;
    }
    if (entry.kind === "note") {
      turns.push(assistant(entry.text));
      continue;
    }
    const step = FLOW[entry.id];
    const { lead, body } = stepText(step, entry.ack);
    const parts: string[] = [];
    if (lead !== "") parts.push(`**${lead}**`);
    if (body !== "") parts.push(body);
    if (step.table) parts.push(...step.table.map(tableColumnMarkdown));
    if (step.prompt) parts.push(step.prompt);
    turns.push(assistant(parts.join("\n\n")));
    if (entry.answer !== null && entry.said !== "")
      turns.push(user(entry.said));
  }
  return turns;
}

/** The creation page's own turns: the settings, the setup line with its reading material, and the ready line with its list. */
export function creationTurns(
  request: CreateAttemptRequestSummary,
  isCloudPreset: boolean,
): WelcomeChatTurn[] {
  const guide = SETUP_SECTIONS.map((section) => disclosureMarkdown(section));
  return [
    user(summaryLines(request, isCloudPreset).join("\n")),
    assistant([SETUP_LINE, "", ...guide].join("\n")),
    assistant(readyTurnMarkdown()),
  ];
}

/** The whole conversation so far, as the chat the workspace opens on. */
export function welcomeChatBody(
  entries: TranscriptEntry[],
  request: CreateAttemptRequestSummary,
  // Whether the create was the start flow's Imbue Cloud answer, which the settings turn restates as that one choice.
  isCloudPreset: boolean,
): WelcomeChatBody {
  return {
    title: WELCOME_CHAT_TITLE,
    turns: [
      ...manifestoTurns(),
      ...flowTurns(entries),
      ...creationTurns(request, isCloudPreset),
    ],
  };
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

/** Hand the conversation to the finished attempt's workspace; resolves with the seeded chat's id. */
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
