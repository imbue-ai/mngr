import { createContext, useContext, onCleanup } from 'solid-js';
import { createStore, produce } from 'solid-js/store';

// Global SSE-driven store. Subscribes to a single EventSource (today
// /_chrome/events; the migration extends it to carry per-page state too)
// and applies typed envelope events to slices of a single store.
//
// Envelope shape: { topic: string, payload: any }. Each topic owns one
// slice of the store keyed by the topic name. Components subscribe by
// reading `store.<topic>`; Solid's fine-grained reactivity handles the
// per-component update.
//
// Reconnect:
//   * EventSource handles transport-level reconnects automatically.
//   * On reconnect the server is expected to push a `snapshot` envelope
//     carrying the full state, so we replace rather than merge.

const SseStoreContext = createContext(null);

function makeInitialState(initial) {
  // Always start from a frozen-shape object so components can rely on
  // every slice existing (even if empty) when they mount.
  return {
    workspaces: [],
    auth: { status: 'unknown' },
    requests: { count: 0, ids: [] },
    providers: [],
    ...(initial || {}),
  };
}

export function createSseStore({ url, initial, eventSourceImpl } = {}) {
  const [state, setState] = createStore(makeInitialState(initial));
  let eventSource = null;
  let closed = false;

  function applyEnvelope(envelope) {
    if (envelope == null || typeof envelope !== 'object') return;
    const { topic, payload } = envelope;
    if (typeof topic !== 'string') return;
    if (topic === 'snapshot' && payload && typeof payload === 'object') {
      setState(produce((s) => {
        for (const key of Object.keys(payload)) {
          s[key] = payload[key];
        }
      }));
      return;
    }
    setState(topic, payload);
  }

  function open() {
    if (!url) return;
    if (closed) return;
    const Impl = eventSourceImpl || (typeof EventSource !== 'undefined' ? EventSource : null);
    if (!Impl) return; // SSR / Node without polyfill -- caller seeded `initial`.
    eventSource = new Impl(url);
    eventSource.onmessage = (event) => {
      try {
        const env = JSON.parse(event.data);
        applyEnvelope(env);
      } catch {
        // Malformed envelopes are dropped silently; logging them would
        // spam the console on every transient parse hiccup.
      }
    };
  }

  function close() {
    closed = true;
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  return { state, applyEnvelope, open, close };
}

export function SseStoreProvider(props) {
  const store = createSseStore({
    url: props.url,
    initial: props.initial,
    eventSourceImpl: props.eventSourceImpl,
  });
  store.open();
  onCleanup(() => store.close());
  return (
    <SseStoreContext.Provider value={store}>
      {props.children}
    </SseStoreContext.Provider>
  );
}

export function useSseStore() {
  const store = useContext(SseStoreContext);
  if (!store) {
    throw new Error('useSseStore() requires <SseStoreProvider> above it');
  }
  return store;
}
