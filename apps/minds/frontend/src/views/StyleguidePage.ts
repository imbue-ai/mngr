// The /_dev/styleguide page. The big static catalog (design tokens + component
// patterns) is a trusted HTML constant rendered via m.trust; the three live
// sections are real mithril mounts (the smoke island, the Icon/Badge/Spinner
// primitives, and the shared WorkspaceRow) placed into their containers after
// the static shell renders. Dev-only; not part of any user flow.
import m from "mithril";

import { mountWithTeardown, requireElement } from "../mount";
import { mountStyleguidePrimitives, mountStyleguideWorkspaceRows } from "./StyleguideRows";
import { mountStyleguideSmoke } from "./StyleguideSmoke";
import { STYLEGUIDE_CATALOG_HTML } from "./styleguide_catalog";

export function mountStyleguidePage(target: Element | null): void {
  const el = requireElement(target, "styleguide page container");
  // m.trust renders the static catalog (a trusted constant -- never
  // interpolated data). onupdate is a no-op: the catalog never changes, so
  // the trusted subtree is stable and the live-section mounts below own their
  // own containers.
  mountWithTeardown(el, { view: () => m.trust(STYLEGUIDE_CATALOG_HTML) });
  // Place the live component sections into the containers the static catalog
  // rendered.
  mountStyleguideSmoke(el.querySelector("#styleguide-js-smoke"));
  mountStyleguidePrimitives(el.querySelector("#styleguide-js-primitives"));
  mountStyleguideWorkspaceRows(el.querySelector("#styleguide-sidebar-rows"));
}
