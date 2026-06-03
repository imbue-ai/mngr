import { createSignal, onCleanup, onMount, Show } from 'solid-js';
import { PageContainer } from '../components/ui/PageContainer.jsx';
import { Badge } from '../components/ui/Badge.jsx';
import { Spinner } from '../components/ui/Spinner.jsx';
import { Button } from '../components/ui/Button.jsx';
import { AccentStripe } from '../components/ui/AccentStripe.jsx';

// Destroy detail page. Mirrors templates/destroying.html plus the polling
// behaviour from static/destroying.js (the Jinja template loads the JS
// via a <script> tag, so we replace both at once).
//
// Polls /api/destroying/<id>/{status,log} every second and:
//   * appends new log content to the tail
//   * flips the badge between running / failed / done
//   * on "done", waits 800ms and navigates back to /
//   * on "failed", shows Retry + Dismiss actions
//
// Props (from the Python SSR shim):
//   * agent_id, agent_name, pid (int), status ("running"/"failed"/"done"),
//     accent (precomputed OKLCH string)

const POLL_INTERVAL_MS = 1000;
const DONE_REDIRECT_DELAY_MS = 800;

export function DestroyingRoute(props) {
  const initialStatus = props.status || 'running';
  const [status, setStatus] = createSignal(initialStatus);
  const [logText, setLogText] = createSignal('');
  const [retryDisabled, setRetryDisabled] = createSignal(false);
  const [dismissDisabled, setDismissDisabled] = createSignal(false);
  let logOffset = 0;
  let stopped = false;
  let timer = null;
  let logEl;

  const agentId = () => props.agent_id;

  async function fetchLog() {
    try {
      const resp = await fetch(`/api/destroying/${agentId()}/log?after=${logOffset}`);
      if (resp.status === 404) return;
      const data = await resp.json();
      if (!data) return;
      if (data.content) {
        setLogText((prev) => prev + data.content);
        // Defer to the next tick so the DOM has applied the new text.
        queueMicrotask(() => {
          if (logEl) logEl.scrollTop = logEl.scrollHeight;
        });
      }
      if (typeof data.next_offset === 'number') {
        logOffset = data.next_offset;
      }
    } catch {
      // Network blips during polling are silently ignored; the next tick
      // retries from the same offset.
    }
  }

  async function fetchStatus() {
    try {
      const resp = await fetch(`/api/destroying/${agentId()}/status`);
      if (resp.status === 404) return null;
      const data = await resp.json();
      return data?.status || null;
    } catch {
      return null;
    }
  }

  async function tick() {
    if (stopped) return;
    const [, nextStatus] = await Promise.all([fetchLog(), fetchStatus()]);
    if (nextStatus && nextStatus !== status()) {
      setStatus(nextStatus);
    }
    if (nextStatus === 'done') {
      stopped = true;
      await fetchLog();
      if (typeof window !== 'undefined') {
        window.setTimeout(() => {
          window.location.href = '/';
        }, DONE_REDIRECT_DELAY_MS);
      }
      return;
    }
    if (nextStatus === 'failed') {
      stopped = true;
      await fetchLog();
      return;
    }
    timer = window.setTimeout(tick, POLL_INTERVAL_MS);
  }

  async function handleRetry() {
    setRetryDisabled(true);
    try {
      const resp = await fetch(`/api/destroy-agent/${agentId()}`, { method: 'POST' });
      if (!resp.ok) {
        setRetryDisabled(false);
        if (typeof window !== 'undefined') window.alert('Could not start retry');
        return;
      }
      // Reset state and resume polling.
      setLogText('');
      logOffset = 0;
      stopped = false;
      setStatus('running');
      setRetryDisabled(false);
      tick();
    } catch {
      setRetryDisabled(false);
      if (typeof window !== 'undefined') window.alert('Could not start retry');
    }
  }

  async function handleDismiss() {
    setDismissDisabled(true);
    try {
      await fetch(`/api/destroying/${agentId()}/dismiss`, { method: 'POST' });
    } catch {
      // Best-effort dismiss: even if the POST fails, take the user back
      // to the landing page.
    }
    if (typeof window !== 'undefined') {
      window.location.href = '/';
    }
  }

  onMount(() => {
    tick();
  });
  onCleanup(() => {
    stopped = true;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  });

  return (
    <>
      <AccentStripe
        agentId={agentId()}
        accent={props.accent}
        class="fixed top-0 left-0 right-0 h-[3px] z-50 !overflow-visible !flex-none"
      />
      <PageContainer>
        <div
          id="destroying-page"
          data-agent-id={agentId()}
          data-pid={props.pid}
          data-initial-status={initialStatus}
        >
          <h1 class="text-xl font-semibold text-zinc-900 leading-tight">
            Destroying {props.agent_name || agentId()}
          </h1>
          <p class="text-xs text-zinc-400 mb-4">
            {agentId()} &middot; pid {props.pid}
          </p>

          <div id="destroying-status" class="my-4 flex items-center gap-2">
            <Show when={status() === 'running'}>
              <Spinner size="sm" />
              <span class="text-zinc-700">Running...</span>
            </Show>
            <Show when={status() === 'failed'}>
              <Badge variant="error">Failed</Badge>
            </Show>
            <Show when={status() === 'done'}>
              <Badge variant="success">Done. Redirecting...</Badge>
            </Show>
          </div>

          <h2 class="text-sm font-medium text-zinc-600 mt-6 mb-2">Log</h2>
          <div
            id="destroying-log"
            ref={(el) => {
              logEl = el;
            }}
            class="p-3 bg-zinc-900 text-zinc-200 font-mono text-xs rounded-xl max-h-[420px] overflow-y-auto whitespace-pre-wrap border border-zinc-900"
          >
            {logText()}
          </div>

          <Show when={status() === 'failed'}>
            <div id="destroying-actions" class="mt-6 flex gap-3">
              <Button
                variant="primary"
                id="destroying-retry-btn"
                disabled={retryDisabled()}
                onClick={handleRetry}
              >
                Retry
              </Button>
              <Button
                variant="secondary"
                id="destroying-dismiss-btn"
                disabled={dismissDisabled()}
                onClick={handleDismiss}
              >
                Dismiss
              </Button>
            </div>
          </Show>

          <div class="mt-8">
            <a href="/" class="text-blue-600 hover:underline">
              &larr; Back to projects
            </a>
          </div>
        </div>
      </PageContainer>
    </>
  );
}
