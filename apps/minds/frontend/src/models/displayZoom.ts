// Keeps the surfaces that show the display zoom (Settings > Display, the
// sidebar menu's Zoom row) current when it changes elsewhere: from a zoom
// shortcut, another window, or the other surface. Main's change listener has
// no unregister, so it is registered once and fans out to whoever is
// subscribed now.

import { electronBridge } from "../electron-bridge";

const listeners = new Set<(percent: number) => void>();
let isRegistered = false;

/** Hear every display zoom change while subscribed; returns the unsubscribe. */
export function subscribeToDisplayZoom(
  listener: (percent: number) => void,
): () => void {
  if (!isRegistered && electronBridge.stepsDisplayZoom) {
    isRegistered = true;
    electronBridge.onDisplayZoomChanged((percent) => {
      for (const subscribed of [...listeners]) subscribed(percent);
    });
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
