// The progress an operation reports (a workspace create, a connection's
// provider prepare), as the status polls carry it, and the display that turns
// polls two seconds apart into a bar that moves every frame.
//
// The server owns the arithmetic: its percent is the expected-duration-weighted
// share of completed steps and never decreases. Between polls the page
// extrapolates along the rate the server gave, but only up to the cap, which is
// where the running step's linear easing ends; past that only the next poll
// moves the bar. Keep in sync with create_progress.py.

export type ProgressStepStatus = "PENDING" | "RUNNING" | "DONE";

export interface ProgressStepSummary {
  step_id: string;
  label: string;
  status: ProgressStepStatus;
}

export interface OperationProgress {
  percent: number;
  percent_cap: number;
  percent_per_second: number;
  /** The remaining time, said coarsely; empty while nothing is known. */
  remaining_text: string;
  /** The caption of the innermost running step; empty before the first. */
  current_label: string;
  steps: ProgressStepSummary[];
}

/** The highest the bar is drawn while the operation still runs: completion alone fills it. */
export const MAX_RUNNING_PERCENT = 99.5;

/**
 * Folds progress polls into the percent to draw at any instant. Monotonic: a
 * poll that lands below what was already drawn (the server rounds its own
 * monotonic percent; an extrapolation ran slightly ahead) holds the bar
 * rather than pulling it back.
 */
export class ProgressDisplay {
  private latest: OperationProgress | null = null;
  private polledAtMs = 0;
  private drawnPercent = 0;

  get progress(): OperationProgress | null {
    return this.latest;
  }

  apply(progress: OperationProgress, nowMs: number): void {
    this.latest = progress;
    this.polledAtMs = nowMs;
  }

  /** The percent to draw now; 100 once the operation is done. */
  percentAt(nowMs: number, isDone: boolean): number {
    if (isDone) {
      this.drawnPercent = 100;
      return 100;
    }
    const latest = this.latest;
    if (latest === null) return this.drawnPercent;
    const elapsedSeconds = Math.max(0, nowMs - this.polledAtMs) / 1000;
    const extrapolated = latest.percent + latest.percent_per_second * elapsedSeconds;
    const bounded = Math.min(extrapolated, Math.max(latest.percent, latest.percent_cap), MAX_RUNNING_PERCENT);
    this.drawnPercent = Math.max(this.drawnPercent, bounded);
    return this.drawnPercent;
  }

  /** The caption to show under the bar: the running step's own, else `fallback`. */
  labelOr(fallback: string): string {
    const label = this.latest?.current_label ?? "";
    return label !== "" ? label : fallback;
  }

  remainingText(): string {
    return this.latest?.remaining_text ?? "";
  }
}

/** Whether a status body carries a progress object this page can draw. */
export function progressOf(data: { progress?: OperationProgress | null } | null): OperationProgress | null {
  const progress = data?.progress ?? null;
  if (progress === null || typeof progress.percent !== "number") return null;
  return progress;
}
