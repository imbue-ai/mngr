import { createSignal, onMount, onCleanup } from 'solid-js';
import { Sidebar } from '../components/chrome/Sidebar.jsx';
import { onChromeEvent, onCurrentWorkspaceChanged } from '../lib/electron_bridge.js';

// Route component for the standalone sidebar WebContentsView. Wraps
// <Sidebar /> with the SSE subscription that keeps the workspace list
// fresh (and, in Electron, the IPC subscription that highlights the
// currently-active workspace).
//
// Props:
//   mngrForwardOrigin       -- bare origin of the mngr_forward plugin.
//   workspaces              -- initial workspace list from the server
//                              (used both for SSR and as the seed before
//                              the SSE pump catches up).
//   sseUrl                  -- override for the SSE source URL (tests).
export function SidebarRoute(props) {
  const [workspaces, setWorkspaces] = createSignal(props.workspaces || []);
  const [currentWorkspaceId, setCurrentWorkspaceId] = createSignal(null);

  const updateFromChromeEvent = (data) => {
    if (!data || typeof data !== 'object') return;
    if (data.type === 'workspaces' && Array.isArray(data.workspaces)) {
      setWorkspaces(data.workspaces);
    }
  };

  onMount(() => {
    // The standalone sidebar bundle uses window.minds.onChromeEvent
    // (Electron-only IPC); in browser mode the chrome page hosts its
    // own SSE, but we also fall back to direct SSE here so the sidebar
    // remains useful when opened standalone (e.g. dev). Mirrors the
    // logic in static/sidebar.js.
    if (onChromeEvent(updateFromChromeEvent)) {
      onCurrentWorkspaceChanged((agentId) => setCurrentWorkspaceId(agentId || null));
      return;
    }
    const Impl =
      props.eventSourceImpl ||
      (typeof EventSource !== 'undefined' ? EventSource : null);
    if (!Impl) return;
    let eventSource = new Impl(props.sseUrl || '/_chrome/events');
    eventSource.onmessage = (event) => {
      try {
        updateFromChromeEvent(JSON.parse(event.data));
      } catch {
        // malformed events are silently dropped, matching the legacy code
      }
    };
    onCleanup(() => {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
    });
  });

  return (
    <div class="bg-zinc-900 font-sans antialiased overflow-y-auto min-h-screen">
      <h2 class="text-sm text-zinc-200 p-3 m-0 border-b border-white/10 font-medium">
        Projects
      </h2>
      <Sidebar
        workspaces={workspaces()}
        mngrForwardOrigin={props.mngrForwardOrigin || ''}
        currentWorkspaceId={currentWorkspaceId()}
        showOpenInNewWindow
      />
    </div>
  );
}
