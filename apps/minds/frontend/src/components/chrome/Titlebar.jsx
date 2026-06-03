import { Show } from 'solid-js';
import {
  isElectron as isElectronBridge,
  toggleSidebar,
  toggleRequestsPanel,
  minimizeWindow,
  maximizeWindow,
  closeWindow,
  contentGoBack,
  contentGoForward,
  navigateContent,
} from '../../lib/electron_bridge.js';

// The macOS-style titlebar that sits across the top of the persistent
// chrome shell. The full-window drag region, traffic-light padding, and
// per-workspace accent stripe all live here. The container is non-drag
// for buttons (-webkit-app-region: no-drag) but drag for everything
// else so the user can grab any flat area to move the window.
//
// Props:
//   isMac              -- toggles macOS traffic-light padding and hides
//                         the Windows-style window controls on the right.
//   isAuthenticated    -- selects the initial label of the "user" button
//                         while waiting for the auth status SSE event.
//   pageTitle          -- text rendered in the centered title slot.
//   workspaceAccent    -- OKLCH stripe color or null; when null the
//                         per-workspace swatch is hidden.
//   requestCount       -- pending-request badge count; >0 shows the dot.
//   onUserClick        -- called when the "Log in" / "Manage accounts"
//                         button is clicked (parent navigates).
//   userButtonLabel    -- label of the user button (computed by the
//                         chrome route from auth state).

const BUTTON_BASE =
  'bg-transparent border-none text-zinc-400 cursor-pointer w-8 h-7 flex items-center justify-center rounded hover:text-zinc-200 hover:bg-white/5 active:bg-white/10';
const WINDOW_CONTROL_BASE =
  'bg-transparent border-none text-zinc-400 cursor-pointer w-9 h-[38px] flex items-center justify-center hover:bg-white/5 hover:text-zinc-200';

export function Titlebar(props) {
  const containerClass = () =>
    [
      'fixed top-0 left-0 right-0 h-[38px] bg-zinc-900 flex items-center select-none z-[100] border-b border-white/10 px-1',
      props.isMac ? 'pl-[72px]' : '',
    ]
      .filter(Boolean)
      .join(' ');
  const handleHome = () => navigateContent('/');
  return (
    <div
      id="minds-titlebar"
      class={containerClass()}
      attr:style={
        props.workspaceAccent ? `--workspace-accent: ${props.workspaceAccent};` : undefined
      }
    >
      <div class="flex gap-0.5">
        <button
          type="button"
          id="sidebar-toggle"
          title="Projects"
          class={BUTTON_BASE}
          onClick={() => toggleSidebar() || props.onToggleSidebar?.()}
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <line x1="9" y1="3" x2="9" y2="21" />
          </svg>
        </button>
        <button
          type="button"
          id="home-btn"
          title="Home"
          class={BUTTON_BASE}
          onClick={handleHome}
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M3 12L12 3l9 9" />
            <path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10" />
          </svg>
        </button>
        <button
          type="button"
          id="back-btn"
          title="Back"
          class={BUTTON_BASE}
          onClick={() => contentGoBack()}
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </button>
        <button
          type="button"
          id="forward-btn"
          title="Forward"
          class={BUTTON_BASE}
          onClick={() => contentGoForward()}
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polyline points="9 6 15 12 9 18" />
          </svg>
        </button>
      </div>
      <div class="flex-1 flex items-center justify-center gap-2 px-2 min-w-0">
        <Show when={props.workspaceAccent}>
          <span
            id="title-swatch"
            class="accent-swatch w-2.5 h-2.5 rounded-sm shrink-0"
          />
        </Show>
        <span
          id="page-title"
          class="text-zinc-200 text-xs whitespace-nowrap overflow-hidden text-ellipsis"
        >
          {props.pageTitle || 'Minds'}
        </span>
      </div>
      <div class="relative minds-user-area shrink-0">
        <button
          type="button"
          id="user-btn"
          class="!w-auto !h-auto !inline-block text-zinc-400 cursor-pointer px-2.5 py-1 rounded text-xs font-sans whitespace-nowrap hover:bg-white/5 hover:text-zinc-200"
          title={props.userButtonTitle || 'Account'}
          onClick={() => props.onUserClick?.()}
        >
          {props.userButtonLabel || (props.isAuthenticated ? 'Manage account(s)' : 'Log in')}
        </button>
      </div>
      <button
        type="button"
        id="requests-toggle"
        title="Requests"
        class={'relative ' + BUTTON_BASE}
        onClick={() => toggleRequestsPanel()}
      >
        <svg
          class="w-4 h-4"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
        <Show when={(props.requestCount || 0) > 0}>
          <span
            id="requests-badge"
            class="absolute top-0.5 right-0.5 w-2 h-2 rounded-full bg-red-500"
          />
        </Show>
      </button>
      <Show when={!props.isMac}>
        <div class="flex">
          <button
            type="button"
            id="min-btn"
            title="Minimize"
            class={WINDOW_CONTROL_BASE}
            attr:style="border-radius: 0;"
            onClick={() => minimizeWindow()}
          >
            <svg
              viewBox="0 0 12 12"
              class="w-3 h-3"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <line x1="2" y1="6" x2="10" y2="6" />
            </svg>
          </button>
          <button
            type="button"
            id="max-btn"
            title="Maximize"
            class={WINDOW_CONTROL_BASE}
            attr:style="border-radius: 0;"
            onClick={() => maximizeWindow()}
          >
            <svg
              viewBox="0 0 12 12"
              class="w-3 h-3"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect x="2" y="2" width="8" height="8" rx="0.5" />
            </svg>
          </button>
          <button
            type="button"
            id="close-btn"
            title="Close"
            class={'bg-transparent border-none text-zinc-400 cursor-pointer w-9 h-[38px] flex items-center justify-center hover:bg-red-600 hover:text-white'}
            attr:style="border-radius: 0;"
            onClick={() => closeWindow()}
          >
            <svg
              viewBox="0 0 12 12"
              class="w-3 h-3"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <line x1="2" y1="2" x2="10" y2="10" />
              <line x1="10" y1="2" x2="2" y2="10" />
            </svg>
          </button>
        </div>
      </Show>
    </div>
  );
}

export function isElectron() {
  return isElectronBridge();
}
