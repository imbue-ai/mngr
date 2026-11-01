// The account-keyed settings record's one write path: a partial PUT to
// /ui/api/settings/<account_id> (the update window, the auto-update toggle
// and its one-time offer's answer, the master notifications toggle). Every
// surface that changes one of these goes through here, so the endpoint cannot
// drift apart between them. Each account id writes its own synced record
// (the user-profile-state spec's settings account document).

import type { AutoUpdatePromptDecision } from "./autoUpdatePrompt";

/** The record as the PUT echoes it back. */
export interface AccountSettings {
  settings_format: number;
  update_window_start_hour: number;
  update_window_end_hour: number;
  auto_update_workspaces: boolean;
  auto_update_prompt_decision: AutoUpdatePromptDecision | null;
  notifications_enabled: boolean;
}

/** Any subset of the record's fields; the window hours come as a pair or not at all. */
export interface AccountSettingsWrite {
  update_window_start_hour?: number;
  update_window_end_hour?: number;
  auto_update_workspaces?: boolean;
  auto_update_prompt_decision?: AutoUpdatePromptDecision;
  notifications_enabled?: boolean;
}

// Narrower than `typeof fetch` so callers holding their own fetch-shaped
// hook (notificationsUi's, the test fakes) can pass it without a cast.
type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export function putAccountSettings(
  fetchImpl: FetchLike,
  accountId: string,
  write: AccountSettingsWrite,
): Promise<Response> {
  return fetchImpl(`/ui/api/settings/${encodeURIComponent(accountId)}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(write),
  });
}
