import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render } from '@solidjs/testing-library';
import { DestroyingRoute } from './destroying.jsx';

function mockFetch(responses) {
  // Returns a fetch double that walks through a queue of {match, ok, body}
  // entries. Each ``match`` is a substring of the requested URL.
  const queue = [...responses];
  const calls = [];
  const fn = vi.fn(async (url) => {
    calls.push(String(url));
    const idx = queue.findIndex((entry) => String(url).includes(entry.match));
    if (idx === -1) {
      return { ok: false, status: 404, async json() { return null; } };
    }
    const [entry] = queue.splice(idx, 1);
    return {
      ok: entry.ok !== false,
      status: entry.status || 200,
      async json() { return entry.body; },
    };
  });
  fn.calls = calls;
  return fn;
}

describe('DestroyingRoute', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.useRealTimers();
  });

  it('renders the running badge for an in-flight destroy', () => {
    globalThis.fetch = mockFetch([]);
    const { getByText } = render(() => (
      <DestroyingRoute
        agent_id="abc"
        agent_name="My agent"
        pid={1234}
        status="running"
        accent="oklch(65% 0.15 100)"
      />
    ));
    expect(getByText('Destroying My agent')).toBeInTheDocument();
    expect(getByText('Running...')).toBeInTheDocument();
    expect(getByText(/pid 1234/)).toBeInTheDocument();
  });

  it('renders the Failed badge and Retry / Dismiss actions for the failed status', () => {
    globalThis.fetch = mockFetch([]);
    const { getByText, getByRole } = render(() => (
      <DestroyingRoute
        agent_id="abc"
        agent_name="Bad agent"
        pid={42}
        status="failed"
        accent="oklch(65% 0.15 100)"
      />
    ));
    expect(getByText('Failed')).toBeInTheDocument();
    expect(getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
  });

  it('falls back to the agent id when no name is provided', () => {
    globalThis.fetch = mockFetch([]);
    const { getByText } = render(() => (
      <DestroyingRoute
        agent_id="abc-bare"
        pid={1}
        status="running"
        accent="oklch(65% 0.15 100)"
      />
    ));
    expect(getByText('Destroying abc-bare')).toBeInTheDocument();
  });
});
