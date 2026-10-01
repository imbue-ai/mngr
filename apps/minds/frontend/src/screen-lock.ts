// Whether the OS reports the screen locked, as Electron main relays it (the
// desktop app on macOS and Windows; everywhere else it stays unlocked). The
// backend's banner gate stops trusting window focus and watched chats while
// any window reports a lock.

let isLocked = false;

/** Record the lock state Electron main relayed. */
export function setScreenLocked(locked: boolean): void {
  isLocked = locked;
}

export function isScreenLocked(): boolean {
  return isLocked;
}
