import { createSignal, createEffect, onCleanup, onMount, For, Show } from 'solid-js';
import { PageContainer } from '../components/ui/PageContainer.jsx';
import { ButtonLink } from '../components/ui/ButtonLink.jsx';
import { Badge } from '../components/ui/Badge.jsx';
import { WorkspaceRow } from '../components/cards/WorkspaceRow.jsx';
import { WorkspaceCardEmpty } from '../components/cards/WorkspaceCardEmpty.jsx';

// Landing page: list of accessible workspaces + a providers panel.
//
// Props (from the Python SSR shim):
//   * agent_ids: string[]
//   * agent_accents: { [agentId]: oklch string }
//   * mngr_forward_origin: string -- prefix for workspace links
//   * agent_names: { [agentId]: string }
//   * destroying_status_by_agent_id: { [agentId]: "running" | "failed" }
//   * is_discovering: bool -- show "discovering" empty state
//
// Live updates wire through /_chrome/events (workspace list churn,
// per-row health, providers state) and /api/backup-status (one-shot
// fetch). Behavior mirrors templates/landing.html's inline script.

function relativeAgo(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function badgeVariantForStatus(status) {
  if (status === 'ok') return 'success';
  if (status === 'error') return 'error';
  return 'neutral';
}

function badgeLabelForStatus(status) {
  if (status === 'ok') return 'OK';
  if (status === 'error') return 'Error';
  if (status === 'disabled') return 'Disabled';
  return status;
}

function ProvidersPanel(props) {
  const [open, setOpen] = createSignal(false);
  const entries = () => props.entries || [];
  const enabledCount = () => entries().filter((e) => e.status !== 'disabled').length;
  const errorCount = () => entries().filter((e) => e.status === 'error').length;
  const providerWord = () => (enabledCount() === 1 ? 'provider' : 'providers');
  const errorWord = () => (errorCount() === 1 ? 'error' : 'errors');
  const summary = () => {
    let text = `${enabledCount()} ${providerWord()} enabled`;
    if (errorCount() > 0) text += ` (${errorCount()} ${errorWord()})`;
    return text;
  };
  return (
    <Show when={entries().length > 0}>
      <section class="mt-10 pt-6 border-t border-zinc-200" data-providers-panel>
        <button
          type="button"
          class="w-full flex items-center justify-between text-left text-sm text-zinc-600 hover:text-zinc-900 bg-transparent border-0 cursor-pointer p-0"
          onClick={() => setOpen((v) => !v)}
        >
          <span>{summary()}</span>
          <span class="text-zinc-400 ml-2">{open() ? '▾' : '▸'}</span>
        </button>
        <Show when={open()}>
          <div class="mt-3">
            <div class="flex items-center justify-end mb-3">
              <div class="text-xs text-zinc-500 flex gap-4">
                <span>last event {relativeAgo(props.lastEventAt)}</span>
                <span>last snapshot {relativeAgo(props.lastSnapshotAt)}</span>
              </div>
            </div>
            <div class="flex flex-col gap-1.5">
              <For each={entries()}>
                {(entry) => (
                  <div class="flex items-center gap-3 bg-white border border-zinc-200 rounded-xl px-4 py-2.5">
                    <span class="font-medium text-zinc-900">{entry.name}</span>
                    <Show when={entry.backend}>
                      <span class="text-xs text-zinc-500">{entry.backend}</span>
                    </Show>
                    <Badge variant={badgeVariantForStatus(entry.status)} extra="text-xs">
                      {badgeLabelForStatus(entry.status)}
                    </Badge>
                    <Show
                      when={entry.status === 'error' && entry.error_message}
                      fallback={<span class="flex-1" />}
                    >
                      <span
                        class="flex-1 text-xs text-zinc-600 truncate"
                        title={`${entry.error_type || ''}: ${entry.error_message}`}
                      >
                        {entry.error_type || ''}: {entry.error_message}
                      </span>
                    </Show>
                    <button
                      type="button"
                      class="px-2 py-1 text-xs rounded border border-zinc-300 bg-white hover:bg-zinc-50 text-zinc-700"
                      onClick={() => props.onToggle(entry.name, entry.status === 'disabled')}
                    >
                      {entry.status === 'disabled' ? 'Enable' : 'Disable'}
                    </button>
                  </div>
                )}
              </For>
            </div>
          </div>
        </Show>
      </section>
    </Show>
  );
}

export function LandingRoute(props) {
  const agentIds = () => props.agent_ids || [];
  const accents = () => props.agent_accents || {};
  const agentNames = () => props.agent_names || {};
  const destroyingStatus = () => props.destroying_status_by_agent_id || {};
  const forwardOrigin = () => props.mngr_forward_origin || '';
  const isDiscovering = () => Boolean(props.is_discovering);

  const [providersState, setProvidersState] = createSignal({
    entries: [],
    lastEventAt: null,
    lastSnapshotAt: null,
  });

  function handleProviderToggle(name, isEnabled) {
    if (typeof fetch === 'undefined') return;
    fetch(`/api/providers/${encodeURIComponent(name)}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_enabled: isEnabled }),
    }).catch(() => {});
  }

  function handleRowClick(agentId, href) {
    if (typeof window === 'undefined' || !href) return;
    window.location.assign(href);
  }

  function handleRestartClick(agentId) {
    if (typeof window === 'undefined') return;
    const defaultHref = `${forwardOrigin()}/goto/${agentId}/`;
    const target =
      `/agents/${encodeURIComponent(agentId)}/recovery` +
      `?return_to=${encodeURIComponent(defaultHref)}&intent=restart`;
    window.location.assign(target);
  }

  let eventSource = null;
  onMount(() => {
    if (typeof EventSource === 'undefined') return;
    try {
      eventSource = new EventSource('/_chrome/events');
      eventSource.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data?.type === 'providers_state') {
          setProvidersState({
            entries: data.providers || [],
            lastEventAt: data.last_event_at || null,
            lastSnapshotAt: data.last_full_snapshot_at || null,
          });
          return;
        }
        if (data?.type === 'workspaces' && Array.isArray(data.workspaces)) {
          const renderedSet = new Set(agentIds().map(String));
          const liveSet = new Set(data.workspaces.map((w) => String(w.id)));
          const differs =
            renderedSet.size !== liveSet.size ||
            [...renderedSet].some((id) => !liveSet.has(id)) ||
            [...liveSet].some((id) => !renderedSet.has(id));
          if (differs && typeof window !== 'undefined') {
            eventSource?.close();
            window.location.reload();
          }
        }
      };
    } catch {
      // EventSource construction failure: log silently; the page is still
      // useful without live updates.
    }
  });
  onCleanup(() => {
    if (eventSource) eventSource.close();
  });

  // is_discovering re-render: a watchdog reload so the page eventually
  // shows discovered agents (matches the old <script>setTimeout reload).
  createEffect(() => {
    if (!isDiscovering()) return;
    if (typeof window === 'undefined') return;
    const handle = window.setTimeout(() => window.location.reload(), 2000);
    onCleanup(() => window.clearTimeout(handle));
  });

  const hasAgents = () => agentIds().length > 0;

  return (
    <PageContainer>
      <Show
        when={hasAgents()}
        fallback={
          <WorkspaceCardEmpty variant={isDiscovering() ? 'discovering' : 'empty'} />
        }
      >
        <div class="flex items-center justify-between mb-5">
          <h1 class="text-xl font-semibold text-zinc-900">Projects</h1>
          <ButtonLink href="/create" variant="primary">
            Create
          </ButtonLink>
        </div>
        <div class="flex flex-col gap-1.5">
          <For each={agentIds()}>
            {(agentId) => {
              const status = destroyingStatus()[String(agentId)];
              const rowStatus = !status
                ? 'running'
                : status === 'running'
                  ? 'destroying'
                  : 'destroy_failed';
              return (
                <WorkspaceRow
                  agentId={agentId}
                  name={agentNames()[String(agentId)] || agentId}
                  accent={accents()[String(agentId)]}
                  status={rowStatus}
                  href={`${forwardOrigin()}/goto/${agentId}/`}
                  onClick={handleRowClick}
                  onRestart={handleRestartClick}
                />
              );
            }}
          </For>
        </div>
      </Show>

      <ProvidersPanel
        entries={providersState().entries}
        lastEventAt={providersState().lastEventAt}
        lastSnapshotAt={providersState().lastSnapshotAt}
        onToggle={handleProviderToggle}
      />
    </PageContainer>
  );
}

