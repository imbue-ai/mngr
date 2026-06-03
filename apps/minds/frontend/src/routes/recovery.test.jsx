import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render } from '@solidjs/testing-library';
import { RecoveryRoute } from './recovery.jsx';

describe('RecoveryRoute', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () => ({
      ok: false,
      status: 500,
      type: 'basic',
      async json() { return null; },
    }));
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('renders the loading spinner while the probe is in flight', () => {
    // Fetch never resolves so the route stays in the loading view.
    globalThis.fetch = vi.fn(() => new Promise(() => {}));
    const { getByText, container } = render(() => (
      <RecoveryRoute
        agent_id="abc"
        return_to=""
        initial_status="stuck"
        initial_error=""
        ssh_command={null}
      />
    ));
    expect(getByText('Loading workspace')).toBeInTheDocument();
    expect(container.querySelector('.spinner')).not.toBeNull();
  });

  it('renders the unresponsive card with manual restart for healthy + no return_to', () => {
    globalThis.fetch = vi.fn(() => new Promise(() => {}));
    const { getByText, getByRole } = render(() => (
      <RecoveryRoute
        agent_id="abc"
        return_to=""
        initial_status="healthy"
        initial_error=""
        ssh_command={null}
      />
    ));
    expect(getByText('Workspace unresponsive')).toBeInTheDocument();
    expect(getByRole('button', { name: 'Restart workspace' })).toBeInTheDocument();
  });

  it('renders the troubleshooting block when restart_failed has an error', () => {
    // Suppress the background scheduleHealthyPoll fetch.
    globalThis.fetch = vi.fn(() => new Promise(() => {}));
    const { getByText } = render(() => (
      <RecoveryRoute
        agent_id="abc"
        return_to=""
        initial_status="restart_failed"
        initial_error="something exploded"
        ssh_command={null}
      />
    ));
    expect(getByText('Troubleshooting')).toBeInTheDocument();
    expect(getByText('Error details')).toBeInTheDocument();
  });
});
