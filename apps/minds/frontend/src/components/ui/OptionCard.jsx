import { Show, splitProps } from 'solid-js';

// Selectable radio-style option used by the onboarding question flow on
// the creating page. The textarea reveal when an editable option is
// selected is driven by CSS in globals.css (``.opt-editable.opt-selected``).
export function OptionCard(props) {
  const [local] = splitProps(props, [
    'value',
    'title',
    'desc',
    'editable',
    'selected',
    'preset',
    'placeholder',
    'rows',
    'onSelect',
    'onTextInput',
    'textValue',
  ]);
  const handleClick = (event) => {
    if (typeof local.onSelect === 'function') {
      local.onSelect(local.value);
    }
    // Defer focus so any DOM reconciliation has flushed.
    if (local.editable) {
      queueMicrotask(() => {
        const target = event.currentTarget;
        if (!target) return;
        const textarea = target.querySelector('textarea.opt-text');
        if (textarea) {
          textarea.focus();
          const len = textarea.value.length;
          textarea.setSelectionRange(len, len);
        }
      });
    }
  };
  const rows = () => local.rows || 2;
  const classes = () =>
    [
      'opt',
      local.editable ? 'opt-editable' : '',
      local.selected ? 'opt-selected' : '',
    ]
      .filter(Boolean)
      .join(' ');
  return (
    <div class={classes()} data-val={local.value} onClick={handleClick}>
      <span class="opt-radio" />
      <div class="opt-body">
        <span class="block text-sm font-medium text-zinc-900">{local.title}</span>
        <span class="opt-desc block text-[12.5px] text-zinc-500 leading-snug mt-0.5">
          {local.desc}
        </span>
        <Show when={local.editable}>
          <div class="opt-edit mt-2">
            <textarea
              class="opt-text w-full px-3 py-2 text-[13px] rounded-md border border-zinc-200 bg-white text-zinc-900 outline-none transition focus:border-blue-600 focus:ring-2 focus:ring-blue-600/15 leading-snug"
              rows={rows()}
              placeholder={local.placeholder || ''}
              value={local.textValue ?? local.preset ?? ''}
              onInput={(event) => {
                if (typeof local.onTextInput === 'function') {
                  local.onTextInput(event.currentTarget.value);
                }
              }}
              onClick={(event) => event.stopPropagation()}
            />
          </div>
        </Show>
      </div>
    </div>
  );
}
