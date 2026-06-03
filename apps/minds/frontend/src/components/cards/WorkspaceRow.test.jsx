import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { WorkspaceRow } from './WorkspaceRow.jsx';

describe('WorkspaceRow', () => {
  it('renders a clickable div with restart + settings controls for a running workspace', () => {
    const { getByText, getByRole, container } = render(() => (
      <WorkspaceRow
        agentId="abc"
        name="My workspace"
        accent="oklch(65% 0.15 100)"
        status="running"
        href="https://forward/goto/abc/"
      />
    ));
    expect(getByText('My workspace')).toBeInTheDocument();
    const root = container.firstElementChild;
    expect(root.tagName.toLowerCase()).toBe('div');
    expect(root.getAttribute('data-status')).toBe('running');
    expect(getByRole('button', { name: 'Restart workspace' })).toBeInTheDocument();
    expect(getByRole('button', { name: 'Workspace settings' })).toBeInTheDocument();
  });

  it('renders an anchor with the destroying badge for status="destroying"', () => {
    const { getByText, container, queryByRole } = render(() => (
      <WorkspaceRow
        agentId="abc"
        name="Going away"
        accent="oklch(65% 0.15 100)"
        status="destroying"
      />
    ));
    const root = container.firstElementChild;
    expect(root.tagName.toLowerCase()).toBe('a');
    expect(root.getAttribute('href')).toBe('/destroying/abc');
    expect(getByText('Destroying...')).toBeInTheDocument();
    // Controls are hidden in the destroying branch.
    expect(queryByRole('button', { name: 'Workspace settings' })).toBeNull();
  });

  it('renders the error badge for status="destroy_failed"', () => {
    const { getByText, container } = render(() => (
      <WorkspaceRow
        agentId="abc"
        name="Broken"
        accent="oklch(65% 0.15 100)"
        status="destroy_failed"
      />
    ));
    expect(getByText('Destroy failed')).toBeInTheDocument();
    expect(container.firstElementChild.tagName.toLowerCase()).toBe('a');
  });

  it('falls back to agentId when no name is provided', () => {
    const { getByText } = render(() => (
      <WorkspaceRow
        agentId="bare-id"
        accent="oklch(65% 0.15 100)"
      />
    ));
    expect(getByText('bare-id')).toBeInTheDocument();
  });

  it('invokes onRestart when the restart control is clicked, without firing the row click', () => {
    const onClick = vi.fn();
    const onRestart = vi.fn();
    const { getByRole } = render(() => (
      <WorkspaceRow
        agentId="x"
        accent="oklch(65% 0.15 100)"
        status="running"
        onClick={onClick}
        onRestart={onRestart}
        onSettings={() => {}}
      />
    ));
    fireEvent.click(getByRole('button', { name: 'Restart workspace' }));
    expect(onRestart).toHaveBeenCalledWith('x');
    expect(onClick).not.toHaveBeenCalled();
  });
});
