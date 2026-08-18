import { afterEach, describe, expect, it, vi } from "vitest";
import { RequestsStore } from "./requests";
import { forgetWarmedRequestDetails, readWarmedRequestDetail } from "./requestDetailPrefetch";

afterEach(() => {
  forgetWarmedRequestDetails();
  vi.unstubAllGlobals();
});

function requestsMessage(ids: string[]) {
  return { type: "requests" as const, count: ids.length, request_ids: ids };
}

describe("RequestsStore", () => {
  it("records the pending set", () => {
    const store = new RequestsStore();

    store.applyRequestsMessage(requestsMessage(["evt-a", "evt-b"]));

    expect(store.requestIds).toEqual(["evt-a", "evt-b"]);
  });

  it("warms each pending request's detail, so any way in opens on data already here", () => {
    // The in-chat card's button and a "Waiting on you" row are different
    // surfaces with different hover stories; warming from the pending set
    // itself is what makes every one of them open on a request rather than a
    // spinner.
    const fetch = vi.fn(async (_url: string) => ({
      ok: true,
      json: async () => ({ detail: { kind: "predefined" } }),
    }));
    vi.stubGlobal("window", { fetch });
    const store = new RequestsStore();

    store.applyRequestsMessage(requestsMessage(["evt-a", "evt-b"]));

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      "/ui/api/inbox/evt-a/detail",
      "/ui/api/inbox/evt-b/detail",
    ]);
    expect(readWarmedRequestDetail("evt-a")).not.toBeNull();
    expect(readWarmedRequestDetail("evt-b")).not.toBeNull();
  });

  it("does not re-fetch a request it is already holding", () => {
    // The channel repeats the pending set on every push, so this is the common
    // case, and each fetch behind it is a latchkey probe on the machine.
    const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ detail: { kind: "predefined" } }) }));
    vi.stubGlobal("window", { fetch });
    const store = new RequestsStore();

    store.applyRequestsMessage(requestsMessage(["evt-a"]));
    store.applyRequestsMessage(requestsMessage(["evt-a"]));
    store.applyRequestsMessage(requestsMessage(["evt-a"]));

    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("records the set even where there is nothing to fetch with", () => {
    // Under node there is no window to fetch from; the store still tracks what
    // is pending, which is what every surface reads.
    const store = new RequestsStore();

    store.applyRequestsMessage(requestsMessage(["evt-a"]));

    expect(store.requestIds).toEqual(["evt-a"]);
    expect(readWarmedRequestDetail("evt-a")).toBeNull();
  });
});
