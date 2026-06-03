import { createResource, splitProps, mergeProps } from 'solid-js';
import { Dynamic } from 'solid-js/web';
import { workspaceAccent, workspaceAccentCached } from '../../lib/workspace_accent.js';

// Per-workspace OKLCH accent stripe. Renders any element (default ``<div>``,
// override with ``component``) with the legacy ``.accent-spine`` class and
// the inline ``--workspace-accent`` CSS variable set from the agent id, so
// the 3px stripe rule in tokens.css picks it up.
//
// Mirrors today's inline ``style="--workspace-accent: {{ accent }};"`` +
// ``class="accent-spine ..."`` pattern in templates/landing.html and
// sharing.html. Keeping the lookup behind one component means no caller has
// to remember to set both the class and the style every time.
//
// Default behavior is to compute the color asynchronously via
// ``crypto.subtle`` and fall back to the neutral default until the cache
// warms. SSR renders end up with the neutral default; once the client
// hydrates and the resource resolves, the color updates in place to the
// per-agent hue. Callers that already have a precomputed accent (e.g. the
// SSR shim passes ``workspace_accent(agent_id)`` in props) can supply it
// directly via the ``accent`` prop and skip the async lookup entirely.

export function AccentStripe(props) {
  const merged = mergeProps({ component: 'div' }, props);
  const [local, rest] = splitProps(merged, [
    'agentId',
    'accent',
    'component',
    'children',
    'class',
    'extra',
    'style',
  ]);

  const [resolved] = createResource(
    () => (local.accent ? null : local.agentId || null),
    async (agentId) => (agentId ? workspaceAccent(agentId) : null),
  );

  const accentValue = () => {
    if (local.accent) return local.accent;
    // Touch the resource so re-evaluation pulls the now-warm cache.
    resolved();
    if (!local.agentId) return 'oklch(65% 0.15 230)';
    return workspaceAccentCached(local.agentId);
  };

  const mergedStyle = () => ({
    '--workspace-accent': accentValue(),
    ...(local.style || {}),
  });

  const cls = () =>
    ['accent-spine relative overflow-hidden', local.class, local.extra]
      .filter(Boolean)
      .join(' ');

  return (
    <Dynamic component={local.component} class={cls()} style={mergedStyle()} {...rest}>
      {local.children}
    </Dynamic>
  );
}
