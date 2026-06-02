import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { Button } from './Button.jsx';

describe('Button', () => {
  it('renders the label with the primary variant by default props', () => {
    const { getByRole } = render(() => <Button variant="primary">Save</Button>);
    const btn = getByRole('button', { name: 'Save' });
    expect(btn).toBeInTheDocument();
    expect(btn.className).toContain('bg-zinc-900');
  });

  it('throws on unknown variant', () => {
    expect(() => render(() => <Button variant="bogus">x</Button>)).toThrow(/Unknown button variant/);
  });

  it('forwards onClick to the underlying button', () => {
    const onClick = vi.fn();
    const { getByRole } = render(() => <Button onClick={onClick}>Go</Button>);
    fireEvent.click(getByRole('button', { name: 'Go' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('respects the block prop by applying w-full', () => {
    const { getByRole } = render(() => <Button block>Wide</Button>);
    expect(getByRole('button').className).toContain('w-full');
  });
});
