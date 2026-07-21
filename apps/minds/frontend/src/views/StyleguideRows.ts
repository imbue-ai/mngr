// The styleguide's "Sidebar items" sample rows, rendered through the same
// WorkspaceRow component the live menu uses so the catalog can't drift from
// production. Explicit accents; withOpenNew shows the richest (Electron)
// treatment: the current row carries no arrow, the other rows do. No event
// wiring -- these are visual only.
import m from "mithril";

import type { ChromeWorkspaceEntry } from "../chrome_state";
import { mountWithTeardown, requireElement } from "../mount";
import { Badge } from "./Badge";
import { Icon } from "./Icon";
import { Spinner } from "./Spinner";
import { WorkspaceRow } from "./WorkspaceRow";

interface SampleRow {
  workspace: ChromeWorkspaceEntry;
  isCurrent: boolean;
}

const SAMPLE_ROWS: SampleRow[] = [
  { workspace: { id: "agent-styleguide-current", name: "current-workspace", accent: "#0b292b" }, isCurrent: true },
  { workspace: { id: "agent-styleguide-other", name: "another-workspace", accent: "#9fbbd3" }, isCurrent: false },
  {
    workspace: { id: "agent-styleguide-stopped", name: "stopped-workspace", accent: "#492222", liveness: "STOPPED" },
    isCurrent: false,
  },
  {
    workspace: { id: "agent-styleguide-stale", name: "stale-workspace", accent: "#cecd0c", is_stale: "true" },
    isCurrent: false,
  },
];

export function mountStyleguideWorkspaceRows(target: Element | null): void {
  const el = requireElement(target, "styleguide sidebar-rows panel");
  mountWithTeardown(el, {
    view: () =>
      SAMPLE_ROWS.map((sample) =>
        m(WorkspaceRow, { workspace: sample.workspace, isCurrent: sample.isCurrent, withOpenNew: true }),
      ),
  });
}

// The JS-components catalog line for the primitive components (Icon / Badge /
// Spinner), so each has a live, drift-proof sample next to its JinjaX twin.
export function mountStyleguidePrimitives(target: Element | null): void {
  const el = requireElement(target, "styleguide JS-primitives panel");
  mountWithTeardown(el, {
    view: () =>
      m("div", { class: "flex items-center gap-4 text-primary" }, [
        m(Icon, { name: "home" }),
        m(Icon, { name: "settings" }),
        m(Icon, { name: "arrow-up-right", extra: "text-accent" }),
        m(Badge, {}),
        m(Badge, { count: 3 }),
        m(Badge, { count: 150 }),
        m(Spinner, { size: "sm" }),
        m(Spinner, { size: "md", tone: "accent" }),
      ]),
  });
}
