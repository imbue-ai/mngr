import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render } from '@solidjs/testing-library';
import { RequestsPanel } from './RequestsPanel.jsx';

describe('RequestsPanel', () => {
  it('renders an empty state when there are no pending requests', () => {
    const { getByText } = render(() => <RequestsPanel requests={[]} />);
    expect(getByText('No pending requests')).toBeInTheDocument();
  });

  it('renders each pending request as a clickable row', () => {
    const handler = vi.fn();
    const { getByText } = render(() => (
      <RequestsPanel
        requests={[
          { id: 'req-1', label: 'Approve Slack' },
          { id: 'req-2', label: 'Approve GitHub', agent_id: 'agent-x' },
        ]}
        onSelect={handler}
      />
    ));
    fireEvent.click(getByText('Approve Slack'));
    fireEvent.click(getByText('Approve GitHub'));
    expect(handler).toHaveBeenCalledTimes(2);
    expect(handler).toHaveBeenNthCalledWith(1, 'req-1');
    expect(handler).toHaveBeenNthCalledWith(2, 'req-2');
    expect(getByText('agent-x')).toBeInTheDocument();
  });

  it('uses a custom empty-state message when provided', () => {
    const { getByText } = render(() => (
      <RequestsPanel requests={[]} emptyMessage="Nothing waiting" />
    ));
    expect(getByText('Nothing waiting')).toBeInTheDocument();
  });
});
