import { For, Show, splitProps } from 'solid-js';

const SELECT_CLASS =
  'w-48 px-3 py-2 text-sm rounded-md border border-zinc-200 ' +
  'bg-white text-zinc-900 outline-none transition ' +
  'focus:border-blue-600 focus:ring-2 focus:ring-blue-600/15';

// Inline-row select: label sits to the left, the select pulls right
// at a fixed 48-unit width. Compresses the five-times-repeated
// "<div flex><label /><select /></div>" block in templates/create.html
// down to one component call per row.
//
// Props:
//   label    string                 label text rendered to the left
//   name     string                 select name + id fallback
//   id       string                 explicit id (defaults to name)
//   value    string                 currently selected option value
//   onChange fn                     change handler; passed the new value
//   options  Array<{value, label, requiresAccount?}>
//             option list. `requiresAccount` mirrors the
//             `data-requires-account` attribute used by the Jinja
//             template to disable the IMBUE_CLOUD option when no
//             account is selected.
//   disabledValues Set<string>      values to mark as disabled
//   error    string                 optional amber error line
//             rendered right-aligned under the row, mirroring the
//             Jinja `*-account-error` paragraphs.
export function FormSelect(props) {
  const [local] = splitProps(props, [
    'label',
    'name',
    'id',
    'value',
    'onChange',
    'options',
    'disabledValues',
    'error',
  ]);
  const fieldId = () => local.id || local.name;
  const handleChange = (event) => {
    if (typeof local.onChange === 'function') {
      local.onChange(event.currentTarget.value);
    }
  };
  const isOptionDisabled = (option) =>
    Boolean(local.disabledValues && local.disabledValues.has(option.value));
  return (
    <div>
      <div class="flex items-center justify-between gap-3">
        <label for={fieldId()} class="text-sm text-zinc-900 font-medium">
          {local.label}
        </label>
        <select
          id={fieldId()}
          name={local.name}
          value={local.value}
          onChange={handleChange}
          class={SELECT_CLASS}
        >
          <For each={local.options}>
            {(option) => (
              <option value={option.value} disabled={isOptionDisabled(option)}>
                {option.label}
              </option>
            )}
          </For>
        </select>
      </div>
      <Show when={local.error}>
        <p class="mt-1 text-xs text-amber-600 text-right">{local.error}</p>
      </Show>
    </div>
  );
}
