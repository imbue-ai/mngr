// The one query client the models share: the cache a slow read is answered
// from while a fresh read runs behind it, and the ledger of the writes in
// flight. Its answers outlive the surface that fetched them, so a panel opened
// again shows what it showed last time at once.

import { QueryClient } from "@tanstack/query-core";

/** How long an answer nobody is looking at is kept for the next reader. */
const UNWATCHED_ANSWER_LIFETIME_MS = 30 * 60_000;

export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Every read is a round trip to the desktop client on this machine,
        // which answers with the wifi off, so a network the browser reports
        // as down must not hold a read back.
        networkMode: "always",
        // A read here costs the desktop client a subprocess or an exec into
        // the workspace: a failure is reported where it was asked for rather
        // than paid for again, and nothing is re-read just because the window
        // was looked at again. A surface reads afresh when it opens.
        retry: false,
        refetchOnWindowFocus: false,
        staleTime: 0,
        gcTime: UNWATCHED_ANSWER_LIFETIME_MS,
      },
      mutations: { networkMode: "always", retry: false },
    },
  });
}

let appQueryClient: QueryClient | null = null;

/** The client every model shares, created on first use. Tests construct their
 * own with `createAppQueryClient` so no case can read another's answers. */
export function getAppQueryClient(): QueryClient {
  appQueryClient ??= createAppQueryClient();
  return appQueryClient;
}
