import { createSignal, onMount, Show, For } from 'solid-js';
import { AccentStripe } from '../components/ui/AccentStripe.jsx';
import { Spinner } from '../components/ui/Spinner.jsx';

// Workspace recovery page. Mirrors render_recovery_page() in templates.py,
// which used to build its HTML by hand inside a shared "loading card"
// shell from mngr_forward.loading_page. The Solid port collapses the
// hand-written script tag plus inline CSS into a regular component.
//
// Initial-state contract (props):
//   * agent_id (string)
//   * return_to (string; "" if no destination)
//   * initial_status: one of "stuck" / "restarting" / "restart_failed" / "healthy"
//   * initial_error (string; non-empty only on restart_failed)
//   * ssh_command (string | null)
//
// At runtime:
//   * stuck (default)    -- run host-health probe, auto-dispatch the
//                           appropriate restart tier, then refresh.
//   * restarting         -- show the spinner and meta-refresh after 1s.
//   * restart_failed     -- show the error and diagnostics; offer a
//                           manual restart and poll for healthy.
//   * healthy            -- offer a manual restart.

const REFRESH_INTERVAL_MS = 1000;

function probeGlyph(answer) {
  if (answer === 'yes') {
    return (
      <span class="probe-glyph probe-glyph-yes" aria-label="yes">
        {'✓'}
      </span>
    );
  }
  if (answer === 'no') {
    return (
      <span class="probe-glyph probe-glyph-no" aria-label="no">
        {'✗'}
      </span>
    );
  }
  return (
    <span class="probe-glyph probe-glyph-unknown" aria-label="unknown">
      ?
    </span>
  );
}

function ProbeRow(props) {
  const body = () => `$ ${props.probe.command}\n\n${props.probe.output}`;
  return (
    <details class={`probe-row probe-row-${props.probe.answer || 'unknown'}`}>
      <summary>
        {probeGlyph(props.probe.answer)}
        <span class="probe-question">{props.probe.question}</span>
      </summary>
      <pre>{body()}</pre>
    </details>
  );
}

