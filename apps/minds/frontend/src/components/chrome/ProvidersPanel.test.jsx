import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { ProvidersPanel } from './ProvidersPanel.jsx';

describe('ProvidersPanel', () => {
  it('shows the fallback message when no providers are configured', () => {
    const { getByText } = render(() => <ProvidersPanel providers={[]} />);
    expect(getByText('No providers configured')).toBeInTheDocument();
  });

  it('renders a pill per provider with the enabled / disabled tint', () => {
    const { getByText } = render(() => (
      <ProvidersPanel
        providers={[
          { name: 'imbue-prod', enabled: true },
          { name: 'imbue-dev', enabled: false },
        ]}
      />
    ));
    const enabledPill = getByText('imbue-prod').closest('span');
    const disabledPill = getByText('imbue-dev').closest('span');
    expect(enabledPill.className).toContain('emerald');
    expect(disabledPill.className).toContain('zinc-800');
  });
});
