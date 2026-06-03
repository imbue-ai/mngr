import { Show, splitProps, mergeProps } from 'solid-js';
import { AccentStripe } from '../ui/AccentStripe.jsx';
import { Badge } from '../ui/Badge.jsx';
import { IconButton } from '../ui/IconButton.jsx';
import { Spinner } from '../ui/Spinner.jsx';

// Single workspace row used by the landing page.
//
// One component absorbs what the Jinja landing template did with three
// near-duplicate branches:
//
//   * ``status="running"``       -- regular row with name + restart/settings controls
//   * ``status="destroying"``    -- read-only row linking to /destroying/<id>, with a "Destroying..." badge
//   * ``status="destroy_failed"`` -- read-only row linking to /destroying/<id>, with a "Destroy failed" badge
//
// Props:
//   * ``agentId``        -- string id; used as the workspace key
//   * ``name``           -- display name (falls back to ``agentId`` if omitted)
//   * ``accent``         -- precomputed OKLCH accent (from the Python SSR shim)
//   * ``status``         -- one of the strings above; defaults to "running"
//   * ``href``           -- target URL for click navigation (e.g. mngr_forward goto url)
//   * ``destroyingHref`` -- URL for the destroying detail page (defaults to ``/destroying/<id>``)
//   * ``onRestart``      -- callback for the restart icon button (running only)
//   * ``onSettings``     -- callback for the settings icon button (running only); defaults to navigating to /workspace/<id>/settings
//   * ``onClick``        -- callback when the body of a running row is clicked

const SHELL_CLASS =
  'flex items-center gap-3 bg-white border border-zinc-200 rounded-xl px-4 py-3.5 ' +
  'transition no-underline text-inherit hover:border-zinc-300 hover:shadow-sm';

export function WorkspaceRow(rawProps) {
  const props = mergeProps({ status: 'running' }, rawProps);
  const [local] = splitProps(props, [
    'agentId',
    'name',
    'accent',
    'status',
    'href',
    'destroyingHref',
    'onClick',
    'onRestart',
    'onSettings',
  ]);

  const displayName = () => local.name || local.agentId;
  const isLink = () => local.status !== 'running';
  const linkHref = () => local.destroyingHref || `/destroying/${local.agentId}`;

  const handleSettings = (event) => {
    event.stopPropagation();
    if (typeof local.onSettings === 'function') {
      local.onSettings(local.agentId);
      return;
    }
    if (typeof window !== 'undefined') {
      window.location.assign(`/workspace/${local.agentId}/settings`);
    }
  };

  const handleRestart = (event) => {
    event.stopPropagation();
    if (typeof local.onRestart === 'function') {
      local.onRestart(local.agentId);
    }
  };

  const handleRowClick = () => {
    if (typeof local.onClick === 'function') {
      local.onClick(local.agentId, local.href);
    } else if (typeof window !== 'undefined' && local.href) {
      window.location.assign(local.href);
    }
  };

  return (
    <AccentStripe
      agentId={local.agentId}
      accent={local.accent}
      component={isLink() ? 'a' : 'div'}
      class={[
        SHELL_CLASS,
        isLink() ? '' : 'cursor-pointer',
      ]
        .filter(Boolean)
        .join(' ')}
      href={isLink() ? linkHref() : undefined}
      data-agent-id={local.agentId}
      data-status={local.status}
      onClick={isLink() ? undefined : handleRowClick}
    >
      <span
        class={[
          'flex-1 font-medium pl-1',
          local.status === 'running' ? 'text-zinc-900' : 'text-zinc-500',
        ].join(' ')}
      >
        {displayName()}
      </span>

      <Show when={local.status === 'destroying'}>
        <Badge variant="neutral">
          <Spinner size="sm" />
          Destroying...
        </Badge>
      </Show>

      <Show when={local.status === 'destroy_failed'}>
        <Badge variant="error">Destroy failed</Badge>
      </Show>

      <Show when={local.status === 'running'}>
        <IconButton
          kind="restart"
          label="Restart workspace"
          onClick={handleRestart}
        />
        <IconButton
          kind="settings"
          label="Workspace settings"
          onClick={handleSettings}
        />
      </Show>
    </AccentStripe>
  );
}
