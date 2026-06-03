import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { Badge } from './Badge.jsx';

describe('Badge', () => {
  it('renders the children with the neutral variant by default', () => {
    const { getByText } = render(() => <Badge>Default</Badge>);
    const el = getByText('Default');
    expect(el.className).toContain('bg-zinc-100');
    expect(el.className).toContain('text-zinc-600');
  });

  it('applies success styling for the success variant', () => {
    const { getByText } = render(() => <Badge variant="success">Done</Badge>);
    expect(getByText('Done').className).toContain('bg-emerald-100');
  });

  it('applies error styling for the error variant', () => {
    const { getByText } = render(() => <Badge variant="error">Failed</Badge>);
    expect(getByText('Failed').className).toContain('bg-red-100');
  });

  it('throws on an unknown variant', () => {
    expect(() => render(() => <Badge variant="bogus">x</Badge>)).toThrow(/Unknown badge variant/);
  });
});
