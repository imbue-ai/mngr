import { Show, splitProps, onMount, onCleanup, createEffect } from 'solid-js';

// Generic modal dialog. A reusable variant of the skip-account dialog
// from welcome.jsx: a backdrop that dims the page, a centered card with
// optional title, click-on-backdrop-to-close, and Escape-to-close.
//
// Props:
//   * ``open`` (boolean) -- whether the dialog is visible
//   * ``onClose`` (function) -- invoked when the user dismisses
//   * ``title`` (string, optional) -- heading rendered above ``children``
//   * ``children`` -- dialog body (caller-owned: text, form, action row, etc.)
//
// Keep the markup minimal. We intentionally do not own the action buttons
// or footer layout; the caller composes those from <Button>/<ButtonLink>.

export function Dialog(props) {
  const [local] = splitProps(props, ['open', 'onClose', 'title', 'children', 'labelledBy']);

  function handleBackdropClick(event) {
    if (event.target === event.currentTarget && typeof local.onClose === 'function') {
      local.onClose();
    }
  }

  function handleKey(event) {
    if (event.key === 'Escape' && local.open && typeof local.onClose === 'function') {
      local.onClose();
    }
  }

  onMount(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', handleKey);
      onCleanup(() => window.removeEventListener('keydown', handleKey));
    }
  });

  // Lock body scroll while the dialog is open.
  createEffect(() => {
    if (typeof document === 'undefined') return;
    const previous = document.body.style.overflow;
    if (local.open) document.body.style.overflow = 'hidden';
    onCleanup(() => {
      document.body.style.overflow = previous;
    });
  });

  return (
    <Show when={local.open}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={local.labelledBy}
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
        onClick={handleBackdropClick}
      >
        <div class="bg-white rounded-xl shadow-lg border border-zinc-200 max-w-sm w-full mx-4 p-6 text-left">
          <Show when={local.title}>
            <h2 id={local.labelledBy} class="text-lg font-semibold text-zinc-900 mb-3">
              {local.title}
            </h2>
          </Show>
          {local.children}
        </div>
      </div>
    </Show>
  );
}
