import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { OptionCard } from './OptionCard.jsx';

describe('OptionCard', () => {
  it('renders title and description', () => {
    const { getByText } = render(() => (
      <OptionCard value="full" title="Full access" desc="Read and write." />
    ));
    expect(getByText('Full access')).toBeInTheDocument();
    expect(getByText('Read and write.')).toBeInTheDocument();
  });

  it('applies opt-selected when selected', () => {
    const { container } = render(() => (
      <OptionCard value="x" title="X" desc="d" selected />
    ));
    expect(container.firstElementChild.className).toContain('opt-selected');
  });

  it('invokes onSelect with the value when clicked', () => {
    const onSelect = vi.fn();
    const { container } = render(() => (
      <OptionCard value="agree" title="Agree" desc="ok" onSelect={onSelect} />
    ));
    fireEvent.click(container.firstElementChild);
    expect(onSelect).toHaveBeenCalledWith('agree');
  });

  it('renders the textarea when editable', () => {
    const { container } = render(() => (
      <OptionCard
        value="custom"
        title="Custom"
        desc="d"
        editable
        textValue="initial"
      />
    ));
    const textarea = container.querySelector('textarea.opt-text');
    expect(textarea).toBeInTheDocument();
    expect(textarea.value).toBe('initial');
  });
});
