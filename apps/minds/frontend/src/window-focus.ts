// Shared "does this window currently have OS focus" resolution, used by
// anything that gates behavior on it (the channel's client_state focus
// report, the notification arrival controller's toast/OS-banner routing).

/** Resolve window focus via an injectable override (tests), or
 * document.hasFocus() otherwise. Node-env tests have no document; a missing
 * focus signal must not silence everything downstream, so absence reads as
 * focused. */
export function resolveWindowFocus(override: (() => boolean) | undefined): boolean {
  if (override !== undefined) return override();
  if (typeof document === "undefined" || typeof document.hasFocus !== "function")
    return true;
  return document.hasFocus();
}
