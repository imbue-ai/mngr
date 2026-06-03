import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { Sidebar } from './Sidebar.jsx';

const WORKSPACES = [
  { id: 'agent-a', name: 'Apples', account: 'team@example.com' },
  { id: 'agent-b', name: 'Bananas' },
  { id: 'agent-c', name: 'Cherries', account: 'team@example.com' },
];

describe('Sidebar', () => {
  it('renders an empty state when there are no workspaces', () => {
    const { getByText } = render(() => <Sidebar workspaces={[]} mngrForwardOrigin="" />);
    expect(getByText('No projects')).toBeInTheDocument();
  });

  it('groups workspaces by account with Private first', () => {
    const { getAllByText, getByText, container } = render(() => (
      <Sidebar workspaces={WORKSPACES} mngrForwardOrigin="http://forward" />
    ));
    // The "Private" header always renders first when present.
    const headers = container.querySelectorAll('div.text-\\[11px\\]');
    expect(headers[0].textContent).toBe('PRIVATE');
    expect(headers[1].textContent).toBe('team@example.com');
    // Each workspace label renders.
    expect(getByText('Apples')).toBeInTheDocument();
    expect(getByText('Bananas')).toBeInTheDocument();
    expect(getByText('Cherries')).toBeInTheDocument();
    // Workspace name renders exactly once per workspace.
    expect(getAllByText('Apples')).toHaveLength(1);
  });

  it('highlights the current workspace row when showOpenInNewWindow is true', () => {
    const { container } = render(() => (
      <Sidebar
        workspaces={WORKSPACES}
        mngrForwardOrigin="http://forward"
        currentWorkspaceId="agent-b"
        showOpenInNewWindow
      />
    ));
    const currentRow = container.querySelector('[data-agent-id="agent-b"]');
    expect(currentRow).not.toBeNull();
    expect(currentRow.classList.contains('is-current')).toBe(true);
    const otherRow = container.querySelector('[data-agent-id="agent-a"]');
    expect(otherRow.classList.contains('is-current')).toBe(false);
  });

  it('renders the open-in-new affordance only when showOpenInNewWindow is true', () => {
    const { container, unmount } = render(() => (
      <Sidebar
        workspaces={[WORKSPACES[1]]}
        mngrForwardOrigin="http://forward"
        showOpenInNewWindow={false}
      />
    ));
    expect(container.querySelector('button[data-open-new]')).toBeNull();
    unmount();

    const { container: container2 } = render(() => (
      <Sidebar
        workspaces={[WORKSPACES[1]]}
        mngrForwardOrigin="http://forward"
        showOpenInNewWindow
      />
    ));
    expect(container2.querySelector('button[data-open-new]')).not.toBeNull();
  });
});
