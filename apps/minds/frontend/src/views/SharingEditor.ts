// The sharing editor: heading, shared-URL section, three-state access list
// (existing / added / removed), Update-or-Share / Disable actions, and the
// in-place state refresh after a save (the deleted static/sharing.js).
//
// Runs on two surfaces: the full /sharing page (browser mode) and the
// centered sharing modal hosted in the Electron overlay surface. Behavior
// matches the full page exactly, with one difference carried over from the
// JS it replaces: after Update/Disable the editor re-fetches the sharing
// state in place instead of navigating -- a navigation (or reload) would
// blank the modal's overlay iframe, so the editor stays visible (grayed out)
// until the fresh state is applied.
//
// Every email renders through vdom text nodes, never HTML, so a crafted
// email cannot inject script (the same property the DOM-building JS had).
import m from "mithril";

import { normalizeApiError } from "../api_errors";
import type { SharingBootExtras, SharingBootIsland } from "../chrome_state";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { TEXT_INPUT_CLASSES, buttonClasses } from "../ui";
import { Spinner } from "./Spinner";

// Cloudflare can take a few seconds after sharing is enabled to publish the
// Access app at the edge. Until then the hostname does not return the Access
// login redirect, so revealing the URL immediately makes forwarding look
// broken. Poll the minds-side readiness probe (which checks for the Access
// 302) and only reveal the link once the edge is live, or after a short
// timeout with a "may take a moment" note so the user is never stuck.
const READINESS_POLL_INTERVAL_MS = 2000;
const READINESS_MAX_ATTEMPTS = 12;

const COPY_LABEL_RESET_MS = 2000;

// The status endpoint emits the AuthPolicy shape from the imbue_cloud
// plugin: {"emails": [...], "email_domains": [...], "require_idp": ...}.
interface SharingStatusResponse {
  enabled?: boolean;
  url?: string;
  policy?: { emails?: unknown };
}

function emailsFromPolicy(policy: { emails?: unknown } | undefined): string[] {
  if (policy === undefined || !Array.isArray(policy.emails)) return [];
  return policy.emails.map(String);
}

// fetch only rejects on network failure -- a 4xx/5xx response is a successful
// Promise. Wrap it so callers can treat transport and server-side errors
// uniformly, with the body routed through the shared error normalizer.
async function requestWithErrorCheck(url: string, options?: RequestInit): Promise<Response> {
  const response = await fetch(url, options);
  if (response.ok) return response;
  const text = await response.text();
  let detail = text;
  try {
    detail = normalizeApiError(JSON.parse(text)).message;
  } catch {
    // Leave detail as the raw body text.
  }
  throw new Error(detail !== "" ? detail : `HTTP ${response.status}`);
}

type LoadPhase = "loading" | "ready" | "load_failed";
type UrlPhase = "hidden" | "provisioning" | "ready";
type AclVariant = "existing" | "added" | "removed";

const ACL_ROW_BASE = "flex items-center justify-between px-3 py-2 border rounded-md my-1 ";
const ACL_ROW_VARIANTS: Record<AclVariant, string> = {
  existing: "bg-surface-primary border-default",
  added: "bg-success/12 border-success/30",
  removed: "bg-important/12 border-important/30 line-through",
};

export interface MountSharingEditorOptions {
  // Modal-mode Cancel: dismiss the overlay (the modal page's inline script
  // supplies its dismissSharingModal). Unused on the full page, whose Cancel
  // is a link back to workspace settings.
  onDismiss?: () => void;
}

// One controller per mount; the editor and the heading (a second, sibling
// mount root) both render from it.
class SharingController {
  extras: SharingBootExtras;
  onDismiss: () => void;
  phase: LoadPhase = "loading";
  loadErrorMessage = "";
  isEnabled = false;
  existing: string[] = [];
  added: string[] = [];
  removed: string[] = [];
  draftEmail = "";
  shareUrl = "";
  urlPhase: UrlPhase = "hidden";
  isShowingFallbackNote = false;
  isSubmitting = false;
  errorMessage: string | null = null;
  copyLabel = "Copy";
  private copyResetTimer: number | null = null;
  // Invalidates the previous readiness poll loop when a save restarts it.
  private pollSeq = 0;

  constructor(extras: SharingBootExtras, onDismiss: () => void) {
    this.extras = extras;
    this.onDismiss = onDismiss;
  }

  statusUrl(): string {
    return `/api/v1/workspaces/${this.extras.agent_id}/sharing/${this.extras.service_name}`;
  }

