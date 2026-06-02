import { describe, it, expect } from 'vitest';
import { createSseStore } from './sse_store.jsx';

describe('createSseStore', () => {
  it('initialises with the default slice shape', () => {
    const { state } = createSseStore({});
    expect(state.workspaces).toEqual([]);
    expect(state.auth.status).toBe('unknown');
    expect(state.requests.count).toBe(0);
  });

  it('applies a topic envelope to its slice', () => {
    const { state, applyEnvelope } = createSseStore({});
    applyEnvelope({ topic: 'workspaces', payload: [{ id: 'agent-1' }] });
    expect(state.workspaces).toEqual([{ id: 'agent-1' }]);
  });

  it('replaces multiple slices from a snapshot envelope', () => {
    const { state, applyEnvelope } = createSseStore({});
    applyEnvelope({
      topic: 'snapshot',
      payload: {
        workspaces: [{ id: 'agent-1' }],
        requests: { count: 2, ids: ['r1', 'r2'] },
      },
    });
    expect(state.workspaces).toHaveLength(1);
    expect(state.requests.count).toBe(2);
  });

  it('ignores malformed envelopes', () => {
    const { state, applyEnvelope } = createSseStore({});
    applyEnvelope(null);
    applyEnvelope({});
    applyEnvelope({ topic: 42 });
    expect(state.workspaces).toEqual([]);
  });
});
