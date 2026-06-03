import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render } from '@solidjs/testing-library';
import { LandingRoute } from './landing.jsx';

describe('LandingRoute', () => {
  let originalEventSource;
  beforeEach(() => {
    originalEventSource = globalThis.EventSource;
    // Stub the EventSource constructor with a no-op so onMount does not
    // crash under jsdom and the route doesn't try to actually connect.
    class StubEventSource {
      constructor() {}
      close() {}
    }
    globalThis.EventSource = StubEventSource;
  });
  afterEach(() => {
    globalThis.EventSource = originalEventSource;
  });

  it('shows the empty state when no workspaces are accessible', () => {
    const { getByText, getByRole } = render(() => (
      <LandingRoute
        agent_ids={[]}
        agent_accents={{}}
        mngr_forward_origin=""
        agent_names={{}}
        destroying_status_by_agent_id={{}}
        is_discovering={false}
      />
    ));
    expect(getByText('No projects yet')).toBeInTheDocument();
    expect(getByRole('link', { name: 'Create' })).toBeInTheDocument();
  });

  it('shows the discovering state when is_discovering is true', () => {
    const { getByText, queryByText } = render(() => (
      <LandingRoute
        agent_ids={[]}
        agent_accents={{}}
        mngr_forward_origin=""
        agent_names={{}}
        destroying_status_by_agent_id={{}}
        is_discovering={true}
      />
    ));
    expect(getByText('Discovering agents...')).toBeInTheDocument();
    expect(queryByText('No projects yet')).toBeNull();
  });

  it('renders one workspace row per agent_id, with names + accents', () => {
    const { getByText, getAllByRole } = render(() => (
      <LandingRoute
        agent_ids={['a-1', 'a-2']}
        agent_accents={{ 'a-1': 'oklch(65% 0.15 100)', 'a-2': 'oklch(65% 0.15 200)' }}
        mngr_forward_origin="http://forward"
        agent_names={{ 'a-1': 'First', 'a-2': 'Second' }}
        destroying_status_by_agent_id={{}}
        is_discovering={false}
      />
    ));
    expect(getByText('Projects')).toBeInTheDocument();
    expect(getByText('First')).toBeInTheDocument();
    expect(getByText('Second')).toBeInTheDocument();
    // Two restart + two settings buttons (one per running row).
    const restartButtons = getAllByRole('button', { name: 'Restart workspace' });
    expect(restartButtons).toHaveLength(2);
  });

  it('renders the destroying badge for in-flight destroys and the failed badge for failed ones', () => {
    const { getByText } = render(() => (
      <LandingRoute
        agent_ids={['a-1', 'a-2']}
        agent_accents={{ 'a-1': 'oklch(65% 0.15 100)', 'a-2': 'oklch(65% 0.15 200)' }}
        mngr_forward_origin=""
        agent_names={{ 'a-1': 'GoingAway', 'a-2': 'BrokenDestroy' }}
        destroying_status_by_agent_id={{ 'a-1': 'running', 'a-2': 'failed' }}
        is_discovering={false}
      />
    ));
    expect(getByText('Destroying...')).toBeInTheDocument();
    expect(getByText('Destroy failed')).toBeInTheDocument();
  });
});