  finalEmails(): string[] {
    return this.existing.filter((email) => !this.removed.includes(email)).concat(this.added);
  }

  addDraftEmail(): void {
    const email = this.draftEmail.trim();
    if (email === "") return;
    if (this.removed.includes(email)) {
      this.removed = this.removed.filter((entry) => entry !== email);
    } else if (!this.existing.includes(email) && !this.added.includes(email)) {
      this.added.push(email);
    }
    this.draftEmail = "";
  }

  markRemoved(email: string): void {
    if (!this.removed.includes(email)) this.removed.push(email);
  }

  unmarkAdded(email: string): void {
    this.added = this.added.filter((entry) => entry !== email);
  }

  unmarkRemoved(email: string): void {
    this.removed = this.removed.filter((entry) => entry !== email);
  }

  copyUrl(): void {
    void navigator.clipboard.writeText(this.shareUrl);
    this.copyLabel = "Copied";
    if (this.copyResetTimer !== null) window.clearTimeout(this.copyResetTimer);
    this.copyResetTimer = window.setTimeout(() => {
      this.copyLabel = "Copy";
      m.redraw();
    }, COPY_LABEL_RESET_MS);
  }

  async fetchStatus(): Promise<SharingStatusResponse> {
    const response = await requestWithErrorCheck(this.statusUrl());
    return (await response.json()) as SharingStatusResponse;
  }

  // Apply a fresh status payload -- exactly what the full page computes on
  // load. ``isInitial`` folds the URL-proposed emails in only once (a page
  // reload re-reads them from the URL; the in-place refresh must not re-add
  // drafts the user just committed or discarded).
  applyLoadedState(data: SharingStatusResponse, isInitial: boolean): void {
    const serverEmails = emailsFromPolicy(data.policy);
    if (data.enabled === true) {
      this.isEnabled = true;
      this.existing = serverEmails;
      if (typeof data.url === "string" && data.url !== "") {
        this.shareUrl = data.url;
        this.startReadinessPolling(data.url);
      }
    } else {
      this.isEnabled = false;
      // Treat the default policy (owner email) as the editor's initial draft
      // so the user sees their own email pre-populated.
      serverEmails.forEach((email) => {
        if (!this.added.includes(email)) this.added.push(email);
      });
      this.urlPhase = "hidden";
    }
    if (isInitial) {
      this.extras.initial_emails.forEach((email) => {
        if (!this.existing.includes(email) && !this.added.includes(email)) this.added.push(email);
      });
    }
    m.redraw();
  }

  startReadinessPolling(url: string): void {
    this.urlPhase = "provisioning";
    this.isShowingFallbackNote = false;
    const seq = ++this.pollSeq;
    let attempts = 0;
    const poll = async (): Promise<void> => {
      attempts += 1;
      const probeUrl = `${this.statusUrl()}/readiness?url=${encodeURIComponent(url)}`;
      let isReady = false;
      try {
        const response = await fetch(probeUrl);
        if (response.ok) {
          const data = (await response.json()) as { ready?: boolean };
          isReady = data.ready === true;
        }
      } catch {
        // Not ready yet.
      }
      if (seq !== this.pollSeq) return;
      if (isReady) {
        this.urlPhase = "ready";
      } else if (attempts >= READINESS_MAX_ATTEMPTS) {
        this.urlPhase = "ready";
        this.isShowingFallbackNote = true;
      } else {
        window.setTimeout(() => void poll(), READINESS_POLL_INTERVAL_MS);
        return;
      }
      m.redraw();
    };
    void poll();
  }

  // After a successful Update/Disable, re-fetch and apply the fresh state in
  // place of the full page's old navigation-to-self. The editor stays
  // visible and grayed out until the state lands.
  async refreshAfterSave(): Promise<void> {
    this.existing = [];
    this.added = [];
    this.removed = [];
    try {
      const data = await this.fetchStatus();
      this.applyLoadedState(data, false);
      this.isSubmitting = false;
    } catch (error) {
      this.isSubmitting = false;
      this.errorMessage = `Saved, but refreshing the editor failed: ${error instanceof Error ? error.message : String(error)}`;
    }
    m.redraw();
  }

