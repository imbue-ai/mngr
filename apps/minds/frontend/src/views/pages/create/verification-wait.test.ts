import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VerificationWait } from "./verification-wait";

const POLL_MS = 100;

type Verdict = boolean | "unreachable";

interface Harness {
  wait: VerificationWait;
  verdicts: Verdict[];
  resent: string[];
  raises: number;
  created: number;
  unchecked: number;
}

/**
 * `verdicts` answers the checks in order (the last one repeats); an "unreachable" entry fails that
 * check, and an empty list makes every check fail.
 */
function harness(verdicts: Verdict[], isResendSent = true): Harness {
  const counters: Omit<Harness, "wait"> = { verdicts, resent: [], raises: 0, created: 0, unchecked: 0 };
  const wait = new VerificationWait({
    isEmailVerified: () => {
      const verdict = counters.verdicts.length > 1 ? counters.verdicts.shift() : counters.verdicts[0];
      if (verdict === undefined || verdict === "unreachable") return Promise.reject(new Error("unreachable"));
      return Promise.resolve(verdict);
    },
    resendVerificationEmail: (email) => {
      counters.resent.push(email);
      return Promise.resolve(isResendSent);
    },
    redraw: () => undefined,
    bringAppToFront: () => {
      counters.raises += 1;
    },
    pollMs: POLL_MS,
  });
  return Object.assign(counters, { wait });
}

function requireCreate(state: Harness): void {
  state.wait.require(
    "new@example.com",
    () => {
      state.created += 1;
    },
    () => {
      state.unchecked += 1;
    },
  );
}

describe("VerificationWait", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("creates at once for a verified email, sending nothing and waiting on nothing", async () => {
    const state = harness([true]);
    requireCreate(state);
    await vi.advanceTimersByTimeAsync(0);

    expect(state.created).toBe(1);
    expect(state.wait.email).toBeNull();
    expect(state.resent).toEqual([]);
  });

  it("does not create when the check cannot be made", async () => {
    const state = harness([]);
    requireCreate(state);
    await vi.advanceTimersByTimeAsync(0);

    expect(state.created).toBe(0);
    expect(state.unchecked).toBe(1);
    expect(state.wait.email).toBeNull();
  });

  it("sends the link for an unverified email and creates once, when it is clicked", async () => {
    const state = harness([false, false, true]);
    requireCreate(state);
    await vi.advanceTimersByTimeAsync(0);

    expect(state.wait.email).toBe("new@example.com");
    expect(state.resent).toEqual(["new@example.com"]);
    expect(state.created).toBe(0);

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(state.created).toBe(0);

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(state.created).toBe(1);
    expect(state.raises).toBe(1);
    expect(state.wait.email).toBeNull();

    await vi.advanceTimersByTimeAsync(POLL_MS * 5);
    expect(state.created).toBe(1);
  });

  it("keeps waiting through a poll that cannot check, unlike a first check that cannot", async () => {
    const state = harness([false, "unreachable", true]);
    requireCreate(state);
    await vi.advanceTimersByTimeAsync(0);

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(state.wait.email).toBe("new@example.com");
    expect(state.created).toBe(0);
    expect(state.unchecked).toBe(0);

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(state.created).toBe(1);
    expect(state.unchecked).toBe(0);
  });

  it("drops the create when the wait is cancelled", async () => {
    const state = harness([false, true]);
    requireCreate(state);
    await vi.advanceTimersByTimeAsync(0);
    state.wait.cancel();

    await vi.advanceTimersByTimeAsync(POLL_MS * 5);
    expect(state.created).toBe(0);
    expect(state.wait.email).toBeNull();
  });

  it("drops a first check that answers after a cancel", async () => {
    const state = harness([true]);
    requireCreate(state);
    state.wait.cancel();
    await vi.advanceTimersByTimeAsync(0);

    expect(state.created).toBe(0);
  });

  it("reports whether a resend went out", async () => {
    const state = harness([false], false);
    requireCreate(state);
    await vi.advanceTimersByTimeAsync(0);
    expect(state.wait.isResendSent).toBeNull();

    state.wait.resend();
    await vi.advanceTimersByTimeAsync(0);

    expect(state.resent).toEqual(["new@example.com", "new@example.com"]);
    expect(state.wait.isResendSent).toBe(false);
    state.wait.cancel();
  });
});
