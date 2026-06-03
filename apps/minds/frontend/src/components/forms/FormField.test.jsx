import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { FormField } from './FormField.jsx';

describe('FormField', () => {
  it('renders a label tied to its input by id', () => {
    const { getByLabelText } = render(() => (
      <FormField label="Workspace name" name="host_name" value="" />
    ));
    const input = getByLabelText('Workspace name');
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('name', 'host_name');
    expect(input).toHaveAttribute('id', 'host_name');
  });

  it('shows the helper paragraph above the input when provided', () => {
    const { getByText } = render(() => (
      <FormField label="Repository" name="git_url" helper="Git URL or local path" />
    ));
    expect(getByText('Git URL or local path')).toBeInTheDocument();
  });

  it('renders the error message in amber styling', () => {
    const { getByText } = render(() => (
      <FormField label="Name" name="host_name" error="not a valid hostname" />
    ));
    const err = getByText('not a valid hostname');
    expect(err.className).toContain('text-amber-600');
  });

  it('forwards onInput to the underlying input', () => {
    const onInput = vi.fn();
    const { getByLabelText } = render(() => (
      <FormField label="Name" name="host_name" value="" onInput={onInput} />
    ));
    fireEvent.input(getByLabelText('Name'), { target: { value: 'next' } });
    expect(onInput).toHaveBeenCalled();
  });

  it('supports the password type for sensitive fields', () => {
    const { getByLabelText } = render(() => (
      <FormField label="API key" name="anthropic_api_key" type="password" />
    ));
    expect(getByLabelText('API key')).toHaveAttribute('type', 'password');
  });
});
