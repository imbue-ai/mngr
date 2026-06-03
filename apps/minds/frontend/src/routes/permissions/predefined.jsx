import { For, Show, createSignal } from 'solid-js';
import { Notice } from '../../components/ui/Notice.jsx';
import { PermissionRequest } from '../../components/permissions/PermissionRequest.jsx';

// Latchkey-backed predefined-permission request dialog. The simple view
// summarizes which permissions Approve will grant; clicking "Adjust"
// reveals the full checkbox editor. The hidden editor inputs always
// exist in the DOM so the shared submit handler can read the checked
// set in either view.
//
// Props:
//   agentId, requestId, wsName, rationale, displayName, accent
//                              -- forwarded to PermissionRequest.
//   permissionSchemas          -- ordered list of schema names rendered
//                                 as checkboxes (including the catch-all
//                                 "any" if applicable).
//   checkedPermissions         -- subset of schemas that should be
//                                 pre-checked on first render.
//   descriptionByPermissionName -- map of schema name -> plain-English
//                                 summary; rendered alongside the row
//                                 when present.
//   willOpenBrowser            -- toggles the progress copy between
//                                 "Authenticating..." (browser flow) and
//                                 "Granting permission..." (no flow).
export function PermissionsPredefinedRoute(props) {
  const [editorVisible, setEditorVisible] = createSignal(false);
  const checkedSet = new Set(props.checkedPermissions || []);
  const schemas = props.permissionSchemas || [];
  const descriptions = props.descriptionByPermissionName || {};
  const grantedSchemas = schemas.filter((schema) => checkedSet.has(schema));
  const wsLabel = props.wsName || props.agentId;

  const progressLabel = props.willOpenBrowser ? 'Authenticating...' : 'Granting permission...';
  const progressDetail = props.willOpenBrowser
    ? `Latchkey is opening a browser window for you to sign in to ${props.displayName}. This dialog will close automatically when the flow completes.`
    : '';

  return (
    <PermissionRequest
      agentId={props.agentId}
      requestId={props.requestId}
      wsName={props.wsName}
      rationale={props.rationale}
      displayName={props.displayName}
      accent={props.accent}
      progressLabel={progressLabel}
      progressDetail={progressDetail}
    >
      <div id="permissions-simple-view" class={editorVisible() ? 'hidden' : ''}>
        <Show
          when={grantedSchemas.length > 0}
          fallback={
            <>
              <Notice variant="warn">
                The agent did not request any specific permissions. Click{' '}
                <span class="font-medium">Adjust</span> below to choose what to grant.
              </Notice>
              <div class="flex justify-end mt-3">
                <button
                  type="button"
                  id="permissions-adjust-link"
                  onClick={() => setEditorVisible(true)}
                  class="text-xs text-blue-600 hover:underline cursor-pointer bg-transparent border-0 p-0"
                >
                  Adjust
                </button>
              </div>
            </>
          }
        >
          <p class="text-sm text-zinc-700 mb-3">
            Approving will grant {wsLabel} and its sibling agents the following permissions:
          </p>
          <div class="bg-white border border-zinc-200 rounded-xl p-4 space-y-3">
            <For each={grantedSchemas}>
              {(permission) => (
                <div>
                  <div class="flex items-center gap-2.5">
                    <svg
                      viewBox="0 0 20 20"
                      fill="currentColor"
                      aria-hidden="true"
                      class="w-4 h-4 shrink-0 text-emerald-600"
                    >
                      <path
                        fill-rule="evenodd"
                        clip-rule="evenodd"
                        d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 1 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
                      />
                    </svg>
                    <code class="text-sm font-mono text-zinc-900">{permission}</code>
                  </div>
                  <Show when={descriptions[permission]}>
                    <span class="block text-sm text-zinc-600 mt-0.5 ml-[26px]">
                      {descriptions[permission]}
                    </span>
                  </Show>
                </div>
              )}
            </For>
            <div class="flex justify-end border-t border-zinc-100 pt-2">
              <button
                type="button"
                id="permissions-adjust-link"
                onClick={() => setEditorVisible(true)}
                class="text-xs text-blue-600 hover:underline cursor-pointer bg-transparent border-0 p-0"
              >
                Adjust
              </button>
            </div>
          </div>
        </Show>
      </div>
      <div id="permissions-editor-view" class={editorVisible() ? '' : 'hidden'}>
        <h2 class="text-sm font-medium text-zinc-700 mb-2">Permissions to grant:</h2>
        <div class="bg-white border border-zinc-200 rounded-xl p-4 space-y-3">
          <For each={schemas}>
            {(permission) => (
              <label class="flex items-start gap-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  name="permissions"
                  value={permission}
                  class="mt-1 shrink-0"
                  checked={checkedSet.has(permission)}
                />
                <span class="min-w-0">
                  <code class="text-sm font-mono text-zinc-900">{permission}</code>
                  <Show when={descriptions[permission]}>
                    <span class="block text-sm text-zinc-600 mt-0.5">
                      {descriptions[permission]}
                    </span>
                  </Show>
                </span>
              </label>
            )}
          </For>
        </div>
      </div>
    </PermissionRequest>
  );
}
