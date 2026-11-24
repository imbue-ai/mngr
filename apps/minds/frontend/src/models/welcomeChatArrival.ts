// Knowing when a new workspace's welcome chat, or the AI-provider chooser, is
// really on screen, so the cover over the workspace offers Continue only then.
// Outlives the creation page, like the cover it readies: the page is gone once
// the workspace is mounted, and the answer only comes from the workspace's
// frame after that.

/** Far above a healthy create's seed and first paint, so only a chat that is not coming waits this long. */
export const WELCOME_CHAT_WAIT_BOUND_MS = 45_000;

/** How the wait ended; the values are what the desktop client's landing route takes. */
export type WelcomeChatLanding =
  | "ON_SCREEN"
  | "PROVIDER_CHOOSER_ON_SCREEN"
  | "SEED_FAILED"
  | "NO_CHAT_REPORT"
  | "TIMED_OUT";

/** What a workspace's page promised to report when it announced itself (minds:workspace-ready). */
export interface WorkspaceReporting {
  reportsChatOnScreen: boolean;
  reportsProviderChooserOnScreen: boolean;
}

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
   * Wait for the chat `seed` names, or the provider chooser, to be reported on screen. `seed`
   * answers null for a seed that failed. Resolves with how the wait ended, or null when it was
   * cancelled or a later one took over.
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

  /**
   * The provider chooser is on screen. The user's next step is to pick a provider whichever chat is
   * behind it, so the wait ends without the seed's answer.
   */
  noteProviderChooserOnScreen(): void {
    if (this.wait !== null) this.finish(this.wait, "PROVIDER_CHOOSER_ON_SCREEN");
  }

  /** The workspace's page announced itself; one that does not promise the chat report will never send it. */
  noteWorkspaceReady(reporting: WorkspaceReporting): void {
    const wait = this.wait;
    if (wait === null || reporting.reportsChatOnScreen) return;
    wait.isWorkspaceSilent = true;
    this.finishIfAnswered(wait);
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
