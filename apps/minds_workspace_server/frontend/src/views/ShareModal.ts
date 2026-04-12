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

// Module-level state for the share modal (only one can be open at a time)
let modalLoading = true;
let modalStatus: SharingStatus | null = null;
let modalError: string | null = null;
let modalActionInProgress = false;
let modalCopied = false;
let modalFetchedFor: string | null = null;

function resetModalState(): void {
  modalLoading = true;
  modalStatus = null;
  modalError = null;
  modalActionInProgress = false;
  modalCopied = false;
  modalFetchedFor = null;
}

async function fetchStatus(serverName: string): Promise<void> {
  modalLoading = true;
  modalError = null;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`));
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      modalError = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      modalStatus = (await response.json()) as SharingStatus;
    }
  } catch (e) {
    modalError = `Network error: ${(e as Error).message}`;
  }
  modalLoading = false;
  m.redraw();
}

async function enableSharing(serverName: string): Promise<void> {
  modalActionInProgress = true;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`), { method: "PUT" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      modalError = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      modalStatus = (await response.json()) as SharingStatus;
    }
  } catch (e) {
    modalError = `Network error: ${(e as Error).message}`;
  }
  modalActionInProgress = false;
  m.redraw();
}

async function disableSharing(serverName: string): Promise<void> {
  modalActionInProgress = true;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`), { method: "DELETE" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      modalError = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      modalStatus = (await response.json()) as SharingStatus;
    }
  } catch (e) {
    modalError = `Network error: ${(e as Error).message}`;
  }
  modalActionInProgress = false;
  m.redraw();
}

export const ShareModal: m.Component<ShareModalAttrs> = {
  view(vnode) {
    const { serverName, onClose } = vnode.attrs;

    // Fetch once when the modal opens for a new server
    if (modalFetchedFor !== serverName) {
      resetModalState();
      modalFetchedFor = serverName;
      fetchStatus(serverName);
    }

    return m("div.share-modal-overlay", { onclick: (e: Event) => { if (e.target === e.currentTarget) { resetModalState(); onClose(); } } }, [
      m("div.share-modal", [
        m("h3.share-modal-title", `Share: ${serverName}`),

        modalLoading
          ? m("p.share-modal-loading", "Loading...")
          : modalError
            ? m("div", [
                m("p.share-modal-error", modalError),
                m("button.share-modal-btn.share-modal-btn-secondary", {
                  onclick: () => fetchStatus(serverName),
                }, "Retry"),
              ])
            : modalStatus?.enabled
              ? m("div", [
                  m("p.share-modal-label", "This application is shared globally:"),
                  m("div.share-modal-url-row", [
                    m("input.share-modal-url-input", {
                      type: "text",
                      readonly: true,
                      value: modalStatus.url ?? "(URL not available)",
                      onclick: (e: Event) => (e.target as HTMLInputElement).select(),
                    }),
                    m("button.share-modal-btn.share-modal-btn-primary", {
                      onclick: () => {
                        if (modalStatus?.url) {
                          navigator.clipboard.writeText(modalStatus.url);
                          modalCopied = true;
                          setTimeout(() => { modalCopied = false; m.redraw(); }, 2000);
                        }
                      },
                    }, modalCopied ? "Copied" : "Copy"),
                  ]),
                  m("button.share-modal-btn.share-modal-btn-destructive", {
                    disabled: modalActionInProgress,
                    onclick: () => disableSharing(serverName),
                  }, modalActionInProgress ? "Disabling..." : "Disable sharing"),
                ])
              : m("div", [
                  m("p.share-modal-label", "This application is not currently shared."),
                  m("button.share-modal-btn.share-modal-btn-primary", {
                    disabled: modalActionInProgress,
                    onclick: () => enableSharing(serverName),
                  }, modalActionInProgress ? "Enabling..." : "Enable sharing"),
                ]),

        m("div.share-modal-actions", [
          m("button.share-modal-btn.share-modal-btn-secondary", {
            onclick: () => { resetModalState(); onClose(); },
          }, "Close"),
        ]),
      ]),
    ]);
  },
};
