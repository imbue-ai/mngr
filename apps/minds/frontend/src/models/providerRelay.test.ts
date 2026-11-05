import { afterEach, describe, expect, it, vi } from "vitest";
import { armProviderRelay } from "./providerRelay";

function answerWith(response: Response | Error): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      if (response instanceof Error) throw response;
      return response;
    }),
  );
}

describe("armProviderRelay", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads a relaying answer as relaying, with no reason", async () => {
    answerWith(new Response(JSON.stringify({ relay: true, reason: null }), { status: 200 }));
    expect(await armProviderRelay("agent-ab12", "flow-61", "https://claude.ai/oauth/authorize")).toEqual({
      relay: true,
    });
  });

  it("carries the backend's reason for a refusal", async () => {
    answerWith(new Response(JSON.stringify({ relay: false, reason: "port_taken" }), { status: 200 }));
    expect(await armProviderRelay("agent-ab12", "flow-62", "https://claude.ai/oauth/authorize")).toEqual({
      relay: false,
      reason: "port_taken",
    });
  });

  it("reads a refused request or an unreachable backend as a refusal with no reason", async () => {
    answerWith(new Response(JSON.stringify({ error: "Not a provider sign-in" }), { status: 400 }));
    expect(await armProviderRelay("agent-ab12", "flow-63", "https://claude.ai/oauth/authorize")).toEqual({
      relay: false,
    });
    answerWith(new TypeError("Failed to fetch"));
    expect(await armProviderRelay("agent-ab12", "flow-64", "https://claude.ai/oauth/authorize")).toEqual({
      relay: false,
    });
  });
});
