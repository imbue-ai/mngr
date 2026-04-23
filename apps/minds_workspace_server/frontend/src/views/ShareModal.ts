/**
 * Read-only modal dialog for viewing Cloudflare forwarding (sharing) status.
 * Shows sharing status, shared URL (with copy button), and who has access.
 * Editing is done via request events processed by the minds desktop client.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface ShareModalAttrs {
  serviceName: string;
  onClose: () => void;
}

interface SharingStatus {
  enabled: boolean;
  url: string | null;
  auth_rules: Array<{ action: string; include: Array<{ email?: { email: string } }> }>;
}

// Module-level state for the share modal (only one can be open at a time)
let modalLoading = true;
let modalStatus: SharingStatus | null = null;
let modalError: string | null = null;
let modalCopied = false;
let modalFetchedFor: string | null = null;
let modalRequestInProgress = false;
let modalRequestSent = false;

function resetModalState(): void {
  modalLoading = true;
  modalStatus = null;
  modalError = null;
  modalCopied = false;
  modalFetchedFor = null;
  modalRequestInProgress = false;
  modalRequestSent = false;
}

function extractEmails(status: SharingStatus): string[] {
  const emails: string[] = [];
  for (const rule of status.auth_rules) {
    for (const inc of rule.include || []) {
      if (inc.email?.email) {
        emails.push(inc.email.email);
      }
    }
  }
  return emails;
}

async function fetchStatus(serviceName: string): Promise<void> {
  modalLoading = true;
  modalError = null;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serviceName)}`));
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

async function requestEdit(serviceName: string, onClose: () => void): Promise<void> {
  modalRequestInProgress = true;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serviceName)}/request`), {
      method: "POST",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      modalError = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
      modalRequestInProgress = false;
      m.redraw();
      return;
    }
    modalRequestSent = true;
    modalRequestInProgress = false;
    m.redraw();
    // Close after a brief delay so the user sees the confirmation
    setTimeout(() => {
      resetModalState();
      onClose();
    }, 1500);
  } catch (e) {
    modalError = `Network error: ${(e as Error).message}`;
    modalRequestInProgress = false;
    m.redraw();
  }
}

function renderEmailList(emails: string[]): m.Vnode {
  if (emails.length === 0) {
    return m("p", { style: "color: #666; font-size: 13px;" }, "No access policies configured");
  }
  return m("div.share-modal-emails", [
    m("p", { style: "font-size: 13px; color: #666; margin-bottom: 4px;" }, "Shared with:"),
    m(
      "ul.share-modal-email-list",
      emails.map((email) => m("li.share-modal-email-item", { key: email }, [m("span", email)])),
    ),
  ]);
}

export const ShareModal: m.Component<ShareModalAttrs> = {
  view(vnode) {
    const { serviceName, onClose } = vnode.attrs;

    if (modalFetchedFor !== serviceName) {
      resetModalState();
      modalFetchedFor = serviceName;
      fetchStatus(serviceName);
    }

    const emails = modalStatus ? extractEmails(modalStatus) : [];
    const isEnabled = modalStatus?.enabled ?? false;
    const title = isEnabled ? `${serviceName} sharing` : `${serviceName} sharing`;

    const close = () => {
      resetModalState();
      onClose();
    };

    return m(
      "div.share-modal-overlay",
      {
        onclick: (e: Event) => {
          if (e.target === e.currentTarget) close();
        },
      },
      [
        m("div.share-modal", [
          m("div.share-modal-header", [
            m("h3.share-modal-title", title),
            m("button.share-modal-close-x", { onclick: close, title: "Close" }, "x"),
          ]),

          modalRequestSent
            ? m(
                "p",
                { style: "padding: 16px; color: #22c55e; text-align: center;" },
                "Sharing request sent -- check the Minds app inbox",
              )
            : modalLoading
              ? m("p.share-modal-loading", "Loading...")
              : modalError
                ? m("div", [
                    m("p.share-modal-error", modalError),
                    m(
                      "button.share-modal-btn.share-modal-btn-secondary",
                      {
                        onclick: () => fetchStatus(serviceName),
                      },
                      "Retry",
                    ),
                  ])
                : isEnabled
                  ? m("div", [
                      m("div.share-modal-url-row", [
                        m("input.share-modal-url-input", {
                          type: "text",
                          readonly: true,
                          value: modalStatus?.url ?? "(URL not available)",
                          onclick: (e: Event) => (e.target as HTMLInputElement).select(),
                        }),
                        m(
                          "button.share-modal-btn.share-modal-btn-secondary",
                          {
                            onclick: () => {
                              if (modalStatus?.url) {
                                navigator.clipboard.writeText(modalStatus.url);
                                modalCopied = true;
                                setTimeout(() => {
                                  modalCopied = false;
                                  m.redraw();
                                }, 2000);
                              }
                            },
                          },
                          modalCopied ? "Copied" : "Copy",
                        ),
                      ]),
                      renderEmailList(emails),
                      m("div.share-modal-footer", [
                        m("button.share-modal-btn.share-modal-btn-secondary", { onclick: close }, "Close"),
                        m(
                          "button.share-modal-btn.share-modal-btn",
                          {
                            disabled: modalRequestInProgress,
                            onclick: () => requestEdit(serviceName, onClose),
                          },
                          modalRequestInProgress ? "Sending..." : "Edit sharing",
                        ),
                      ]),
                    ])
                  : m("div", [
                      m("p", { style: "padding: 8px 0; color: #666;" }, "Sharing is not enabled for this service."),
                      m("div.share-modal-footer", [
                        m("button.share-modal-btn.share-modal-btn-secondary", { onclick: close }, "Close"),
                        m(
                          "button.share-modal-btn.share-modal-btn",
                          {
                            disabled: modalRequestInProgress,
                            onclick: () => requestEdit(serviceName, onClose),
                          },
                          modalRequestInProgress ? "Sending..." : "Enable sharing",
                        ),
                      ]),
                    ]),
        ]),
      ],
    );
  },
};
