import { Show, splitProps } from 'solid-js';
import { TextInput } from '../ui/TextInput.jsx';

// Label-above-input form field with optional helper text and inline
// error. Compresses the "label, helper paragraph, input, error
// paragraph" four-element block the Jinja create form spells out by
// hand five separate times.
//
// Props:
//   label       string  text rendered above the input
//   name        string  the `name` attribute (used as id fallback)
//   id          string  optional explicit id, defaults to `name`
//   type        string  defaults to 'text' (also accepts 'password')
//   value       string  current value (controlled)
//   onInput     fn      Solid input handler
//   placeholder string  placeholder text
//   helper      string  optional muted helper line under the label
//   error       string  optional amber error line under the input
//   required    bool    HTML required attribute
//   extra       string  extra classes appended to the input
export function FormField(props) {
  const [local, rest] = splitProps(props, [
    'label',
    'name',
    'id',
    'type',
    'value',
    'onInput',
    'placeholder',
    'helper',
    'error',
    'required',
    'extra',
  ]);
  const fieldId = () => local.id || local.name;
  return (
    <div>
      <Show when={local.label}>
        <label for={fieldId()} class="text-sm text-zinc-900 font-medium block mb-1">
          {local.label}
        </label>
      </Show>
      <Show when={local.helper}>
        <p class="mb-1 text-xs text-zinc-400">{local.helper}</p>
      </Show>
      <TextInput
        id={fieldId()}
        name={local.name}
        type={local.type || 'text'}
        value={local.value ?? ''}
        onInput={local.onInput}
        placeholder={local.placeholder}
        required={local.required}
        extra={local.extra}
        {...rest}
      />
      <Show when={local.error}>
        <p class="mt-1 text-xs text-amber-600">{local.error}</p>
      </Show>
    </div>
  );
}
