import { Show, createSignal, onMount, onCleanup } from 'solid-js';
import { Titlebar } from '../components/chrome/Titlebar.jsx';
import { Sidebar } from '../components/chrome/Sidebar.jsx';
import {
  isElectron,
  navigateContent,
  onChromeEvent,
  onContentTitleChange,
  onContentURLChange,
  onCurrentWorkspaceChanged,
  onWindowTitleChange,
} from '../lib/electron_bridge.js';
import { workspaceAccentCached } from '../lib/workspace_accent.js';

// The persistent chrome shell: titlebar across the top, optional
// slide-in workspace sidebar on the left, and (in browser mode) the
// content iframe filling the rest. In Electron the content + sidebar
// are separate WebContentsViews -- the iframe and the slide-in panel
// are hidden via the `isElectron()` check below.
//
// Props:
//   isMac                   -- macOS-style traffic-light padding.
//   isAuthenticated         -- initial state for the user button label
//                              before /auth/api/status reports back.
//   mngrForwardOrigin       -- bare origin of the mngr_forward plugin.
//   initialWorkspaces       -- workspace list seeded from server-side
//                              data; the SSE subscription replaces it.

const RECOVERY_NAVIGATION = new Map();

function buildRecoveryUrl(agentId, mngrForwardOrigin) {
  const fallback = `${mngrForwardOrigin}/goto/${agentId}/`;
  let returnTo = '';
  try {
    if (typeof document !== 'undefined') {
      const frame = document.getElementById('content-frame');
      if (frame && frame.contentWindow) {
        returnTo = frame.contentWindow.location.href || '';
      }
    }
  } catch {
    returnTo = '';
  }
  if (!returnTo) returnTo = fallback;
  return `/agents/${encodeURIComponent(agentId)}/recovery?return_to=${encodeURIComponent(returnTo)}`;
}

