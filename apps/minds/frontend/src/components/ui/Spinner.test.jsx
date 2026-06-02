import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { Spinner } from './Spinner.jsx';

describe('Spinner', () => {
  it('applies the medium size class by default', () => {
    const { container } = render(() => <Spinner />);
    expect(container.firstElementChild.className).toContain('w-[18px]');
  });

  it('throws on unknown size', () => {
    expect(() => render(() => <Spinner size="xl" />)).toThrow(/Unknown spinner size/);
  });
});
