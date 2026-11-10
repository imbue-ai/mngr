import { describe, expect, it } from "vitest";
import { settle } from "../testing";
import { WELCOME_CHAT_WAIT_BOUND_MS, WelcomeChatArrival } from "./welcomeChatArrival";
import type { WelcomeChatLanding } from "./welcomeChatArrival";

const CHAT_ID = "agent-0123abcd";

function makeArrival(): {
  arrival: WelcomeChatArrival;
  boundDelays: number[];
  fireBound: () => void;
  answerSeed: (chatId: string | null) => void;
  landings: (WelcomeChatLanding | null)[];
} {
  const boundDelays: number[] = [];
  let bound: (() => void) | null = null;
  let timerCount = 0;
  const arrival = new WelcomeChatArrival(
    (handler, delayMs) => {
      boundDelays.push(delayMs);
      bound = handler;
      timerCount += 1;
      return timerCount as unknown as ReturnType<typeof setTimeout>;
    },
    () => {
      bound = null;
    },
  );
  let answerSeed: (chatId: string | null) => void = () => undefined;
  const seed = new Promise<string | null>((resolve) => {
    answerSeed = resolve;
  });
  const landings: (WelcomeChatLanding | null)[] = [];
  void arrival.begin(seed).then((landing) => landings.push(landing));
  return {
    arrival,
    boundDelays,
    fireBound: () => bound?.(),
    answerSeed: (chatId) => answerSeed(chatId),
    landings,
  };
}

describe("WelcomeChatArrival", () => {
  it("lands only once the seeded chat itself is reported on screen", async () => {
    const { arrival, answerSeed, landings } = makeArrival();
    answerSeed(CHAT_ID);
    await settle();
    expect(landings).toEqual([]);

    arrival.noteChatOnScreen("agent-99999999");
    await settle();
    expect(landings).toEqual([]);

    arrival.noteChatOnScreen(CHAT_ID);
    await settle();
    expect(landings).toEqual(["ON_SCREEN"]);
  });

  it("keeps a report that arrives before the seed's answer", async () => {
    const { arrival, answerSeed, landings } = makeArrival();
    arrival.noteChatOnScreen(CHAT_ID);
    await settle();
    expect(landings).toEqual([]);

    answerSeed(CHAT_ID);
    await settle();
    expect(landings).toEqual(["ON_SCREEN"]);
  });

  it("does not take a workspace that promises the report for one that will never send it", async () => {
    const { arrival, answerSeed, landings } = makeArrival();
    arrival.noteWorkspaceReady(true);
    answerSeed(CHAT_ID);
    await settle();
    expect(landings).toEqual([]);
  });

  it("lands once the seed answers for a workspace that cannot report, in either order", async () => {
    const readyFirst = makeArrival();
    readyFirst.arrival.noteWorkspaceReady(false);
    await settle();
    expect(readyFirst.landings).toEqual([]);
    readyFirst.answerSeed(CHAT_ID);
    await settle();
    expect(readyFirst.landings).toEqual(["NO_CHAT_REPORT"]);

    const seedFirst = makeArrival();
    seedFirst.answerSeed(CHAT_ID);
    await settle();
    seedFirst.arrival.noteWorkspaceReady(false);
    await settle();
    expect(seedFirst.landings).toEqual(["NO_CHAT_REPORT"]);
  });

  it("lands at once on a seed that definitively failed", async () => {
    const { answerSeed, landings } = makeArrival();
    answerSeed(null);
    await settle();
    expect(landings).toEqual(["SEED_FAILED"]);
  });

  it("lands at the bound when nothing answers, so the user is never stranded", async () => {
    const { boundDelays, fireBound, landings } = makeArrival();
    expect(boundDelays).toEqual([WELCOME_CHAT_WAIT_BOUND_MS]);
    expect(landings).toEqual([]);
    fireBound();
    await settle();
    expect(landings).toEqual(["TIMED_OUT"]);
  });

  it("answers null when cancelled and ignores everything after", async () => {
    const { arrival, answerSeed, fireBound, landings } = makeArrival();
    arrival.cancel();
    answerSeed(CHAT_ID);
    arrival.noteChatOnScreen(CHAT_ID);
    fireBound();
    await settle();
    expect(landings).toEqual([null]);
  });
});
