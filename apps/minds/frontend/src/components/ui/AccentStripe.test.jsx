import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { AccentStripe } from './AccentStripe.jsx';

describe('AccentStripe', () => {
  it('renders a div with accent-spine and the provided accent CSS variable', () => {
    const { container } = render(() => (
      <AccentStripe accent="oklch(65% 0.15 100)">
        <span>row</span>
      </AccentStripe>
    ));
    const root = container.firstElementChild;
    expect(root.tagName.toLowerCase()).toBe('div');
    expect(root.className).toContain('accent-spine');
    expect(root.style.getPropertyValue('--workspace-accent')).toBe('oklch(65% 0.15 100)');
  });

  it('renders the underlying tag via the component prop (e.g. anchor)', () => {
    const { container } = render(() => (
      <AccentStripe component="a" href="/foo" accent="oklch(65% 0.15 200)">
        link
      </AccentStripe>
    ));
    const root = container.firstElementChild;
    expect(root.tagName.toLowerCase()).toBe('a');
    expect(root.getAttribute('href')).toBe('/foo');
  });

  it('falls back to the neutral accent when no agent or accent is given', () => {
    const { container } = render(() => <AccentStripe>row</AccentStripe>);
    const root = container.firstElementChild;
    expect(root.style.getPropertyValue('--workspace-accent')).toContain('oklch(65% 0.15 230)');
  });

  it('forwards extra class names', () => {
    const { container } = render(() => (
      <AccentStripe accent="oklch(65% 0.15 50)" class="extra-class">
        x
      </AccentStripe>
    ));
    expect(container.firstElementChild.className).toContain('extra-class');
  });
});
