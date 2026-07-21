// The swap-engine-compatible mount protocol shared by every MindsUI mount
// function (normative -- see specs/minds-chrome-mithril-migration/spec.md,
// "Mount protocol"). A converted page's Jinja shell renders a mount container
// plus a JSON boot-state island; the page's inline script (re-run per swap by
// the chrome.js swap engine) calls the mount function, which reads the island
// and mounts synchronously so the first paint is complete on arrival.
import m from "mithril";

// Dispatched by the chrome.js swap engine on the window when it swaps the
// current page body out in place (no document teardown). A mounted component
// must unmount then, exactly like the inline page scripts guard their timers.
const PAGE_TEARDOWN_EVENT = "minds:page-teardown";

// The id of the JSON island a converted page's Jinja shell renders
// (`<script type="application/json" id="minds-boot-state">`). One per page.
const BOOT_STATE_ISLAND_ID = "minds-boot-state";

export class MindsUIError extends Error {}

export function requireElement(target: Element | null, description: string): Element {
  if (target === null) {
    throw new MindsUIError(`mount target not found: ${description}`);
  }
  return target;
}

// Reads and parses the current document's boot-state island. Throws rather
// than falling back: a converted page without a (valid) island is a template
// bug that must surface, not render empty.
export function readBootState(): unknown {
  const island = document.getElementById(BOOT_STATE_ISLAND_ID);
  if (island === null) {
    throw new MindsUIError(`missing #${BOOT_STATE_ISLAND_ID} island in this document`);
  }
  try {
    return JSON.parse(island.textContent ?? "");
  } catch (error) {
    throw new MindsUIError(`invalid JSON in #${BOOT_STATE_ISLAND_ID}: ${String(error)}`);
  }
}

// Mounts a component and registers its swap-engine teardown. The
// `data-minds-mounted` marker lets harnesses (visual diff, Playwright) wait
// for client rendering to have happened before capturing.
export function mountWithTeardown(el: Element, component: m.ComponentTypes): void {
  m.mount(el, component);
  el.setAttribute("data-minds-mounted", "true");
  window.addEventListener(
    PAGE_TEARDOWN_EVENT,
    () => {
      m.mount(el, null);
      el.removeAttribute("data-minds-mounted");
    },
    { once: true },
  );
}

// Mounts a component that belongs to the persistent SHELL (e.g. the
// browser-mode workspace menu living outside #local-page-root): it must
// survive hub-page swaps, so no teardown listener is registered. Only use
// this for containers outside the swappable page root -- a page-scoped mount
// that skipped teardown would leak across swaps.
export function mountPersistent(el: Element, component: m.ComponentTypes): void {
  m.mount(el, component);
  el.setAttribute("data-minds-mounted", "true");
}
