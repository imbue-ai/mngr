// Hide the pointer while text is being typed, so it never sits over the
// words being entered. Chromium does not do this itself (macOS does it for
// native text fields, but not for web content), so the page toggles a class
// on <html> whose rule in style.css sets ``cursor: none`` everywhere, and
// drops it again on the first real pointer movement.
//
// This document only: ``cursor`` does not cross into the workspace iframe,
// whose own pages install the same behavior for themselves.

/** The class on <html> while the pointer is hidden. */
export const CURSOR_HIDDEN_CLASS = "typing-hides-cursor";

// "Process" is what Chromium reports for every key an input method editor is
// composing with (CJK input), in place of the character.
const TEXT_EDITING_NAMED_KEYS = new Set([
  "Backspace",
  "Delete",
  "Enter",
  "Process",
]);

const NON_TEXT_INPUT_TYPES = new Set([
  "button",
  "checkbox",
  "color",
  "file",
  "hidden",
  "image",
  "radio",
  "range",
  "reset",
  "submit",
]);

/** Whether a keystroke inserts or deletes text: a printable character or one
 * of the editing keys, with no command modifier held (shift is fine). A
 * shortcut like Cmd+C or a bare modifier press leaves the pointer alone. */
export function isTextEditingKeystroke(
  event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey">,
): boolean {
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  return event.key.length === 1 || TEXT_EDITING_NAMED_KEYS.has(event.key);
}

/** Whether typing into the focused element edits text there: a writable
 * text-taking input or textarea, or an editable region. A checkbox or radio
 * is an input too, but Space toggles it rather than typing into it. */
export function isTextEditingTarget(element: Element | null): boolean {
  if (element instanceof HTMLInputElement) {
    return (
      !NON_TEXT_INPUT_TYPES.has(element.type) &&
      !element.readOnly &&
      !element.disabled
    );
  }
  if (element instanceof HTMLTextAreaElement) {
    return !element.readOnly && !element.disabled;
  }
  return element instanceof HTMLElement && element.isContentEditable === true;
}

/** Install the behavior on ``doc``: a text-editing keystroke into an editable
 * element hides the pointer, and real pointer movement, a press, or a scroll
 * shows it again. Returns the uninstaller. */
export function installCursorHidingWhileTyping(doc: Document): () => void {
  const html = doc.documentElement;
  const controller = new AbortController();
  const options = { capture: true, signal: controller.signal };
  // Chromium fires mousemove for a stationary pointer when the content under
  // it reflows (a growing textarea, a scroll), which would bring the pointer
  // straight back on the next character, so only a change of screen position
  // counts as movement.
  let lastScreenX: number | null = null;
  let lastScreenY: number | null = null;
  const show = (): void => html.classList.remove(CURSOR_HIDDEN_CLASS);

  doc.addEventListener(
    "keydown",
    (event) => {
      if (
        isTextEditingKeystroke(event) &&
        isTextEditingTarget(doc.activeElement)
      ) {
        html.classList.add(CURSOR_HIDDEN_CLASS);
      }
    },
    options,
  );
  doc.addEventListener(
    "mousemove",
    (event) => {
      if (event.screenX === lastScreenX && event.screenY === lastScreenY)
        return;
      lastScreenX = event.screenX;
      lastScreenY = event.screenY;
      show();
    },
    options,
  );
  doc.addEventListener("mousedown", show, options);
  doc.addEventListener("wheel", show, { ...options, passive: true });
  return () => controller.abort();
}
