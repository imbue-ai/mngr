import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { IconButton } from './IconButton.jsx';

describe('IconButton', () => {
  it('renders a built-in settings icon for kind="settings"', () => {
    const { getByRole, container } = render(() => (
      <IconButton kind="settings" label="Settings" />
    ));
    const button = getByRole('button', { name: 'Settings' });
    expect(button).toBeInTheDocument();
    expect(container.querySelector('svg')).not.toBeNull();
  });

  it('renders a built-in restart icon for kind="restart"', () => {
    const { getByRole, container } = render(() => (
      <IconButton kind="restart" label="Restart" />
    ));
    expect(getByRole('button', { name: 'Restart' })).toBeInTheDocument();
    expect(container.querySelector('svg')).not.toBeNull();
  });

  it('falls back to children when no kind is provided', () => {
    const { getByText } = render(() => (
      <IconButton label="Custom"><span>custom</span></IconButton>
    ));
    expect(getByText('custom')).toBeInTheDocument();
  });

  it('throws on an unknown kind', () => {
    expect(() => render(() => <IconButton kind="bogus" />)).toThrow(/Unknown icon kind/);
  });

  it('forwards click handlers', () => {
    const onClick = vi.fn();
    const { getByRole } = render(() => (
      <IconButton kind="settings" label="Settings" onClick={onClick} />
    ));
    fireEvent.click(getByRole('button', { name: 'Settings' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
