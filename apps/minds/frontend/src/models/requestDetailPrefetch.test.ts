import { afterEach, describe, expect, it, vi } from "vitest";
import {
  forgetWarmedRequestDetails,
  readWarmedRequestDetail,
  requestDetailUrl,
  retainWarmedRequestDetails,
  warmRequestDetail,
} from "./requestDetailPrefetch";

afterEach(() => {
  forgetWarmedRequestDetails();
});

function okWith(detail: unknown): Response {
  return { ok: true, json: async () => ({ detail }) } as unknown as Response;
}

describe("warmRequestDetail", () => {
  it("fetches the detail the popup would ask for", async () => {
    const fetcher = vi.fn(async () => okWith({ kind: "predefined" }));

    warmRequestDetail("evt-1", fetcher);

    expect(fetcher).toHaveBeenCalledWith(requestDetailUrl("evt-1"));
    await expect(readWarmedRequestDetail("evt-1")).resolves.toEqual({ kind: "predefined" });
  });

  it("fetches once however many times the row is pointed at", async () => {
    // The server answers this by running the latchkey CLI and letting it probe
    // the service, so a fetch per mouse event is exactly what must not happen.
    const fetcher = vi.fn(async () => okWith({ kind: "predefined" }));

    warmRequestDetail("evt-1", fetcher);
    warmRequestDetail("evt-1", fetcher);
    warmRequestDetail("evt-1", fetcher);

    expect(fetcher).toHaveBeenCalledTimes(1);
    await readWarmedRequestDetail("evt-1");
  });

  it("keeps the warm for a reopen, which is the same pending request", async () => {
    // Closing the popup and opening the same request again is ordinary; it was
    // the slow open before, because the first open spent the warm.
    const fetcher = vi.fn(async () => okWith({ kind: "predefined" }));
    warmRequestDetail("evt-1", fetcher);

    await readWarmedRequestDetail("evt-1");

    expect(readWarmedRequestDetail("evt-1")).not.toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("drops the warms for requests that are no longer pending", async () => {
    // Their answers cannot be opened any more, and would be stale if they were.
    const fetcher = vi.fn(async () => okWith({ kind: "predefined" }));
    warmRequestDetail("evt-1", fetcher);
    warmRequestDetail("evt-2", fetcher);

    retainWarmedRequestDetails(["evt-2"]);

    expect(readWarmedRequestDetail("evt-1")).toBeNull();
    expect(readWarmedRequestDetail("evt-2")).not.toBeNull();
  });

  it("reports nothing warmed for a request nobody pointed at", () => {
    expect(readWarmedRequestDetail("evt-never")).toBeNull();
  });

  it("resolves to null when the warm fails, so the open can report for itself", async () => {
    // A warm is an optimization with no surface of its own: a failure here must
    // not be what the user is told, and must not stand in for a real answer.
    const failing = vi.fn(async () => ({ ok: false }) as unknown as Response);
    warmRequestDetail("evt-bad", failing);
    await expect(readWarmedRequestDetail("evt-bad")).resolves.toBeNull();

    const throwing = vi.fn(async () => {
      throw new Error("offline");
    });
    warmRequestDetail("evt-throw", throwing);
    await expect(readWarmedRequestDetail("evt-throw")).resolves.toBeNull();
  });

  it("forgets every warm when asked", async () => {
    const fetcher = vi.fn(async () => okWith({ kind: "predefined" }));
    warmRequestDetail("evt-1", fetcher);
    warmRequestDetail("evt-2", fetcher);

    forgetWarmedRequestDetails();

    expect(readWarmedRequestDetail("evt-1")).toBeNull();
    expect(readWarmedRequestDetail("evt-2")).toBeNull();
  });
});
