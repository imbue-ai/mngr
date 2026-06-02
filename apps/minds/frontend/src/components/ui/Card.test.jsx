import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { Card, CardRow } from './Card.jsx';

describe('Card', () => {
  it('renders children inside the card body', () => {
    const { getByText } = render(() => (
      <Card>
        <span>hello</span>
      </Card>
    ));
    expect(getByText('hello')).toBeInTheDocument();
  });
});

describe('CardRow', () => {
  it('applies the flex layout classes', () => {
    const { container } = render(() => (
      <CardRow>
        <span>left</span>
        <span>right</span>
      </CardRow>
    ));
    const root = container.firstElementChild;
    expect(root.className).toContain('flex');
    expect(root.className).toContain('justify-between');
  });
});
