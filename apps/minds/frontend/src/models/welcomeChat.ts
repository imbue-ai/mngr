// The new workspace's first chat. Every creation page asks the desktop client to seed its workspace with the
// first-run arrival (specs/onboarding-tweaks, 2.4): the workspace then owes its owner the provider page and opens
// its main chat on its own first conversation. The seed's body is the desktop client's, so the page sends none.

import type { WelcomeChatLanding } from "./welcomeChatArrival";

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
 * Ask for the attempt's workspace to be seeded. Resolves with the seeded chat's id once
 * the attempt is done, or with "" while it is still being built: the desktop client owes
 * the seed from then on and runs it when the attempt finishes.
 */
export async function postWelcomeChat(
  createAttemptId: string,
  fetcher: (url: string, init?: RequestInit) => Promise<Response> = (
    url,
    init,
  ) => fetch(url, init),
): Promise<string> {
  const url = `/ui/api/create/attempts/${encodeURIComponent(createAttemptId)}/welcome-chat`;
  const response = await fetcher(url, { method: "POST", credentials: "same-origin" });
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

/** Tell the desktop client how the cover over the new workspace came to lift, for its log. */
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
