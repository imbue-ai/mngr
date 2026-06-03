import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { PermissionRequest } from './PermissionRequest.jsx';

describe('PermissionRequest', () => {
  it('renders the dialog scaffolding with header, rationale, and action buttons', () => {
    const { container, getByText } = render(() => (
      <PermissionRequest
        agentId="agent-1"
        requestId="req-1"
        wsName="Demo workspace"
        rationale="I need to read the channel"
        displayName="Slack"
      />
    ));
    expect(container.querySelector('#permissions-backdrop')).not.toBeNull();
    expect(container.querySelector('#permissions-dialog')).not.toBeNull();
    expect(container.querySelector('#permissions-close-btn')).not.toBeNull();
    expect(getByText('Demo workspace says:')).toBeInTheDocument();
    expect(getByText('I need to read the channel')).toBeInTheDocument();
    expect(getByText('Approve')).toBeInTheDocument();
    expect(getByText('Deny')).toBeInTheDocument();
  });

  it('falls back to the agent id in the rationale label when ws_name is empty', () => {
    const { getByText } = render(() => (
      <PermissionRequest
        agentId="agent-fallback"
        requestId="req-1"
        wsName=""
        rationale="reason"
        displayName="Display"
      />
    ));
    expect(getByText('agent-fallback says:')).toBeInTheDocument();
  });

  it('targets the right grant endpoint via the form action', () => {
    const { container } = render(() => (
      <PermissionRequest
        agentId="agent-1"
        requestId="req-99"
        rationale=""
        displayName="X"
      />
    ));
    const form = container.querySelector('#permissions-form');
    expect(form).not.toBeNull();
    expect(form.getAttribute('action')).toBe('/requests/req-99/grant');
  });
});