export function ChromeRoute(props) {
  const [sidebarOpen, setSidebarOpen] = createSignal(false);
  const [workspaces, setWorkspaces] = createSignal(props.initialWorkspaces || []);
  const [pageTitle, setPageTitle] = createSignal('Minds');
  const [currentWorkspaceId, setCurrentWorkspaceId] = createSignal(null);
  const [requestCount, setRequestCount] = createSignal(0);
  const [signedIn, setSignedIn] = createSignal(!!props.isAuthenticated);
  const [userEmail, setUserEmail] = createSignal('');
  const systemInterfaceStatus = new Map();
  const mngrForwardOrigin = props.mngrForwardOrigin || '';

  const accent = () => {
    const aid = currentWorkspaceId();
    return aid ? workspaceAccentCached(aid) : null;
  };

  const handleUserClick = () => {
    if (signedIn()) navigateContent('/accounts');
    else navigateContent('/auth/login');
  };

  const maybeRedirectToRecovery = (agentId) => {
    if (!agentId) return;
    if (systemInterfaceStatus.get(agentId) !== 'stuck') return;
    if (RECOVERY_NAVIGATION.get(agentId)) return;
    RECOVERY_NAVIGATION.set(agentId, true);
    navigateContent(buildRecoveryUrl(agentId, mngrForwardOrigin));
  };

  const handleChromeEvent = (data) => {
    if (!data || typeof data !== 'object') return;
    if (data.type === 'workspaces' && Array.isArray(data.workspaces)) {
      setWorkspaces(data.workspaces);
      return;
    }
    if (data.type === 'auth_status') {
      setSignedIn(!!data.signedIn);
      setUserEmail(data.email || '');
      return;
    }
    if (data.type === 'requests') {
      setRequestCount(Number(data.count) || 0);
      return;
    }
    if (data.type === 'system_interface_status') {
      if (data.status === 'healthy') {
        systemInterfaceStatus.delete(data.agent_id);
        RECOVERY_NAVIGATION.delete(data.agent_id);
        return;
      }
      systemInterfaceStatus.set(data.agent_id, data.status);
      if (data.agent_id === currentWorkspaceId()) {
        maybeRedirectToRecovery(data.agent_id);
      }
    }
  };

  const refreshAuthStatus = async () => {
    try {
      const response = await fetch('/auth/api/status');
      if (!response.ok) return;
      const data = await response.json();
      setSignedIn(!!data.signedIn);
      setUserEmail(data.email || '');
    } catch {
      // SSE-only mode (offline / dev); leave the cached state alone.
    }
  };

  onMount(() => {
    refreshAuthStatus();
    // Title-tracking inside the iframe content (browser mode). In
    // Electron the content WebContentsView pushes title changes over
    // IPC instead.
    if (isElectron()) {
      const titleHandler = (title) => setPageTitle(title || 'Minds');
      if (!onWindowTitleChange(titleHandler)) {
        onContentTitleChange(titleHandler);
      }
      onContentURLChange(() => {
        refreshAuthStatus();
      });
      onCurrentWorkspaceChanged((agentId) => {
        const next = agentId || null;
        if (next !== currentWorkspaceId()) {
          RECOVERY_NAVIGATION.delete(next);
        }
        setCurrentWorkspaceId(next);
        maybeRedirectToRecovery(next);
      });
    } else {
      const interval = setInterval(() => {
        try {
          if (typeof document === 'undefined') return;
          const frame = document.getElementById('content-frame');
          if (!frame || !frame.contentWindow) return;
          const innerDoc = frame.contentDocument;
          if (innerDoc && innerDoc.title) setPageTitle(innerDoc.title);
          const match = frame.contentWindow.location.pathname.match(/^\/goto\/([^/]+)/);
          const next = match ? match[1] : null;
          if (next !== currentWorkspaceId()) {
            RECOVERY_NAVIGATION.delete(next);
          }
          setCurrentWorkspaceId(next);
          maybeRedirectToRecovery(next);
        } catch {
          // cross-origin probes are expected during navigation; ignore.
        }
      }, 500);
      onCleanup(() => clearInterval(interval));
    }
    // SSE wire-up. Electron uses window.minds.onChromeEvent to receive
    // multiplexed envelopes; the browser path opens its own
    // /_chrome/events stream.
    if (!onChromeEvent(handleChromeEvent)) {
      const Impl =
        props.eventSourceImpl ||
        (typeof EventSource !== 'undefined' ? EventSource : null);
      if (!Impl) return;
      let eventSource = new Impl(props.sseUrl || '/_chrome/events');
      eventSource.onmessage = (event) => {
        try {
          handleChromeEvent(JSON.parse(event.data));
        } catch {
          // malformed payloads dropped silently
        }
      };
      onCleanup(() => {
        if (eventSource) {
          eventSource.close();
          eventSource = null;
        }
      });
    }
  });

  const electronMode = isElectron();

  return (
    <div
      class="font-sans antialiased bg-zinc-900 min-h-screen"
      data-is-mac={props.isMac ? 'true' : 'false'}
      data-is-authenticated={props.isAuthenticated ? 'true' : 'false'}
      data-mngr-forward-origin={mngrForwardOrigin}
    >
      <Titlebar
        isMac={!!props.isMac}
        isAuthenticated={signedIn()}
        pageTitle={pageTitle()}
        workspaceAccent={accent()}
        requestCount={requestCount()}
        userButtonTitle={userEmail() || (signedIn() ? 'Manage accounts' : 'Sign in to your account')}
        userButtonLabel={signedIn() ? 'Manage account(s)' : 'Log in'}
        onUserClick={handleUserClick}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
      />
      <Show when={!electronMode}>
        <div
          id="sidebar-panel"
          class={
            'fixed left-0 top-[38px] w-[260px] h-[calc(100%-38px)] bg-zinc-900 z-50 shadow-[4px_0_12px_rgba(0,0,0,0.3)] transition-transform duration-200 ease-in-out overflow-y-auto border-r border-white/10 ' +
            (sidebarOpen() ? '' : '-translate-x-full')
          }
        >
          <Sidebar
            workspaces={workspaces()}
            mngrForwardOrigin={mngrForwardOrigin}
            currentWorkspaceId={currentWorkspaceId()}
            showOpenInNewWindow={false}
          />
        </div>
        <iframe
          id="content-frame"
          src="/"
          class="fixed left-[6px] top-[38px] border-0 rounded-xl bg-zinc-50"
          attr:style="width: calc(100% - 12px); height: calc(100% - 44px); box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset;"
        />
      </Show>
    </div>
  );
}
