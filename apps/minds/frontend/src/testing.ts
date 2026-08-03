// Shared non-fixture test utilities, explicitly imported by *.test.ts files.
// Deliberately NOT named *.test.ts so vitest does not collect it as a suite.

/** A JSON Response carrying the given payload with the given status. */
export function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Run `run` with `globalThis.fetch` swapped for a stub that mimics the
 * browser's receiver check: it throws "Illegal invocation" unless invoked as
 * a plain call (as a model's default `fetchImpl` wrapper must), and otherwise
 * resolves to a JSON response carrying `payload`. Restores the real fetch. */
export async function withReceiverGuardedGlobalFetch(payload: unknown, run: () => Promise<void>): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = function (this: unknown) {
    if (this !== undefined && this !== globalThis) {
      throw new TypeError("Illegal invocation");
    }
    return Promise.resolve(jsonResponse(payload));
  } as typeof fetch;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

/** Flush pending promise work: three microtask hops cover the await chains
 * inside the models (fetch result -> body parse -> state application). */
export async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
