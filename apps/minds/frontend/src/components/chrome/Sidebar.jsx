import { For, Show, createMemo, createSignal } from 'solid-js';
import {
  navigateContent,
  openWorkspaceInNewWindow,
  showWorkspaceContextMenu,
} from '../../lib/electron_bridge.js';
import { workspaceAccentCached } from '../../lib/workspace_accent.js';

// Renders the grouped workspace list shown in the persistent shell's
// left sidebar and the standalone Electron sidebar WebContentsView.
//
// Props:
//   workspaces            -- array of { id, name, account?, accent? } shapes
//                            emitted by the chrome SSE workspaces event.
//   mngrForwardOrigin     -- bare origin of the mngr_forward plugin; the
//                            sidebar builds /goto/<agent>/ URLs against it.
//   currentWorkspaceId    -- workspace whose row should render with the
//                            "current" highlight (only used by the
//                            Electron-side sidebar bundle today).
//   showOpenInNewWindow   -- true for the standalone sidebar bundle (the
//                            row exposes a hover-revealed "open in new
//                            window" affordance); false for the
//                            chrome-embedded sidebar.

function groupWorkspaces(workspaces) {
  // Mirrors the legacy chrome.js grouping: "Private" first, then any
  // account groups alphabetically.
  const groups = new Map();
  for (const workspace of workspaces) {
    const key = workspace.account || 'Private';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(workspace);
  }
  return [...groups.keys()]
    .sort((a, b) => {
      if (a === 'Private') return -1;
      if (b === 'Private') return 1;
      return a.localeCompare(b);
    })
    .map((key) => ({ key, workspaces: groups.get(key) }));
}

function accentForWorkspace(workspace) {
  if (typeof workspace.accent === 'string') return workspace.accent;
  return workspaceAccentCached(workspace.id);
}

function WorkspaceRow(props) {
  const [isHovered, setIsHovered] = createSignal(false);
  const accent = createMemo(() => accentForWorkspace(props.workspace));
  const accentStyleString = createMemo(() => `--workspace-accent: ${accent()};`);
  const baseClass =
    'sidebar-item group cursor-pointer text-sm font-medium text-zinc-200 rounded-md mx-1.5 my-0.5 py-2.5 pl-4 pr-3 flex items-center justify-between gap-2 transition-colors hover:bg-white/5';
  const rowClass = () =>
    [baseClass, props.isCurrent ? 'is-current bg-white/5' : ''].filter(Boolean).join(' ');
  const handleClick = (event) => {
    if (event.defaultPrevented) return;
    navigateContent(`${props.mngrForwardOrigin}/goto/${props.workspace.id}/`);
  };
  const handleOpenNew = (event) => {
    event.preventDefault();
    event.stopPropagation();
    openWorkspaceInNewWindow(props.workspace.id);
  };
  const handleContextMenu = (event) => {
    event.preventDefault();
    showWorkspaceContextMenu(props.workspace.id, event.clientX, event.clientY);
  };
  const openNewVisible = () => props.showOpenInNewWindow && isHovered() && !props.isCurrent;
  return (
    <div
      class={rowClass()}
      data-agent-id={props.workspace.id}
      attr:style={accentStyleString()}
      onClick={handleClick}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onContextMenu={handleContextMenu}
    >
      <span class="flex-1 whitespace-nowrap overflow-hidden text-ellipsis">
        {props.workspace.name || props.workspace.id}
      </span>
      <Show when={props.showOpenInNewWindow}>
        <button
          type="button"
          class={
            'sidebar-open-new items-center justify-center bg-transparent border-none p-1 cursor-pointer text-zinc-400 rounded hover:text-zinc-200 hover:bg-white/5 ' +
            (openNewVisible() ? 'inline-flex' : 'hidden')
          }
          title="Open in new window"
          tabIndex={-1}
          data-open-new={props.workspace.id}
          onClick={handleOpenNew}
        >
          <svg
            class="w-3.5 h-3.5"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M14 3h7v7" />
            <path d="M10 14L21 3" />
            <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
          </svg>
        </button>
      </Show>
    </div>
  );
}

export function Sidebar(props) {
  const groups = createMemo(() => groupWorkspaces(props.workspaces || []));
  const hasWorkspaces = () => (props.workspaces || []).length > 0;
  return (
    <div id="sidebar-workspaces">
      <Show
        when={hasWorkspaces()}
        fallback={
          <div class="px-4 py-6 text-sm text-zinc-400 text-center">No projects</div>
        }
      >
        <For each={groups()}>
          {(group) => (
            <>
              <div class="px-3 pt-2 pb-0.5 text-[11px] text-zinc-400 tracking-wider">
                {group.key === 'Private' ? 'PRIVATE' : group.key}
              </div>
              <For each={group.workspaces}>
                {(workspace) => (
                  <WorkspaceRow
                    workspace={workspace}
                    mngrForwardOrigin={props.mngrForwardOrigin || ''}
                    isCurrent={workspace.id === props.currentWorkspaceId}
                    showOpenInNewWindow={!!props.showOpenInNewWindow}
                  />
                )}
              </For>
            </>
          )}
        </For>
      </Show>
    </div>
  );
}
