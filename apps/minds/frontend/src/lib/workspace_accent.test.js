import { describe, it, expect, beforeEach } from 'vitest';
import { workspaceAccent, _clearAccentCacheForTests } from './workspace_accent.js';

describe('workspaceAccent', () => {
  beforeEach(() => {
    _clearAccentCacheForTests();
  });

  it('returns a deterministic oklch string', async () => {
    const a = await workspaceAccent('agent-abc123');
    const b = await workspaceAccent('agent-abc123');
    expect(a).toBe(b);
    expect(a).toMatch(/^oklch\(65% 0\.15 \d{1,3}\)$/);
  });

  it('produces matching output to the Python implementation for known agent ids', async () => {
    // These goldens are computed from the Python implementation in
    // imbue.minds.desktop_client.templates.workspace_accent(); they assert
    // that the JS and Python helpers stay aligned. If you regenerate
    // either side, regenerate both.
    expect(await workspaceAccent('agent-0000000000000000')).toBe('oklch(65% 0.15 68)');
    expect(await workspaceAccent('agent-ffffffffffffffff')).toBe('oklch(65% 0.15 230)');
  });
});
