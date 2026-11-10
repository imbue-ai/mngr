// Knowing when a new workspace's welcome chat is really on screen, so the wash
// over the workspace can stay up until then. Outlives the creation page, like
// the wash it releases: the page is gone once the workspace is mounted, and the
// answer only comes from the workspace's frame after that.

/** Far above a healthy create's seed and first paint, so only a chat that is not coming waits this long. */
export const WELCOME_CHAT_WAIT_BOUND_MS = 45_000;

/** How the wait ended; the values are what the desktop client's landing route takes. */
export type WelcomeChatLanding =
  | "ON_SCREEN"
  | "SEED_FAILED"
  | "NO_CHAT_REPORT"
  | "TIMED_OUT";

interface ArrivalWait {
  chatId: string | null;
  isWorkspaceSilent: boolean;
  readonly onScreenChatIds: Set<string>;
  readonly settle: (landing: WelcomeChatLanding | null) => void;
  timer: ReturnType<typeof setTimeout> | null;
}

export class WelcomeChatArrival {
  private wait: ArrivalWait | null = null;
  private readonly setTimer: (handler: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  private readonly clearTimer: (timer: ReturnType<typeof setTimeout>) => void;

  constructor(
    setTimer: (handler: () => void, delayMs: number) => ReturnType<typeof setTimeout> = (handler, delayMs) =>
      setTimeout(handler, delayMs),
    clearTimer: (timer: ReturnType<typeof setTimeout>) => void = (timer) => clearTimeout(timer),
  ) {
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
  }

  /**
   * Wait for the chat `seed` names to be reported on screen. `seed` answers null for a seed that
   * failed. Resolves with how the wait ended, or null when it was cancelled or a later one took over.
   */
  begin(seed: Promise<string | null>): Promise<WelcomeChatLanding | null> {
    this.cancel();
    return new Promise((resolve) => {
      const wait: ArrivalWait = {
        chatId: null,
        isWorkspaceSilent: false,
        onScreenChatIds: new Set<string>(),
        settle: resolve,
        timer: null,
      };
      this.wait = wait;
      wait.timer = this.setTimer(() => this.finish(wait, "TIMED_OUT"), WELCOME_CHAT_WAIT_BOUND_MS);
      void seed.then((chatId) => {
        if (chatId === null) {
          this.finish(wait, "SEED_FAILED");
          return;
        }
        wait.chatId = chatId;
        this.finishIfAnswered(wait);
      });
    });
  }

  noteChatOnScreen(chatId: string): void {
    if (this.wait === null) return;
    this.wait.onScreenChatIds.add(chatId);
    this.finishIfAnswered(this.wait);
  }

  /** The workspace's page announced itself; one that does not promise the report will never send it. */
  noteWorkspaceReady(reportsChatOnScreen: boolean): void {
    if (this.wait === null || reportsChatOnScreen) return;
    this.wait.isWorkspaceSilent = true;
    this.finishIfAnswered(this.wait);
  }

  cancel(): void {
    if (this.wait !== null) this.finish(this.wait, null);
  }

  private finishIfAnswered(wait: ArrivalWait): void {
    if (wait.chatId === null) return;
    if (wait.onScreenChatIds.has(wait.chatId)) this.finish(wait, "ON_SCREEN");
    else if (wait.isWorkspaceSilent) this.finish(wait, "NO_CHAT_REPORT");
  }

  private finish(wait: ArrivalWait, landing: WelcomeChatLanding | null): void {
    if (this.wait !== wait) return;
    this.wait = null;
    if (wait.timer !== null) this.clearTimer(wait.timer);
    wait.settle(landing);
  }
}

/** The one wait a window can run at a time. */
export const welcomeChatArrival = new WelcomeChatArrival();
