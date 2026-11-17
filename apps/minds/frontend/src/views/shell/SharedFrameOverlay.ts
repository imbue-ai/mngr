// The notice drawn over a shared workspace's frame from its open until the
// workspace's own page is up (specs/shared-with-me, 4.7): the frame goes
// through the sign-in's redirects on the way in, which must never show as a
// blank frame or as the accounts origin's sign-in form.

import m from "mithril";
import type { SharedFrameProgress } from "./shell-state";
import { verdictLabelFor } from "../../models/sharedWithMe";
import { Button } from "../components/Button";
import { Spinner } from "../components/Spinner";

// How long an open may take before the overlay admits it is slow and offers Retry.
export const SHARED_OPEN_STALL_MS = 20_000;

export interface SharedFrameOverlayAttrs {
  progress: SharedFrameProgress;
  /** The shared workspace's name, as its card shows it. */
  workspaceName: string;
  nowMs: number;
  onRetry: () => void;
}

/** Whether a connecting open has run long enough for the overlay to say so. */
export function isSharedOpenStalled(progress: SharedFrameProgress, nowMs: number): boolean {
  return progress.phase === "connecting" && nowMs - progress.startedAtMs >= SHARED_OPEN_STALL_MS;
}

/** How long until a connecting open stalls, for the redraw that turns the overlay to its stalled state; null when
 * no such redraw is due (the open is not connecting, or has stalled already). */
export function sharedStallDelayMs(progress: SharedFrameProgress, nowMs: number): number | null {
  if (progress.phase !== "connecting" || isSharedOpenStalled(progress, nowMs)) return null;
  return progress.startedAtMs + SHARED_OPEN_STALL_MS - nowMs;
}

export function SharedFrameOverlay(): m.Component<SharedFrameOverlayAttrs> {
  return {
    view(vnode) {
      const { progress, workspaceName, nowMs, onRetry } = vnode.attrs;
      const isFailed = progress.phase === "failed";
      const isStalled = isSharedOpenStalled(progress, nowMs);
      const heading = isFailed ? `Couldn't open ${workspaceName}` : `Connecting to ${workspaceName}…`;
      const sentence = isFailed
        ? progress.refusalMessage ?? verdictLabelFor(progress.verdict) ?? "The workspace did not let this account in."
        : isStalled
          ? "Still connecting. The workspace may be slow to answer."
          : null;
      return m(
        "div#shared-frame-overlay",
        {
          class: "workspace-surface bg-surface-primary flex items-center justify-center p-6",
          "data-shared-open-phase": isFailed ? "failed" : isStalled ? "stalled" : "connecting",
        },
        m(
          "div",
          {
            class:
              "w-[560px] max-w-full flex flex-col gap-3 rounded-[12px] border border-subtle " +
              "bg-surface-primary shadow-overlay px-6 py-5",
          },
          [
            m("div", { class: "flex items-center gap-3 type-heading" }, [
              isFailed ? null : m(Spinner, { size: "md" }),
              m("span", heading),
            ]),
            sentence === null ? null : m("p", { class: "m-0 type-body text-secondary" }, sentence),
            isFailed || isStalled
              ? m(
                  "div",
                  { class: "flex items-center gap-2" },
                  m(Button, { variant: "secondary", id: "shared-frame-retry", onclick: onRetry }, "Retry"),
                )
              : null,
          ],
        ),
      );
    },
  };
}
