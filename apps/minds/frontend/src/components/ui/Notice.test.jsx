import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { Notice } from './Notice.jsx';

describe('Notice', () => {
  it('renders the error variant with red styling', () => {
    const { getByText } = render(() => <Notice variant="error">Bad input</Notice>);
    const el = getByText('Bad input');
    expect(el.className).toContain('bg-red-50');
  });

  it('throws on unknown variants', () => {
    expect(() => render(() => <Notice variant="bogus">x</Notice>)).toThrow(/Unknown notice variant/);
  });
});
