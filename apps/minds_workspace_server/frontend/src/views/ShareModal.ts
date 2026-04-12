/**
 * Modal dialog for managing Cloudflare forwarding (sharing) for a server.
 * Fetches current status, allows enabling/disabling, and provides a copy-to-clipboard button.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface ShareModalAttrs {
  serverName: string;
  onClose: () => void;
}

interface SharingStatus {
  enabled: boolean;
  url: string | null;
}

interface ModalState {
  loading: boolean;
  status: SharingStatus | null;
  error: string | null;
  actionInProgress: boolean;
  copied: boolean;
}

async function fetchStatus(serverName: string, state: ModalState): Promise<void> {
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`));
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      state.error = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      state.status = (await response.json()) as SharingStatus;
    }
  } catch (e) {
    state.error = `Network error: ${(e as Error).message}`;
  }
  state.loading = false;
  m.redraw();
}

async function enableSharing(serverName: string, state: ModalState): Promise<void> {
  state.actionInProgress = true;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`), { method: "PUT" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      state.error = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      state.status = (await response.json()) as SharingStatus;
    }
  } catch (e) {
    state.error = `Network error: ${(e as Error).message}`;
  }
  state.actionInProgress = false;
  m.redraw();
}

async function disableSharing(serverName: string, state: ModalState): Promise<void> {
  state.actionInProgress = true;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`), { method: "DELETE" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      state.error = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      state.status = (await response.json()) as SharingStatus;
    }
  } catch (e) {
    state.error = `Network error: ${(e as Error).message}`;
  }
  state.actionInProgress = false;
  m.redraw();
}

export function ShareModal(): m.Component<ShareModalAttrs> {
  const state: ModalState = {
    loading: true,
    status: null,
    error: null,
    actionInProgress: false,
    copied: false,
  };

  let initialized = false;

  return {
    view(vnode) {
      const { serverName, onClose } = vnode.attrs;

      if (!initialized) {
        initialized = true;
        fetchStatus(serverName, state);
      }

      return m("div.share-modal-overlay", { onclick: (e: Event) => { if (e.target === e.currentTarget) onClose(); } }, [
        m("div.share-modal", [
          m("h3.share-modal-title", `Share: ${serverName}`),

          state.loading
            ? m("p.share-modal-loading", "Loading...")
            : state.error
              ? m("div", [
                  m("p.share-modal-error", state.error),
                  m("button.share-modal-btn.share-modal-btn-secondary", {
                    onclick: () => { state.error = null; state.loading = true; fetchStatus(serverName, state); },
                  }, "Retry"),
                ])
              : state.status?.enabled
                ? m("div", [
                    m("p.share-modal-label", "This application is shared globally:"),
                    m("div.share-modal-url-row", [
                      m("input.share-modal-url-input", {
                        type: "text",
                        readonly: true,
                        value: state.status.url ?? "(URL not available)",
                        onclick: (e: Event) => (e.target as HTMLInputElement).select(),
                      }),
                      m("button.share-modal-btn.share-modal-btn-primary", {
                        onclick: () => {
                          if (state.status?.url) {
                            navigator.clipboard.writeText(state.status.url);
                            state.copied = true;
                            setTimeout(() => { state.copied = false; m.redraw(); }, 2000);
                          }
                        },
                      }, state.copied ? "Copied" : "Copy"),
                    ]),
                    m("button.share-modal-btn.share-modal-btn-destructive", {
                      disabled: state.actionInProgress,
                      onclick: () => disableSharing(serverName, state),
                    }, state.actionInProgress ? "Disabling..." : "Disable sharing"),
                  ])
                : m("div", [
                    m("p.share-modal-label", "This application is not currently shared."),
                    m("button.share-modal-btn.share-modal-btn-primary", {
                      disabled: state.actionInProgress,
                      onclick: () => enableSharing(serverName, state),
                    }, state.actionInProgress ? "Enabling..." : "Enable sharing"),
                  ]),

          m("div.share-modal-actions", [
            m("button.share-modal-btn.share-modal-btn-secondary", { onclick: onClose }, "Close"),
          ]),
        ]),
      ]);
    },
  };
}