  async submitUpdate(): Promise<void> {
    this.errorMessage = null;
    this.isSubmitting = true;
    m.redraw();
    try {
      await requestWithErrorCheck(this.statusUrl(), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emails: this.finalEmails() }),
      });
      await this.refreshAfterSave();
    } catch (error) {
      this.errorMessage = `Could not save sharing changes: ${error instanceof Error ? error.message : String(error)}`;
      this.isSubmitting = false;
      m.redraw();
    }
  }

  async submitDisable(): Promise<void> {
    this.errorMessage = null;
    this.isSubmitting = true;
    m.redraw();
    try {
      await requestWithErrorCheck(this.statusUrl(), { method: "DELETE" });
      await this.refreshAfterSave();
    } catch (error) {
      this.errorMessage = `Could not disable sharing: ${error instanceof Error ? error.message : String(error)}`;
      this.isSubmitting = false;
      m.redraw();
    }
  }

  async load(): Promise<void> {
    try {
      const data = await this.fetchStatus();
      this.phase = "ready";
      this.applyLoadedState(data, true);
    } catch (error) {
      this.phase = "load_failed";
      this.loadErrorMessage = `Failed to load sharing status: ${error instanceof Error ? error.message : String(error)}`;
      this.added = this.extras.initial_emails.slice();
    }
    m.redraw();
  }
}

// The page/modal heading. The modal renders names as PLAIN TEXT: a link
// there would navigate the overlay iframe to a full page and strand the app
// inside the modal. The full page keeps its links.
export function SharingHeading(): m.Component<{ controller: SharingController }> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const { extras } = controller;
      const isPlain = extras.is_modal;
      const wsName = extras.ws_name !== "" ? extras.ws_name : extras.agent_id;
      const parts: m.Children[] = [
        controller.isEnabled ? "" : "Share ",
        m("code", { class: "code-pill" }, extras.service_name),
        controller.isEnabled ? " shared in " : " in ",
        isPlain
          ? wsName
          : m(
              "a",
              { href: `${extras.mngr_forward_origin}/goto/${extras.agent_id}/`, class: "text-accent hover:underline" },
              wsName,
            ),
      ];
      if (extras.account_email !== "") {
        parts.push(
          " (",
          isPlain ? extras.account_email : m("a", { href: "/accounts", class: "text-accent hover:underline" }, extras.account_email),
          ")",
        );
      }
      if (!controller.isEnabled) parts.push("?");
      return parts;
    },
  };
}

function aclRow(controller: SharingController, email: string, variant: AclVariant): m.Children {
  const action =
    variant === "added"
      ? (): void => controller.unmarkAdded(email)
      : variant === "removed"
        ? (): void => controller.unmarkRemoved(email)
        : (): void => controller.markRemoved(email);
  return m("div", { class: ACL_ROW_BASE + ACL_ROW_VARIANTS[variant] }, [
    m("span", [
      variant === "existing"
        ? null
        : m(
            "span",
            { class: `font-semibold mr-1.5 ${variant === "added" ? "text-success" : "text-important"}` },
            variant === "added" ? "+" : "−",
          ),
      m("span", { class: `type-body ${variant === "removed" ? "text-tertiary" : "text-primary"}` }, email),
    ]),
    m(
      "button",
      {
        class: "bg-transparent border-none cursor-pointer text-tertiary type-heading px-1 hover:text-primary",
        "aria-label": "Remove",
        onclick: action,
      },
      "×",
    ),
  ]);
}

