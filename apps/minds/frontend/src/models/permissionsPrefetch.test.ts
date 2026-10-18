import { afterEach, describe, expect, it, vi } from "vitest";
import {
  forgetWarmedPermissionsOverview,
  permissionsOverviewUrl,
  readWarmedPermissionsOverview,
  warmPermissionsOverview,
} from "./permissionsPrefetch";

afterEach(() => {
  forgetWarmedPermissionsOverview();
  vi.useRealTimers();
});

const AGENT = "agent-ab12";

function okFetch() {
  return vi.fn(async (_url: string) => ({ ok: true, status: 200, body: { connections: [] } }));
}

describe("warmPermissionsOverview", () => {
  it("reads the overview the pane would read", async () => {
    const fetchJson = okFetch();

    warmPermissionsOverview(AGENT, fetchJson);

    expect(fetchJson).toHaveBeenCalledWith(permissionsOverviewUrl(AGENT));
    await expect(readWarmedPermissionsOverview(AGENT)).resolves.toEqual({
      ok: true,
      status: 200,
      body: { connections: [] },
    });
  });

  it("reads once however many times the key is pointed at", async () => {
    // Each read asks the machine's gateway what it holds, so a read per mouse
    // event is exactly what a warm must not cost.
    const fetchJson = okFetch();

    warmPermissionsOverview(AGENT, fetchJson);
    warmPermissionsOverview(AGENT, fetchJson);
    warmPermissionsOverview(AGENT, fetchJson);

    expect(fetchJson).toHaveBeenCalledTimes(1);
  });

  it("answers only for the machine it was read for", () => {
    const fetchJson = okFetch();
    warmPermissionsOverview(AGENT, fetchJson);

    expect(readWarmedPermissionsOverview("agent-other")).toBeNull();
    expect(readWarmedPermissionsOverview(AGENT)).not.toBeNull();
  });

  it("replaces a warm when another machine's key is pointed at", () => {
    // One window shows one panel, so the second machine is the one being opened.
    const fetchJson = okFetch();
    warmPermissionsOverview(AGENT, fetchJson);

    warmPermissionsOverview("agent-cd34", fetchJson);

    expect(readWarmedPermissionsOverview(AGENT)).toBeNull();
    expect(readWarmedPermissionsOverview("agent-cd34")).not.toBeNull();
  });

  it("stops answering once it is too old to trust", () => {
    // It covers a hand moving to a key. Past that, what it holds (a grant, a
    // pending request) has had time to change, and the pane reads for itself.
    vi.useFakeTimers();
    const fetchJson = okFetch();
    warmPermissionsOverview(AGENT, fetchJson);
    expect(readWarmedPermissionsOverview(AGENT)).not.toBeNull();

    vi.advanceTimersByTime(15_001);

    expect(readWarmedPermissionsOverview(AGENT)).toBeNull();
  });

  it("re-reads for a machine whose warm has expired", () => {
    vi.useFakeTimers();
    const fetchJson = okFetch();
    warmPermissionsOverview(AGENT, fetchJson);

    vi.advanceTimersByTime(15_001);
    warmPermissionsOverview(AGENT, fetchJson);

    expect(fetchJson).toHaveBeenCalledTimes(2);
    expect(readWarmedPermissionsOverview(AGENT)).not.toBeNull();
  });

  it("forgets the warm when asked", () => {
    warmPermissionsOverview(AGENT, okFetch());

    forgetWarmedPermissionsOverview();

    expect(readWarmedPermissionsOverview(AGENT)).toBeNull();
  });
});
