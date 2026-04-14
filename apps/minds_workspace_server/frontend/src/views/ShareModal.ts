/**
 * Modal dialog for managing Cloudflare forwarding (sharing) for a server.
 * Shows sharing status, auth policy (email list), and allows enabling/disabling/updating.
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
  auth_rules: Array<{ action: string; include: Array<{ email?: { email: string } }> }>;
}

// Module-level state for the share modal (only one can be open at a time)
let modalLoading = true;
let modalStatus: SharingStatus | null = null;
let modalError: string | null = null;
let modalActionInProgress = false;
let modalCopied = false;
let modalFetchedFor: string | null = null;
let modalNewEmail = "";

function resetModalState(): void {
  modalLoading = true;
  modalStatus = null;
  modalError = null;
  modalActionInProgress = false;
  modalCopied = false;
  modalFetchedFor = null;
  modalNewEmail = "";
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

function buildAuthRules(emails: string[]): Array<{ action: string; include: Array<{ email: { email: string } }> }> {
  if (emails.length === 0) return [];
  return [{
    action: "allow",
    include: emails.map((email) => ({ email: { email } })),
  }];
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

async function enableSharing(serverName: string, emails: string[]): Promise<void> {
  modalActionInProgress = true;
  m.redraw();
  try {
    const body: { auth_rules?: object[] } = {};
    if (emails.length > 0) {
      body.auth_rules = buildAuthRules(emails);
    }
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      modalError = (data as { detail?: string }).detail ?? `HTTP ${response.status}`;
    } else {
      // The enable response is provisional (may not have URL yet).
      // Re-fetch to get the full status including the URL.
      modalActionInProgress = false;
      await fetchStatus(serverName);
      return;
    }
  } catch (e) {
    modalError = `Network error: ${(e as Error).message}`;
  }
  modalActionInProgress = false;
  m.redraw();
}

async function updateAuth(serverName: string, emails: string[]): Promise<void> {
  modalActionInProgress = true;
  m.redraw();
  try {
    const response = await fetch(apiUrl(`/api/sharing/${encodeURIComponent(serverName)}/auth`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auth_rules: buildAuthRules(emails) }),
    });
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
      modalActionInProgress = false;
      m.redraw();
      return;
    }
    // Re-fetch to get the default auth policy for the disabled state
    modalActionInProgress = false;
    await fetchStatus(serverName);
  } catch (e) {
    modalError = `Network error: ${(e as Error).message}`;
    modalActionInProgress = false;
    m.redraw();
  }
}

function addEmail(emails: string[], serverName: string, isEnabled: boolean): void {
  const email = modalNewEmail.trim();
  if (!email || emails.includes(email)) return;
  const updated = [...emails, email];
  modalNewEmail = "";
  if (isEnabled) {
    updateAuth(serverName, updated);
  } else if (modalStatus) {
    modalStatus.auth_rules = buildAuthRules(updated);
  }
}

function removeEmail(emails: string[], email: string, serverName: string, isEnabled: boolean): void {
  const updated = emails.filter((e) => e !== email);
  if (isEnabled) {
    updateAuth(serverName, updated);
  } else if (modalStatus) {
    modalStatus.auth_rules = buildAuthRules(updated);
  }
}

function renderEmailList(emails: string[], serverName: string, isEnabled: boolean): m.Vnode {
  return m("div.share-modal-emails", [
    emails.length === 0
      ? null
      : m("ul.share-modal-email-list",
          emails.map((email) =>
            m("li.share-modal-email-item", { key: email }, [
              m("span", email),
              m("button.share-modal-email-remove", {
                title: "Remove",
                onclick: () => removeEmail(emails, email, serverName, isEnabled),
              }, "x"),
            ]),
          ),
        ),
    m("div.share-modal-add-email", [
      m("input.share-modal-email-input", {
        type: "email",
        placeholder: "user@example.com",
        value: modalNewEmail,
        oninput: (e: Event) => { modalNewEmail = (e.target as HTMLInputElement).value; },
        onkeydown: (e: KeyboardEvent) => {
          if (e.key === "Enter" && modalNewEmail.trim()) {
            e.preventDefault();
            addEmail(emails, serverName, isEnabled);
          }
        },
      }),
      m("button.share-modal-btn.share-modal-btn-secondary", {
        disabled: !modalNewEmail.trim(),
        onclick: () => addEmail(emails, serverName, isEnabled),
      }, "Add"),
    ]),
  ]);
}

export const ShareModal: m.Component<ShareModalAttrs> = {
  view(vnode) {
    const { serverName, onClose } = vnode.attrs;

    if (modalFetchedFor !== serverName) {
      resetModalState();
      modalFetchedFor = serverName;
      fetchStatus(serverName);
    }

    const emails = modalStatus ? extractEmails(modalStatus) : [];
    const isEnabled = modalStatus?.enabled ?? false;
    const title = isEnabled ? `Edit ${serverName} sharing` : `Share ${serverName} with...`;

    const close = () => { resetModalState(); onClose(); };

    return m("div.share-modal-overlay", { onclick: (e: Event) => { if (e.target === e.currentTarget) close(); } }, [
      m("div.share-modal", [
        m("div.share-modal-header", [
          m("h3.share-modal-title", title),
          m("button.share-modal-close-x", { onclick: close, title: "Close" }, "x"),
        ]),

        modalLoading
          ? m("p.share-modal-loading", "Loading...")
          : modalError
            ? m("div", [
                m("p.share-modal-error", modalError),
                m("button.share-modal-btn.share-modal-btn-secondary", {
                  onclick: () => fetchStatus(serverName),
                }, "Retry"),
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
                    m("button.share-modal-btn.share-modal-btn-secondary", {
                      onclick: () => {
                        if (modalStatus?.url) {
                          navigator.clipboard.writeText(modalStatus.url);
                          modalCopied = true;
                          setTimeout(() => { modalCopied = false; m.redraw(); }, 2000);
                        }
                      },
                    }, modalCopied ? "Copied" : "Copy"),
                  ]),
                  renderEmailList(emails, serverName, true),
                  m("div.share-modal-footer", [
                    m("div.share-modal-footer-left", [
                      m("button.share-modal-btn.share-modal-btn-secondary", { onclick: close }, "Cancel"),
                      m("button.share-modal-btn.share-modal-btn-destructive", {
                        disabled: modalActionInProgress,
                        onclick: () => disableSharing(serverName),
                      }, "Disable sharing"),
                    ]),
                    m("button.share-modal-btn.share-modal-btn", {
                      disabled: modalActionInProgress,
                      onclick: () => updateAuth(serverName, emails),
                    }, modalActionInProgress ? "Updating..." : "Update"),
                  ]),
                ])
              : m("div", [
                  renderEmailList(emails, serverName, false),
                  m("div.share-modal-footer", [
                    m("button.share-modal-btn.share-modal-btn-secondary", { onclick: close }, "Cancel"),
                    m("button.share-modal-btn.share-modal-btn", {
                      disabled: modalActionInProgress,
                      onclick: () => enableSharing(serverName, emails),
                    }, modalActionInProgress ? "Enabling..." : "Enable sharing"),
                  ]),
                ]),
      ]),
    ]);
  },
};