export function SharingEditor(): m.Component<{ controller: SharingController }> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const { extras } = controller;
      if (controller.phase === "loading") {
        return m("p", { id: "loading-state", class: "text-secondary py-4" }, "Loading...");
      }
      const visibleRows: m.Children[] = [
        ...controller.existing
          .filter((email) => !controller.removed.includes(email))
          .map((email) => aclRow(controller, email, "existing")),
        ...controller.added.map((email) => aclRow(controller, email, "added")),
        ...controller.removed.map((email) => aclRow(controller, email, "removed")),
      ];
      return [
        controller.phase === "load_failed"
          ? m("p", { id: "loading-state", class: "text-important py-4" }, controller.loadErrorMessage)
          : null,
        m(
          "div",
          {
            id: "editor-content",
            class: controller.isSubmitting ? "opacity-50 pointer-events-none" : "",
          },
          [
            controller.urlPhase === "hidden"
              ? null
              : m("div", { id: "url-section", class: "mb-4" }, [
                  m("p", { class: "font-semibold mb-1" }, "Shared URL"),
                  controller.urlPhase === "provisioning"
                    ? m("div", { id: "url-provisioning", class: "flex gap-2 items-center type-body text-secondary my-2" }, [
                        m(Spinner, { size: "sm" }),
                        m("span", "Provisioning share…"),
                      ])
                    : m("div", { id: "url-ready" }, [
                        m(
                          "div",
                          { class: "flex gap-2 items-center bg-fill-subtle border border-default rounded-md px-3 py-2 my-2" },
                          [
                            m("input", {
                              type: "text",
                              id: "share-url",
                              readonly: true,
                              class: "flex-1 bg-transparent border-0 type-body text-primary font-mono outline-none",
                              value: controller.shareUrl,
                              onclick: (event: Event) => (event.target as HTMLInputElement).select(),
                            }),
                            m(
                              "button",
                              {
                                type: "button",
                                id: "copy-btn",
                                class: buttonClasses("secondary"),
                                disabled: controller.isSubmitting,
                                onclick: () => controller.copyUrl(),
                              },
                              controller.copyLabel,
                            ),
                          ],
                        ),
                        controller.isShowingFallbackNote
                          ? m(
                              "p",
                              { id: "url-fallback-note", class: "type-helper text-tertiary mt-1" },
                              "This link may take a moment to become reachable.",
                            )
                          : null,
                      ]),
                ]),
            m("h2", { class: "type-label text-secondary mt-6 mb-3" }, "Access List"),
            m(
              "div",
              { id: "email-list" },
              visibleRows.length > 0
                ? visibleRows
                : m("p", { class: "type-body text-tertiary" }, "No one in the access list"),
            ),
            m("div", { class: "flex gap-2 items-center mt-2" }, [
              m("input", {
                type: "email",
                name: "new_email",
                id: "new-email",
                placeholder: "Add email address",
                class: `${TEXT_INPUT_CLASSES} flex-1`,
                disabled: controller.isSubmitting,
                value: controller.draftEmail,
                oninput: (event: Event) => {
                  controller.draftEmail = (event.target as HTMLInputElement).value;
                },
                onkeydown: (event: KeyboardEvent) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    controller.addDraftEmail();
                  }
                },
              }),
              m(
                "button",
                {
                  type: "button",
                  class: buttonClasses("secondary"),
                  disabled: controller.isSubmitting,
                  onclick: () => controller.addDraftEmail(),
                },
                "Add",
              ),
            ]),
            controller.errorMessage !== null
              ? m(
                  "div",
                  {
                    id: "sharing-error",
                    class: "mt-3 mb-1 px-3 py-2 rounded-md bg-important/12 border border-important/30 type-body text-important",
                  },
                  controller.errorMessage,
                )
              : null,
            controller.isSubmitting
              ? m("div", { id: "submit-spinner", class: "py-4" }, m("span", { class: "text-secondary" }, "Saving changes..."))
              : m("div", { class: "flex gap-2 mt-4 justify-between", id: "action-buttons" }, [
                  m("div", { class: "flex gap-2" }, [
                    extras.is_modal
                      ? m(
                          "button",
                          { type: "button", class: buttonClasses("secondary"), onclick: () => controller.onDismiss() },
                          "Cancel",
                        )
                      : m(
                          "a",
                          { href: `/workspace/${extras.agent_id}/settings`, class: buttonClasses("secondary") },
                          "Cancel",
                        ),
                    controller.isEnabled
                      ? m(
                          "button",
                          {
                            type: "button",
                            id: "disable-btn",
                            class: buttonClasses("danger"),
                            onclick: () => void controller.submitDisable(),
                          },
                          "Disable Sharing",
                        )
                      : null,
                  ]),
                  m(
                    "button",
                    {
                      type: "button",
                      id: "action-btn",
                      class: buttonClasses("success"),
                      onclick: () => void controller.submitUpdate(),
                    },
                    controller.isEnabled ? "Update" : "Share",
                  ),
                ]),
          ],
        ),
      ];
    },
  };
}

// Mount the sharing editor into its container and take over the
// server-rendered #page-heading (a second mount root: the two wrappers place
// the heading differently -- the modal pins it above its scroll area).
export function mountSharingEditor(target: Element | null, options?: MountSharingEditorOptions): void {
  const el = requireElement(target, "sharing editor container");
  const island = readBootState() as SharingBootIsland;
  if (island.sharing === undefined) {
    throw new MindsUIError("sharing boot island is missing the sharing slice");
  }
  const controller = new SharingController(island.sharing, options?.onDismiss ?? ((): void => undefined));
  mountWithTeardown(el, { view: () => m(SharingEditor, { controller }) });
  const heading = document.getElementById("page-heading");
  if (heading !== null) {
    mountWithTeardown(heading, { view: () => m(SharingHeading, { controller }) });
  }
  void controller.load();
}
