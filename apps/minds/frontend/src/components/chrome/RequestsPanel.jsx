import { For, Show } from 'solid-js';

// Compact list of pending permission requests rendered inside the
// chrome shell's optional requests panel. The chrome shell today opens
// a separate WebContentsView (``/_chrome/requests-panel``) for this in
// Electron, so the component is invoked from within that view -- it
// just needs to render the list given an array.
//
// Props:
//   requests        -- array of { id, label, agent_id? } shapes.
//   onSelect        -- called with the request id when a row is clicked.
//   emptyMessage    -- string shown when the list is empty.
export function RequestsPanel(props) {
  const items = () => props.requests || [];
  const empty = () => props.emptyMessage || 'No pending requests';
  return (
    <div class="requests-panel divide-y divide-white/5">
      <Show
        when={items().length > 0}
        fallback={
          <div class="px-4 py-6 text-sm text-zinc-400 text-center">{empty()}</div>
        }
      >
        <For each={items()}>
          {(request) => (
            <button
              type="button"
              class="block w-full text-left px-4 py-2.5 text-sm text-zinc-200 hover:bg-white/5 cursor-pointer"
              onClick={() => props.onSelect?.(request.id)}
            >
              <div class="font-medium truncate">{request.label || request.id}</div>
              <Show when={request.agent_id}>
                <div class="text-[11px] text-zinc-500 truncate">{request.agent_id}</div>
              </Show>
            </button>
          )}
        </For>
      </Show>
    </div>
  );
}