export function RecoveryRoute(props) {
  // Map the four entry states onto a smaller set of render variants:
  //   * loading     -- spinner; transitions to one of the others on probe completion
  //   * unresponsive -- "Restart workspace" CTA + error / diagnostics
  //   * misconfigured -- "Try restart anyway" CTA; no auto-dispatch
  //   * dispatch_error -- POST to restart failed; user must retry
  const [view, setView] = createSignal(
    props.initial_status === 'restarting' || !props.initial_status
      ? 'loading'
      : props.initial_status === 'restart_failed'
        ? 'restart_failed'
        : props.initial_status === 'healthy'
          ? 'unresponsive'
          : 'loading',
  );
  const [healthData, setHealthData] = createSignal(null);

  const agentId = () => props.agent_id;
  const returnTo = () => props.return_to || '';
  const sshCommand = () => props.ssh_command || null;
  const initialError = () => props.initial_error || '';

  function pollUrl() {
    const ret = returnTo();
    let u = `/agents/${encodeURIComponent(agentId())}/recovery`;
    if (ret) u += `?return_to=${encodeURIComponent(ret)}`;
    return u;
  }

  function scheduleRefresh() {
    if (typeof window === 'undefined') return;
    window.setTimeout(() => {
      window.location.assign(pollUrl());
    }, REFRESH_INTERVAL_MS);
  }

  function scheduleHealthyPoll() {
    if (typeof window === 'undefined') return;
    window.setTimeout(async () => {
      try {
        const resp = await fetch(pollUrl(), {
          credentials: 'same-origin',
          redirect: 'manual',
        });
        if (resp.type === 'opaqueredirect' || (resp.status >= 300 && resp.status < 400)) {
          window.location.assign(pollUrl());
          return;
        }
        scheduleHealthyPoll();
      } catch {
        scheduleHealthyPoll();
      }
    }, REFRESH_INTERVAL_MS);
  }

  async function postRestart(path) {
    setView('loading');
    setHealthData(null);
    try {
      const resp = await fetch(
        `/api/agents/${encodeURIComponent(agentId())}${path}`,
        { method: 'POST', credentials: 'same-origin' },
      );
      if (resp.ok) {
        scheduleRefresh();
      } else {
        setView('dispatch_error');
      }
    } catch {
      setView('dispatch_error');
    }
  }

  async function runProbe(autoDispatch) {
    setView('loading');
    setHealthData(null);
    try {
      const resp = await fetch(
        `/api/agents/${encodeURIComponent(agentId())}/host-health`,
        { credentials: 'same-origin' },
      );
      const data = await resp.json();
      setHealthData(data || null);
      const tier = data && data.dispatch_tier;
      if (tier === 'workspace_misconfigured') {
        setView('misconfigured');
        return;
      }
      if (!autoDispatch) {
        setView('unresponsive');
        return;
      }
      if (tier === 'host_offline') {
        await postRestart('/restart-host?host_already_stopped=1');
        return;
      }
      if (tier === 'interface_unresponsive') {
        await postRestart('/restart-system-interface');
        return;
      }
      setView('unresponsive');
    } catch {
      setView('unresponsive');
    }
  }

  function copyDiagnostics() {
    const data = healthData();
    if (!data) return;
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(JSON.stringify(data, null, 2)).catch(() => {});
    }
  }

  function copySshCommand() {
    const cmd = sshCommand();
    if (!cmd) return;
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(cmd).catch(() => {});
    }
  }

  onMount(() => {
    const initial = props.initial_status || 'stuck';
    if (initial === 'restarting') {
      scheduleRefresh();
      return;
    }
    if (initial === 'restart_failed') {
      runProbe(false);
      scheduleHealthyPoll();
      return;
    }
    if (initial === 'healthy') {
      setView('unresponsive');
      return;
    }
    runProbe(true);
  });

  const isLoading = () => view() === 'loading';
  const titleText = () =>
    view() === 'misconfigured' ? 'Workspace misconfigured' :
    isLoading() ? 'Loading workspace' :
    'Workspace unresponsive';
  const messageText = () => {
    if (isLoading()) return '';
    if (view() === 'misconfigured') {
      return (
        "This workspace's services.toml is missing the [services.system_interface] entry, "
        + 'so the system interface cannot be started. A restart is unlikely to help -- '
        + 'fix services.toml first. See the diagnostics below for details.'
      );
    }
    if (view() === 'dispatch_error') {
      return 'Could not start the restart. Check your connection and try again.';
    }
    return (
      'This workspace needs a restart to recover. In-progress work in all agents will be '
      + 'interrupted. If the problem persists, contact support.'
    );
  };

  const showRestartButton = () => view() === 'unresponsive' || view() === 'misconfigured' || view() === 'dispatch_error';
  const restartLabel = () => (view() === 'misconfigured' ? 'Try restart anyway' : 'Restart workspace');

  const probes = () => {
    const data = healthData();
    return Array.isArray(data?.probes) ? data.probes : [];
  };
  const showDebugDetails = () => probes().length > 0;
  // Show the error details disclosure whenever the page was entered in the
  // restart_failed state and an error reason was supplied -- regardless of
  // whether we've since dropped into loading while the probe re-runs.
  const showErrorDetails = () =>
    props.initial_status === 'restart_failed' && initialError().length > 0;

  return (
    <>
      <AccentStripe
        agentId={agentId()}
        class="fixed top-0 left-0 right-0 h-[3px] z-50 !overflow-visible !flex-none"
      />
      <div class="min-h-screen flex items-center justify-center bg-zinc-50 px-6 py-6">
        <div
          class="card bg-white border border-zinc-200 rounded-xl shadow-sm p-6 max-w-md w-full flex flex-col max-h-[calc(100vh-48px)]"
          data-agent-id={agentId()}
          data-return-to={returnTo()}
          data-initial-status={props.initial_status || 'stuck'}
        >
          <div class="row flex items-center gap-3">
            <Show when={isLoading()}>
              <Spinner size="md" />
            </Show>
            <h1 id="loading-title" class="text-base font-semibold text-zinc-900 m-0">
              {titleText()}
            </h1>
          </div>
          <p id="loading-message" class="text-sm text-zinc-500 mt-2">
            {messageText()}
          </p>

          <Show when={showRestartButton()}>
            <button
              id="recovery-host-btn"
              type="button"
              class={`mt-5 w-full rounded-lg px-4 py-3 text-sm font-semibold text-white cursor-pointer transition-colors ${
                view() === 'misconfigured' ? 'bg-zinc-500 hover:bg-zinc-600' : 'bg-zinc-900 hover:bg-zinc-700'
              }`}
              onClick={() => postRestart('/restart-host')}
            >
              {restartLabel()}
            </button>
          </Show>

          <Show when={showErrorDetails() || showDebugDetails()}>
            <div class="recovery-troubleshooting mt-5 pt-4 border-t border-zinc-100">
              <p class="recovery-troubleshooting-label">Troubleshooting</p>
              <Show when={showErrorDetails()}>
                <details id="recovery-error">
                  <summary>Error details</summary>
                  <pre>{initialError()}</pre>
                </details>
              </Show>
              <Show when={showDebugDetails()}>
                <details id="recovery-debug-details">
                  <summary>Diagnostics</summary>
                  <div id="recovery-debug-content">
                    <For each={probes()}>{(probe) => <ProbeRow probe={probe} />}</For>
                  </div>
                  <div class="debug-section">
                    <button id="copy-diagnostics-btn" type="button" onClick={copyDiagnostics}>
                      Copy diagnostics
                    </button>
                    <Show when={sshCommand() !== null}>
                      <button
                        id="copy-ssh-btn"
                        type="button"
                        data-ssh-command={sshCommand()}
                        onClick={copySshCommand}
                      >
                        Copy SSH command
                      </button>
                    </Show>
                  </div>
                </details>
              </Show>
            </div>
          </Show>
        </div>
      </div>
    </>
  );
}
