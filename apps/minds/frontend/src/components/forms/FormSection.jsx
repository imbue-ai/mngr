import { Show, createSignal, splitProps } from 'solid-js';

// Collapsible panel with a toggle button -- mirrors the
// "Configure..." and "Show advanced settings" disclosures in
// templates/create.html.
//
// Props:
//   summary     string   optional summary text shown next to the toggle
//   showLabel   string   button label when collapsed (e.g. 'Configure...')
//   hideLabel   string   button label when expanded (e.g. 'Hide')
//   initiallyOpen bool   start expanded
//   children    JSX      the collapsible body
//
// The toggle button and the summary line share a row above the
// body, matching the Jinja layout (`config-summary` + the
// `configure-toggle` button). When the user only ever opens the
// section once the API stays out of their way (no controlled-open
// prop required).
export function FormSection(props) {
  const [local] = splitProps(props, [
    'summary',
    'showLabel',
    'hideLabel',
    'initiallyOpen',
    'children',
  ]);
  const [isOpen, setIsOpen] = createSignal(Boolean(local.initiallyOpen));
  const buttonLabel = () => (isOpen() ? local.hideLabel || 'Hide' : local.showLabel || 'Show');
  return (
    <>
      <div class="flex items-center justify-between gap-3 mt-2.5 px-1 text-xs">
        <span class="text-zinc-400 leading-snug">{local.summary || ''}</span>
        <button
          type="button"
          class="text-blue-600 hover:text-blue-700 cursor-pointer transition-colors whitespace-nowrap"
          onClick={() => setIsOpen((open) => !open)}
        >
          {buttonLabel()}
        </button>
      </div>
      <Show when={isOpen()}>
        <div class="mt-5 flex flex-col gap-4">{local.children}</div>
      </Show>
    </>
  );
}
