// The landing page's providers panel (port of the deleted Landing.jinja
// inline providers JS). Collapsed by default to a one-line summary
// ("X providers enabled (N errors)" + chevron); expanding reveals freshness
// counters + the provider list with Enable / Disable buttons. Toggles are
// optimistic-pending ("Waiting...") until a providers_state snapshot at or
// past the click arrives (the store owns that acknowledgement logic).
import m from "mithril";

import type { ChromeProviderEntry } from "../chrome_state";
import { patchProviderEnabled, relativeAgo } from "../landing_service";
import { beginProviderToggle, failProviderToggle, getProviders, isProviderTogglePending } from "../store";

const BADGE_BASE = "inline-flex items-center px-2 py-0.5 rounded-md type-label ";

function statusBadgeClass(status: string): string {
  if (status === "ok") return `${BADGE_BASE}bg-success/15 text-success`;
  if (status === "error") return `${BADGE_BASE}bg-important/15 text-important`;
  return `${BADGE_BASE}bg-fill-subtle text-primary`;
}

function statusBadgeLabel(status: string): string {
  if (status === "ok") return "OK";
  if (status === "error") return "Error";
  if (status === "disabled") return "Disabled";
  return status;
}

function toggleProvider(name: string, isEnabled: boolean): void {
  beginProviderToggle(name, Date.now());
  void patchProviderEnabled(name, isEnabled).then((failure) => {
    if (failure === null) return;
    // The toggle failed (e.g. 409 when disabling a provider that still has
    // active workspaces): drop the optimistic pending state and surface the
    // server's reason instead of silently reverting.
    failProviderToggle(name);
    if (failure.status === 409) {
      window.alert(`Cannot disable ${name}: ${failure.message}`);
    } else {
      console.error("Provider toggle for", name, "returned", failure.status, failure.message);
    }
  });
}

function providerRow(entry: ChromeProviderEntry): m.Children {
  const isPending = isProviderTogglePending(entry.name);
  return m(
    "div",
    // Same surface as Card.jinja: ``.minds-card`` carries the
    // bg/border/rounded; the utilities handle the tight padding + flex row.
    { class: "minds-card flex items-center gap-1.5 px-4 py-2", "data-provider-name": entry.name },
    [
      m("span", { class: "font-semibold text-primary" }, entry.name),
      entry.backend !== null ? m("span", { class: "type-helper text-secondary" }, entry.backend) : null,
      m("span", { class: statusBadgeClass(entry.status) }, statusBadgeLabel(entry.status)),
      entry.status === "error" && entry.error_message !== undefined
        ? m(
            "span",
            {
              class: "flex-1 type-helper text-primary truncate",
              title: `${entry.error_type ?? ""}: ${entry.error_message}`,
            },
            `${entry.error_type ?? ""}: ${entry.error_message}`,
          )
        : m("span", { class: "flex-1" }),
      isPending
        ? m(
            "button",
            {
              type: "button",
              class:
                "px-2 py-1 type-helper rounded-md border border-default bg-surface-primary hover:bg-fill-hover text-primary",
              disabled: true,
            },
            "Waiting…",
          )
        : m(
            "button",
            {
              type: "button",
              class:
                "px-2 py-1 type-helper rounded-md border border-default bg-surface-primary hover:bg-fill-hover text-primary",
              onclick: () => toggleProvider(entry.name, entry.status === "disabled"),
            },
            entry.status === "disabled" ? "Enable" : "Disable",
          ),
    ],
  );
}

export function ProvidersPanel(): m.Component {
  let isExpanded = false;
  // Re-render the "N ago" freshness counters once a second while mounted.
  let freshnessTimer: number | null = null;
  return {
    oncreate() {
      freshnessTimer = window.setInterval(() => m.redraw(), 1000);
    },
    onremove() {
      if (freshnessTimer !== null) window.clearInterval(freshnessTimer);
    },
    view() {
      const providersPayload = getProviders();
      const entries = providersPayload?.providers ?? [];
      if (entries.length === 0) return null;
      const enabledCount = entries.filter((entry) => entry.status !== "disabled").length;
      const errorCount = entries.filter((entry) => entry.status === "error").length;
      const providerWord = enabledCount === 1 ? "provider" : "providers";
      const errorSuffix = errorCount > 0 ? ` (${errorCount} ${errorCount === 1 ? "error" : "errors"})` : "";
      return m("section", { class: "mt-8 pt-6 border-t border-default", "data-providers-panel": "" }, [
        m(
          "button",
          {
            type: "button",
            "data-providers-toggle": "",
            class:
              "w-full flex items-center justify-between text-left type-body text-primary hover:text-primary bg-transparent border-0 cursor-pointer p-0",
            onclick: () => {
              isExpanded = !isExpanded;
            },
          },
          [
            m("span", { "data-providers-summary": "" }, `${enabledCount} ${providerWord} enabled${errorSuffix}`),
            m("span", { class: "text-tertiary ml-2" }, isExpanded ? "▾" : "▸"),
          ],
        ),
        isExpanded
          ? m("div", { class: "mt-3", "data-providers-details": "" }, [
              m("div", { class: "flex items-center justify-end mb-3" }, [
                m("div", { class: "type-helper text-secondary flex gap-4" }, [
                  m("span", ["last event ", m("span", relativeAgo(providersPayload?.last_event_at ?? null))]),
                  m("span", [
                    "last snapshot ",
                    m("span", relativeAgo(providersPayload?.last_full_snapshot_at ?? null)),
                  ]),
                ]),
              ]),
              m("div", { class: "flex flex-col gap-1.5", "data-providers-list": "" }, entries.map(providerRow)),
            ])
          : null,
      ]);
    },
  };
}
