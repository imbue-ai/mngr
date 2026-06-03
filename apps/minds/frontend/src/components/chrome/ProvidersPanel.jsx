import { For, Show } from 'solid-js';

// Bottom-of-chrome status indicator listing provider blocks (e.g.
// imbue_cloud accounts) and whether they are enabled.
//
// Props:
//   providers     -- array of { name, enabled, status? } shapes; rendered
//                    as a horizontal pill row.
export function ProvidersPanel(props) {
  const providers = () => props.providers || [];
  return (
    <div class="providers-panel flex items-center gap-2 px-3 py-1.5 border-t border-white/10 bg-zinc-900 text-xs">
      <Show
        when={providers().length > 0}
        fallback={<span class="text-zinc-500">No providers configured</span>}
      >
        <For each={providers()}>
          {(provider) => (
            <span
              class={
                'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border ' +
                (provider.enabled
                  ? 'bg-emerald-900/30 text-emerald-300 border-emerald-700/60'
                  : 'bg-zinc-800 text-zinc-400 border-zinc-700')
              }
              title={provider.status || ''}
            >
              <span
                class={
                  'w-1.5 h-1.5 rounded-full ' +
                  (provider.enabled ? 'bg-emerald-400' : 'bg-zinc-500')
                }
                aria-hidden="true"
              />
              {provider.name}
            </span>
          )}
        </For>
      </Show>
    </div>
  );
}
