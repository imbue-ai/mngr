import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { FormSelect } from './FormSelect.jsx';

const OPTIONS = [
  { value: 'DOCKER', label: 'docker' },
  { value: 'IMBUE_CLOUD', label: 'imbue_cloud' },
  { value: 'LIMA', label: 'lima' },
];

describe('FormSelect', () => {
  it('renders all options with the current value selected', () => {
    const { getByLabelText } = render(() => (
      <FormSelect label="Compute provider" name="launch_mode" value="LIMA" options={OPTIONS} />
    ));
    const select = getByLabelText('Compute provider');
    expect(select).toBeInTheDocument();
    expect(select.value).toBe('LIMA');
    expect(select.querySelectorAll('option')).toHaveLength(3);
  });

  it('invokes onChange with the new value when the selection changes', () => {
    const onChange = vi.fn();
    const { getByLabelText } = render(() => (
      <FormSelect
        label="Compute provider"
        name="launch_mode"
        value="LIMA"
        options={OPTIONS}
        onChange={onChange}
      />
    ));
    fireEvent.change(getByLabelText('Compute provider'), { target: { value: 'DOCKER' } });
    expect(onChange).toHaveBeenCalledWith('DOCKER');
  });

  it('marks options listed in disabledValues as disabled', () => {
    const { getByLabelText } = render(() => (
      <FormSelect
        label="Compute provider"
        name="launch_mode"
        value="LIMA"
        options={OPTIONS}
        disabledValues={new Set(['IMBUE_CLOUD'])}
      />
    ));
    const select = getByLabelText('Compute provider');
    const imbueOption = select.querySelector('option[value="IMBUE_CLOUD"]');
    expect(imbueOption.disabled).toBe(true);
  });

  it('renders the error message in amber styling when error prop is set', () => {
    const { getByText } = render(() => (
      <FormSelect
        label="Compute provider"
        name="launch_mode"
        value="IMBUE_CLOUD"
        options={OPTIONS}
        error="imbue_cloud requires a selected account."
      />
    ));
    const err = getByText('imbue_cloud requires a selected account.');
    expect(err.className).toContain('text-amber-600');
  });
});
