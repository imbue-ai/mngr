import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { ButtonLink } from './ButtonLink.jsx';

describe('ButtonLink', () => {
  it('renders an anchor with the href and label', () => {
    const { getByRole } = render(() => (
      <ButtonLink href="/create" variant="primary">
        Create
      </ButtonLink>
    ));
    const link = getByRole('link', { name: 'Create' });
    expect(link).toHaveAttribute('href', '/create');
    expect(link.className).toContain('bg-zinc-900');
  });
});
