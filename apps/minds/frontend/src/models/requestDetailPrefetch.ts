// Warms a permission request's detail before the popup asks for it.
//
// Opening a request used to cost two round trips end to end -- the pending
// list, and only then the detail -- and the detail one is the slow half: the
// desktop client answers it by running the latchkey CLI for that service and
// letting it validate the stored credentials over the network. Nothing in the
// "Waiting on you" row covers that (the row has a title and a reason; the
// dialog needs the service's accounts and its permission catalog), so the
// popup genuinely has to go and ask.
//
// It does not have to ask LATE, though. The row is on screen and pointed at
// well before it is clicked, so the fetch starts on the way in and the click
// finds it done or nearly so.

import type { InboxDetail } from "./inbox";

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** In-flight and settled warms, by request id. Module-level because the
 * warming surface (the Permissions tab) and the surface that spends it (the
 * popup) are different components with no shared parent to hold it. */
const warmed = new Map<string, Promise<InboxDetail | null>>();

/** The host's fetch, or null where there is none (unit tests run the stores
 * under node). A warm is an optimization, so having nowhere to fetch from is a
 * reason to skip it, not to fail. */
function defaultFetcher(): FetchLike | null {
  if (typeof window === "undefined" || typeof window.fetch !== "function") return null;
  return (input, init) => window.fetch(input, init);
}

export function requestDetailUrl(id: string): string {
  return `/ui/api/inbox/${encodeURIComponent(id)}/detail`;
}

async function fetchDetail(id: string, fetcher: FetchLike): Promise<InboxDetail | null> {
  try {
    const response = await fetcher(requestDetailUrl(id));
    if (!response.ok) return null;
    const body = (await response.json()) as { detail: InboxDetail };
    return body.detail;
  } catch {
    // A warm is an optimization: a failure here is not reported anywhere, and
    // the popup's own fetch reports for real when the user actually opens it.
    return null;
  }
}

/**
 * Start fetching `id`'s detail if it is not already warm.
 *
 * Idempotent per id, so pointing at the same row repeatedly costs one fetch --
 * the server work behind it is a CLI process and a network probe, which is
 * exactly what must not be repeated per mouse event.
 */
export function warmRequestDetail(id: string, fetcher?: FetchLike): void {
  if (warmed.has(id)) return;
  const fetchWith = fetcher ?? defaultFetcher();
  if (fetchWith === null) return;
  warmed.set(id, fetchDetail(id, fetchWith));
}

/**
 * The warm for `id`, or null when there is none.
 *
 * Kept rather than consumed: a request stays pending until it is answered, so
 * closing the popup and opening the same request again is a normal thing to
 * do, and it should not be the slow one. `forgetWarmedRequestDetails` is what
 * ends a warm's life -- the request being resolved, or dropping out of the
 * pending set.
 */
export function readWarmedRequestDetail(id: string): Promise<InboxDetail | null> | null {
  return warmed.get(id) ?? null;
}

/** Drop every warm. Called when a request is resolved (what any of them offer
 * can change with it) and by tests, so one case's warm cannot answer the
 * next one's open. */
export function forgetWarmedRequestDetails(): void {
  warmed.clear();
}

/** Drop the warms for requests that are no longer pending, keeping the rest.
 * A request that has left the set cannot be opened, and its answer is stale. */
export function retainWarmedRequestDetails(pendingIds: readonly string[]): void {
  const keep = new Set(pendingIds);
  for (const id of [...warmed.keys()]) {
    if (!keep.has(id)) warmed.delete(id);
  }
}
